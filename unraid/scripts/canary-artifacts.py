#!/usr/bin/env python3
"""Offline canary kit preparation, signing and independent verification.

No subcommand installs packages, contacts Unraid, issues live host approvals or
starts services. Public keys inside a kit are documentation, NEVER trust anchors:
verify requires a separately trusted key outside the kit and its SHA256 fingerprint.
Prepared artifacts are not permission to run them. The production owner still has
to approve the exact first-write plan and issue a distinct short-lived host token.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from unraid.lab.archive import verify as verify_archive  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMAGE = load_script("make-canary-image")
PACKAGE = load_script("build-canary-package")
SHA = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
KIT_IDENTITY = "polyseat-unraid-canary-artifact"
KIT_NAMESPACE = "polyseat-unraid-canary-kit-v1"
PACKAGE_NAMESPACE = "polyseat-unraid-canary-controller-v1"
MANIFEST = "canary-kit.json"
MAX_TTL = 7 * 24 * 60 * 60
MAX_KIT_BYTES = 320 * 1024 * 1024
IMAGE_PATH = "payload/share/canary/busybox.tar.gz"
METADATA = {MANIFEST, MANIFEST + ".sig", "SHA256SUMS"}
SCOPES = {"host_approval": False, "gpu": False, "network": False, "host_services": False,
          "persistent_data_test": False, "data_dir": "/var/lib/polyseat-unraid-canary"}
REHEARSAL_STEPS = {"signature-stage", "activate", "prestart-policy", "canary-smoke",
                   "expiry-refusal", "cleanup", "host-state-unchanged"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def regular(path: Path, limit=MAX_KIT_BYTES) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError(f"not a bounded independent regular file: {path.name}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        actual = os.fstat(source.fileno())
        if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("input changed while opening")
        result = source.read(limit + 1)
    if len(result) > limit:
        raise ValueError("input exceeded read limit")
    return result


def strict_json(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj
    return json.loads(data, object_pairs_hook=pairs)


def write_new(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def safe_relative(name):
    return (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./+-]{0,240}", name)
            and all(part and not part.startswith(".") for part in name.split("/")))


def inspect_runtime(archive: Path, manifest: Path):
    data = regular(archive, 256 * 1024 * 1024)
    spec = strict_json(regular(manifest, 1024 * 1024))
    if (not isinstance(spec, dict) or type(spec.get("schema")) is not int or spec.get("schema") != 1
            or spec.get("archive_sha256") != sha(data)):
        raise ValueError("runtime archive digest or schema mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as contents:
        verify_archive(contents, spec)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as contents:
        try:
            image = contents.extractfile(IMAGE_PATH).read(8 * 1024 * 1024 + 1)
        except (KeyError, AttributeError) as error:
            raise ValueError("runtime bundle must contain the fixed canary image") from error
    image_facts = IMAGE.validate_image(image)
    return spec, image_facts


def provenance_check(provenance, archive_sha):
    if not isinstance(provenance, dict) or type(provenance.get("schema")) is not int or provenance.get("schema") != 1:
        raise ValueError("provenance schema must be 1")
    if provenance.get("repository") != "sNyteXx/Polyseat-Unraid":
        raise ValueError("unexpected source repository")
    if not isinstance(provenance.get("commit"), str) or not COMMIT.fullmatch(provenance["commit"]):
        raise ValueError("provenance needs the exact full source commit")
    if provenance.get("archive_sha256") != archive_sha:
        raise ValueError("provenance is not bound to this runtime archive")
    if provenance.get("origin") == "github-actions":
        run_id, artifact_id = provenance.get("run_id"), provenance.get("artifact_id")
        if type(run_id) is not int or run_id < 1 or type(artifact_id) is not int or artifact_id < 1:
            raise ValueError("CI provenance requires numeric run and artifact IDs")
        expected = f"https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/{run_id}"
        if provenance.get("run_url") != expected:
            raise ValueError("run URL does not match the pinned repository/run")
    elif provenance.get("origin") == "local":
        if not re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", provenance.get("build_id", "")):
            raise ValueError("local provenance requires an explicit build identifier")
        if any(name in provenance for name in ("run_id", "run_url", "artifact_id")):
            raise ValueError("local artifacts must not impersonate CI artifacts")
    else:
        raise ValueError("origin must be github-actions or local")


def rehearsal_check(data, commit, archive_sha, controller_sha):
    evidence = strict_json(data)
    if not isinstance(evidence, dict):
        raise ValueError("rehearsal evidence must be an object")
    steps = evidence.get("steps")
    if (type(evidence.get("schema")) is not int or evidence.get("schema") != 1 or evidence.get("commit") != commit
            or evidence.get("archive_sha256") != archive_sha or evidence.get("controller_sha256") != controller_sha
            or evidence.get("platform") != "disposable-linux" or evidence.get("status") != "passed"
            or not isinstance(steps, list) or not all(isinstance(step, str) for step in steps)
            or len(steps) != len(set(steps)) or not REHEARSAL_STEPS.issubset(steps)):
        raise ValueError("rehearsal evidence is not a passed exact-artifact Linux run with all required steps")
    return evidence


def source_check(package_data, plan_data, commit, image_facts):
    """Compare *all* packaged source files and the plan to the named Git commit."""
    def git(*args):
        result = subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, timeout=15)
        return result.stdout
    base = "unraid/plugin/src/emhttp/"
    expected = git("ls-tree", "-r", "--name-only", commit, base).decode().splitlines()
    entries = PACKAGE.inspect_package(package_data, "canary")
    packaged = {member.name: content for member, content in entries if member.isreg() and member.name.startswith(PACKAGE.BASE + "/")}
    if set(packaged) != {PACKAGE.BASE + "/" + path[len(base):] for path in expected}:
        raise ValueError("controller package does not match the commit file set")
    for path in expected:
        wanted = git("show", f"{commit}:{path}")
        if path == base + "scripts/polyseatctl":
            wanted = PACKAGE.render_gate(wanted, "closed", "canary")
        if packaged[PACKAGE.BASE + "/" + path[len(base):]] != wanted:
            raise ValueError(f"controller source differs from named commit: {path}")
    if plan_data != git("show", f"{commit}:unraid/docs/production-canary-plan.md"):
        raise ValueError("canary plan differs from the named source commit")
    pins = dict(line.split("=", 1) for line in git("show", f"{commit}:unraid/build-pins.env").decode().splitlines()
                if line and not line.startswith("#") and "=" in line)
    if (pins.get("BUSYBOX_BINARY_SHA256") != image_facts["binary_sha256"]
            or pins.get("BUSYBOX_VERSION") != image_facts["busybox_version"]):
        raise ValueError("canary image differs from the BusyBox pins at the named commit")


def file_records(kit):
    records = []
    total = 0
    for path in sorted(kit.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink in canary kit")
        if path.is_dir():
            continue
        name = path.relative_to(kit).as_posix()
        if name in METADATA:
            continue
        if not safe_relative(name) or "approval" in path.name.lower() or path.name.endswith((".key", ".pem")):
            raise ValueError("unsafe or approval/private-key file in artifact kit")
        data = regular(path)
        total += len(data)
        if total > MAX_KIT_BYTES or len(records) >= 128:
            raise ValueError("kit size/count limit exceeded")
        records.append({"path": name, "sha256": sha(data), "size": len(data)})
    return records


def read_kit(kit):
    if not kit.is_dir() or kit.is_symlink() or kit.absolute() != kit.resolve():
        raise ValueError("kit must be a real directory without symlink ancestors")
    spec = strict_json(regular(kit / MANIFEST, 1024 * 1024))
    expected_keys = {"schema", "purpose", "source_commit", "created_at", "expires_at", "scope", "runtime", "controller",
                     "image", "plan_sha256", "provenance", "signer_fingerprint", "sealed", "files"}
    if (not isinstance(spec, dict) or set(spec) != expected_keys
            or type(spec.get("schema")) is not int or spec.get("schema") != 1):
        raise ValueError("unknown canary kit manifest schema or fields")
    if (spec["purpose"] != "polyseat-cpu-only-canary-artifacts" or spec["scope"] != SCOPES
            or canonical(spec["scope"]) != canonical(SCOPES)):
        raise ValueError("canary scope must not grant host approval, GPU or service changes")
    if type(spec["created_at"]) is not int or type(spec["expires_at"]) is not int:
        raise ValueError("artifact validity must use integer UTC timestamps")
    if not 1 <= spec["expires_at"] - spec["created_at"] <= MAX_TTL:
        raise ValueError("artifact validity exceeds seven days")
    now = int(time.time())
    if spec["created_at"] > now + 60 or spec["expires_at"] <= now:
        raise ValueError("artifact preparation is future-dated or expired")
    if type(spec["sealed"]) is not bool or spec["files"] != file_records(kit):
        raise ValueError("artifact file set/size/hash differs from the bound kit")
    if regular(kit / MANIFEST, 1024 * 1024) != canonical(spec):
        raise ValueError("kit manifest must be canonical JSON")
    runtime = spec["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"archive", "manifest", "sha256"}:
        raise ValueError("invalid runtime reference")
    for name in (runtime["archive"], runtime["manifest"], spec["controller"]):
        if not safe_relative(name) or "/" in name:
            raise ValueError("artifact references must be simple kit filenames")
    actual, image_facts = inspect_runtime(kit / runtime["archive"], kit / runtime["manifest"])
    if actual["archive_sha256"] != runtime["sha256"] or image_facts != spec["image"]:
        raise ValueError("runtime or image binding mismatch")
    PACKAGE.inspect_package(regular(kit / spec["controller"], 2 * 1024 * 1024), "canary")
    if sha(regular(kit / "PLAN.md", 2 * 1024 * 1024)) != spec["plan_sha256"]:
        raise ValueError("plan binding mismatch")
    provenance_check(spec["provenance"], runtime["sha256"])
    if spec["source_commit"] != spec["provenance"]["commit"]:
        raise ValueError("runtime/controller source commit mismatch")
    return spec


def prepare(args):
    if not args.acknowledge_artifacts_only:
        raise ValueError("--acknowledge-artifacts-only required; it is not production approval")
    if not 1 <= args.expires_in <= MAX_TTL:
        raise ValueError("validity must be between one second and seven days")
    kit = args.out.absolute()
    if kit.exists() or kit.is_symlink() or not kit.parent.is_dir() or kit.parent.resolve() != kit.parent:
        raise ValueError("output must be a new directory under an existing real parent")
    if kit.is_relative_to(ROOT.resolve()):
        raise ValueError("canary artifacts must be outside the repository")
    runtime, image_facts = inspect_runtime(args.archive, args.manifest)
    provenance = strict_json(regular(args.provenance, 1024 * 1024))
    provenance_check(provenance, runtime["archive_sha256"])
    package_data = regular(args.controller, 2 * 1024 * 1024)
    plan_data = regular(args.plan, 2 * 1024 * 1024)
    if not re.fullmatch(r"polyseat-unraid-[0-9.]+[0-9A-Za-z.]*-noarch-1\.txz", args.controller.name):
        raise ValueError("invalid controller package name")
    source_check(package_data, plan_data, provenance["commit"], image_facts)
    evidence = {}
    required = {"abi-report.json", "build-info.txt", "SBOM-runtime.cdx.json", "e2e-result.txt", "canary-rehearsal.json"}
    for path in sorted(args.evidence_dir.iterdir()):
        if path.suffix in (".json", ".txt", ".md") and path.name not in ("SHA256SUMS", "bundle.env"):
            if not safe_relative(path.name) or "approval" in path.name.lower():
                raise ValueError("invalid evidence filename")
            evidence[path.name] = regular(path, 8 * 1024 * 1024)
    if not required.issubset(evidence):
        raise ValueError("ABI, SBOM, controller E2E, exact-artifact canary rehearsal and build provenance are required")
    if strict_json(evidence["abi-report.json"]).get("ok") is not True:
        raise ValueError("ABI evidence is not green")
    if not evidence["e2e-result.txt"].startswith(b"PASS: controller end-to-end on real incusd"):
        raise ValueError("controller E2E evidence is missing or failed")
    sbom_hashes = strict_json(evidence["SBOM-runtime.cdx.json"]).get("metadata", {}).get("component", {}).get("hashes", [])
    if {"alg": "SHA-256", "content": runtime["archive_sha256"]} not in sbom_hashes:
        raise ValueError("runtime SBOM is not bound to the exact bundle")
    rehearsal_check(evidence["canary-rehearsal.json"], provenance["commit"], runtime["archive_sha256"], sha(package_data))
    kit.mkdir(mode=0o700)
    try:
        digest = runtime["archive_sha256"]
        archive_name, manifest_name = f"{digest}.tar.gz", f"{digest}.manifest.json"
        for name, data in ((archive_name, regular(args.archive)), (manifest_name, regular(args.manifest)),
                           (args.controller.name, package_data), ("PLAN.md", plan_data)):
            write_new(kit / name, data)
        (kit / "evidence").mkdir(mode=0o700)
        for name, data in evidence.items():
            write_new(kit / "evidence" / name, data)
        now = int(time.time())
        spec = {"schema": 1, "purpose": "polyseat-cpu-only-canary-artifacts", "source_commit": provenance["commit"],
                "created_at": now, "expires_at": now + args.expires_in, "scope": SCOPES,
                "runtime": {"archive": archive_name, "manifest": manifest_name, "sha256": digest},
                "controller": args.controller.name, "image": image_facts, "plan_sha256": sha(plan_data),
                "provenance": provenance, "signer_fingerprint": None, "sealed": False, "files": file_records(kit)}
        write_new(kit / MANIFEST, canonical(spec))
        read_kit(kit)
    except Exception:
        # Output is our newly created, external artifact directory only.
        shutil.rmtree(kit)
        raise
    print(f"PREPARED UNSIGNED: {kit}; no deployment or host approval")


def public_key(path):
    data = regular(path, 8192).decode("ascii").strip().split()
    if len(data) < 2 or data[0] not in ("ssh-ed25519", "sk-ssh-ed25519@openssh.com"):
        raise ValueError("trusted key must be one Ed25519 public key")
    raw = base64.b64decode(data[1], validate=True)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    check = subprocess.run(["ssh-keygen", "-lf", str(path)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=10)
    if check.returncode or fingerprint not in check.stdout:
        raise ValueError("trusted public key is malformed")
    return " ".join(data[:2]), fingerprint


def external_trust(kit, path, expected_fingerprint):
    if path.resolve().is_relative_to(kit.resolve()):
        raise ValueError("a public key shipped inside the kit cannot bootstrap its own trust")
    key, fingerprint = public_key(path)
    if fingerprint != expected_fingerprint:
        raise ValueError("external signer fingerprint mismatch")
    return key, fingerprint


def verify_signature(document, signature, key, namespace, identity, revoked=None):
    regular(signature, 16384)
    with tempfile.TemporaryDirectory(prefix="polyseat-canary-verify-") as tmp:
        allowed = Path(tmp) / "allowed_signers"
        write_new(allowed, f'{identity} namespaces="{namespace}" {key}\n'.encode())
        command = ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", identity,
                   "-n", namespace, "-s", str(signature)]
        if revoked is not None:
            regular(revoked, 1024 * 1024)
            command += ["-r", str(revoked)]
        result = subprocess.run(command, input=regular(document), capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError(f"signature or revocation check failed ({namespace})")


def render_preview(kit, spec, key):
    root = ET.Element("PLUGIN", {"name": "polyseat-unraid-canary-preview", "version": "1", "min": "7.2.0"})
    gate = ET.SubElement(root, "FILE", {"Run": "/bin/bash"})
    ET.SubElement(gate, "INLINE").text = ('printf "%s\\n" "BLOCKED: artifact preview only; no host approval or installer" >&2\nexit 2\n')
    ET.SubElement(root, "CHANGES").text = "Inert canary preview. Renaming to .plg still aborts before downloads or writes. No automatic activation."
    for name in (spec["controller"], spec["runtime"]["archive"], spec["runtime"]["manifest"], spec["runtime"]["manifest"] + ".sig"):
        entry = ET.SubElement(root, "FILE", {"Name": "/boot/config/plugins/polyseat-unraid/pkg/" + name})
        ET.SubElement(entry, "URL").text = "https://example.invalid/owner-reviewed-canary/" + name
        ET.SubElement(entry, "SHA256").text = sha(regular(kit / name))
    # The public key is documentary text, not an automatically installed trust file.
    ET.SubElement(root, "SIGNER_PREVIEW").text = key
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def checksum_text(kit):
    result = []
    for path in sorted(kit.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            result.append(f"{sha(regular(path))}  {path.relative_to(kit).as_posix()}\n")
    return "".join(result).encode()


def seal(args):
    if not args.acknowledge_artifacts_only:
        raise ValueError("explicit artifact signing acknowledgement required")
    kit = args.kit.absolute()
    spec = read_kit(kit)
    if spec["sealed"]:
        raise ValueError("kit is already sealed; create a new one instead of overwriting signatures")
    key, fingerprint = external_trust(kit, args.trusted_key, args.fingerprint)
    if args.signing_key.resolve().is_relative_to(ROOT.resolve()) or args.signing_key.resolve().is_relative_to(kit):
        raise ValueError("signing key must be outside the repository and kit")
    info = args.signing_key.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("signing key must be a regular file or public key selecting a separately held agent key")
    # Private key contents are never read by this program or copied into artifacts.
    if args.signing_key.suffix != ".pub" and info.st_mode & 0o077:
        raise ValueError("private signing key must be owner-private")
    def sign(name, namespace):
        target = kit / name
        if target.with_name(target.name + ".sig").exists():
            raise ValueError("refusing to overwrite a signature")
        result = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(args.signing_key),
                                 "-n", namespace, str(target)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("signing failed; use an unlocked agent or perform the ceremony interactively outside this tool")
    sign(spec["runtime"]["manifest"], "polyseat-unraid-bundle")
    verify_signature(kit / spec["runtime"]["manifest"], kit / (spec["runtime"]["manifest"] + ".sig"),
                     key, "polyseat-unraid-bundle", "polyseat-unraid-release", args.revoked_keys)
    sign(spec["controller"], PACKAGE_NAMESPACE)
    verify_signature(kit / spec["controller"], kit / (spec["controller"] + ".sig"),
                     key, PACKAGE_NAMESPACE, KIT_IDENTITY, args.revoked_keys)
    write_new(kit / "artifact-signer.pub", (key + "\n").encode())
    write_new(kit / "polyseat-unraid-canary.plg.disabled", render_preview(kit, spec, key))
    spec.update({"sealed": True, "signer_fingerprint": fingerprint, "files": file_records(kit)})
    temporary = kit / "canary-kit.pending"
    write_new(temporary, canonical(spec))
    os.replace(temporary, kit / MANIFEST)
    sign(MANIFEST, KIT_NAMESPACE)
    write_new(kit / "SHA256SUMS", checksum_text(kit))
    verify_kit(kit, args.trusted_key, args.fingerprint, args.revoked_keys, require_rehearsal=False)
    print(f"SEALED: {kit}; signature authenticates artifacts only, NOT a production run")


def verify_kit(kit, trusted_key, fingerprint, revoked=None, *, require_rehearsal=False):
    kit = kit.absolute()
    key, actual_fingerprint = external_trust(kit, trusted_key, fingerprint)
    # Authenticate metadata before parsing the potentially expensive bundle/image.
    verify_signature(kit / MANIFEST, kit / (MANIFEST + ".sig"), key, KIT_NAMESPACE, KIT_IDENTITY, revoked)
    spec = read_kit(kit)
    if spec["sealed"] is not True or spec["signer_fingerprint"] != actual_fingerprint:
        raise ValueError("kit signer binding mismatch")
    verify_signature(kit / spec["runtime"]["manifest"], kit / (spec["runtime"]["manifest"] + ".sig"),
                     key, "polyseat-unraid-bundle", "polyseat-unraid-release", revoked)
    verify_signature(kit / spec["controller"], kit / (spec["controller"] + ".sig"),
                     key, PACKAGE_NAMESPACE, KIT_IDENTITY, revoked)
    if regular(kit / "SHA256SUMS", 1024 * 1024) != checksum_text(kit):
        raise ValueError("complete SHA256SUMS mismatch")
    rehearsal = kit / "evidence/canary-rehearsal.json"
    if rehearsal.exists():
        evidence = rehearsal_check(regular(rehearsal, 1024 * 1024), spec["source_commit"],
                                   spec["runtime"]["sha256"], sha(regular(kit / spec["controller"])))
    elif require_rehearsal:
        raise ValueError("exact-artifact canary rehearsal still missing; not ready for an Unraid test")
    return {"integrity": "verified", "source_commit": spec["source_commit"],
            "archive_sha256": spec["runtime"]["sha256"], "controller_sha256": sha(regular(kit / spec["controller"])),
            "signer_fingerprint": actual_fingerprint, "canary_rehearsal": rehearsal.exists(),
            "ci_overrides": evidence.get("overrides", {}) if rehearsal.exists() else {},
            "ci_renderings": evidence.get("ci_renderings", []) if rehearsal.exists() else [],
            "unraid_test_approved": False}


def compare(args):
    left, left_image = inspect_runtime(args.left_archive, args.left_manifest)
    right, right_image = inspect_runtime(args.right_archive, args.right_manifest)
    # Names/version labels differ by design (ci/localdev); payload bytes and ABI
    # requirements never get normalized away. An ELF path difference is a difference.
    ignore = {"name", "version", "archive_sha256", "files"}
    contract_equal = {k: v for k, v in left.items() if k not in ignore} == {k: v for k, v in right.items() if k not in ignore}
    lfiles = {item["path"]: item for item in left["files"]}
    rfiles = {item["path"]: item for item in right["files"]}
    changed = [name for name in sorted(set(lfiles) | set(rfiles)) if lfiles.get(name) != rfiles.get(name)]
    result = {"schema": 1, "archive_byte_equal": left["archive_sha256"] == right["archive_sha256"],
              "payload_equal": not changed, "contract_equal": contract_equal, "changed_payload_files": changed,
              "left_archive_sha256": left["archive_sha256"], "right_archive_sha256": right["archive_sha256"],
              "labels": {"left": {k: left[k] for k in ("name", "version")},
                         "right": {k: right[k] for k in ("name", "version")}},
              "canary_image_equal": left_image == right_image,
              "qualification": "Exact payload/ABI comparison only; differing build labels are disclosed, not relabeled. Not an attestation."}
    if args.output:
        write_new(args.output, canonical(result))
    print(canonical(result).decode(), end="")
    return 0 if not changed and contract_equal else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    for flag in ("archive", "manifest", "controller", "plan", "provenance", "evidence-dir", "out"):
        prep.add_argument("--" + flag, required=True, type=Path)
    prep.add_argument("--expires-in", type=int, default=86400)
    prep.add_argument("--acknowledge-artifacts-only", action="store_true")
    for name in ("seal", "verify"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--kit", required=True, type=Path)
        cmd.add_argument("--trusted-key", required=True, type=Path)
        cmd.add_argument("--fingerprint", required=True)
        cmd.add_argument("--revoked-keys", type=Path)
        if name == "seal":
            cmd.add_argument("--signing-key", required=True, type=Path)
            cmd.add_argument("--acknowledge-artifacts-only", action="store_true")
        else:
            cmd.add_argument("--require-rehearsal", action="store_true")
    cmp = commands.add_parser("compare-runtime")
    for flag in ("left-archive", "left-manifest", "right-archive", "right-manifest"):
        cmp.add_argument("--" + flag, required=True, type=Path)
    cmp.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if Path("/etc/unraid-version").exists():
            raise ValueError("artifact preparation/verification runs off-host; never on Unraid")
        if args.command == "prepare":
            prepare(args)
        elif args.command == "seal":
            if (os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"
                    or os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"):
                raise ValueError("CI must not seal owner-trusted artifacts; disposable unit-test signing is a separate path")
            seal(args)
        elif args.command == "verify":
            print(canonical(verify_kit(args.kit, args.trusted_key, args.fingerprint, args.revoked_keys,
                                      require_rehearsal=args.require_rehearsal)).decode(), end="")
        else:
            return compare(args)
        return 0
    except (ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as error:
        parser.exit(2, f"CANARY ARTIFACT BLOCKED: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
