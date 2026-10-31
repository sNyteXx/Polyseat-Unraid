#!/usr/bin/env python3
"""Seat-image attachment for a sealed canary kit: prepare, seal, verify (off-host only).

The CPU canary kit (canary-artifacts.py) binds runtime bundle, controller and plan. The seat image is too large
for that kit, so it travels as this attachment, bound to the same source commit, runtime and controller and to
the exact-artifact desktop rehearsal from CI. The seat manifest is re-created here byte-for-byte as CI rehearsed it;
its signature (namespace polyseat-unraid-seat-image) is what `polyseatctl seat-image-stage` verifies on the host.
Nothing here approves, transfers or runs anything on a host.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEAT_NAMESPACE = "polyseat-unraid-seat-image"
ATTACH_NAMESPACE = "polyseat-unraid-seat-attachment-v1"
RELEASE_IDENTITY = "polyseat-unraid-release"
ATTACH_IDENTITY = "polyseat-unraid-canary-artifact"
STEPS = {"stage-activate", "seat-image-stage", "desktop-smoke", "cleanup", "host-state-unchanged"}
NAMES = ("polyseat-desktop.tar.xz", "seat.manifest.json", "seat-image.json", "desktop-canary-rehearsal.json")


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANIFEST = load("make-seat-manifest")


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def regular(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} must be a regular file")
    return path


def strict_json(path: Path, limit=4 * 1024 * 1024):
    if regular(path).stat().st_size > limit:
        raise ValueError(f"{path.name} too large")
    return json.loads(path.read_text(encoding="utf-8"),
                      object_pairs_hook=lambda pairs: dict(pairs) if len({k for k, _ in pairs}) == len(pairs)
                      else (_ for _ in ()).throw(ValueError("duplicate JSON key")))


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()


def check_rehearsal(rehearsal: dict, commit, runtime, controller, image_sha, manifest_sha):
    selftest = rehearsal.get("selftest") or {}
    if (rehearsal.get("schema") != 1 or rehearsal.get("status") != "passed"
            or rehearsal.get("platform") != "disposable-linux" or rehearsal.get("commit") != commit
            or rehearsal.get("archive_sha256") != runtime or rehearsal.get("controller_sha256") != controller
            or rehearsal.get("seat_image_sha256") != image_sha or rehearsal.get("seat_manifest_sha256") != manifest_sha
            or not STEPS.issubset(set(rehearsal.get("steps") or []))
            or any((selftest.get(phase) or {}).get("verdict") != "PASS" for phase in ("start", "restart"))):
        raise ValueError("desktop rehearsal is not a passed exact-artifact run for this commit, runtime, controller and image")


def prepare(args):
    kit = json.loads((args.kit / "canary-kit.json").read_text())
    commit, runtime = kit["source_commit"], kit["runtime"]["sha256"]
    controller = sha_file(regular(args.kit / kit["controller"]))
    image_sha = sha_file(regular(args.image))
    seat_json = strict_json(args.seat_json, 64 * 1024 * 1024)
    if seat_json.get("image_sha256") != image_sha:
        raise ValueError("seat-image.json does not describe this image")
    epoch = int(subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct", commit],
                               check=True, capture_output=True, text=True).stdout)
    manifest = MANIFEST.build(args.image, int(seat_json["unpacked_size_bytes"]), commit, ["cpu", "nvidia"], epoch)
    data = manifest_bytes(manifest)
    rehearsal = strict_json(args.rehearsal)
    check_rehearsal(rehearsal, commit, runtime, controller, image_sha, hashlib.sha256(data).hexdigest())
    if rehearsal.get("seat_manifest") != manifest:
        raise ValueError("re-created seat manifest differs from the rehearsed one")
    out = args.out.absolute()
    if out.exists() or out.is_relative_to(ROOT):
        raise ValueError("output must be a new directory outside the repository")
    out.mkdir(mode=0o700)
    shutil.copyfile(args.image, out / NAMES[0])
    (out / NAMES[1]).write_bytes(data)
    shutil.copyfile(args.seat_json, out / NAMES[2])
    shutil.copyfile(args.rehearsal, out / NAMES[3])
    for name in NAMES:
        os.chmod(out / name, 0o600)
    record = {"schema": 1, "source_commit": commit, "runtime_sha256": runtime, "controller_sha256": controller,
              "seat_image_sha256": image_sha, "seat_manifest_sha256": hashlib.sha256(data).hexdigest(),
              "files": {name: sha_file(out / name) for name in NAMES}, "host_approval": False}
    (out / "attachment.json").write_bytes(canonical(record))
    print(f"PREPARED UNSIGNED: {out}")


def run_sign(path: Path, key: Path, namespace: str):
    sig = Path(str(path) + ".sig")
    if sig.exists():
        raise ValueError("refusing to overwrite a signature")
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", namespace, str(path)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=30)


def verify_sig(path: Path, key: str, namespace: str, identity: str):
    with tempfile.TemporaryDirectory(prefix="polyseat-seat-verify-") as tmp:
        signers = Path(tmp) / "allowed_signers"
        signers.write_text(f'{identity} namespaces="{namespace}" {key}\n')
        with path.open("rb") as handle:
            result = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", identity, "-n", namespace,
                                     "-s", str(path) + ".sig"], stdin=handle, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(f"signature invalid: {path.name} ({namespace})")


def trusted(path: Path, fingerprint: str) -> str:
    key = " ".join(path.read_text().split()[:2])
    out = subprocess.run(["ssh-keygen", "-lf", str(path)], capture_output=True, text=True, check=True).stdout
    if fingerprint not in out.split():
        raise ValueError("trusted key fingerprint mismatch")
    return key


def verify(args) -> dict:
    key = trusted(args.trusted_key, args.fingerprint)
    d = args.dir.absolute()
    verify_sig(d / "attachment.json", key, ATTACH_NAMESPACE, ATTACH_IDENTITY)
    record = strict_json(d / "attachment.json")
    if (d / "attachment.json").read_bytes() != canonical(record) or record.get("host_approval") is not False:
        raise ValueError("attachment record malformed")
    for name in NAMES:
        if sha_file(regular(d / name)) != record["files"][name]:
            raise ValueError(f"{name} differs from the signed attachment record")
    verify_sig(d / NAMES[1], key, SEAT_NAMESPACE, RELEASE_IDENTITY)
    manifest = strict_json(d / NAMES[1])
    if manifest["image_sha256"] != record["seat_image_sha256"] or manifest["image_size"] != (d / NAMES[0]).stat().st_size:
        raise ValueError("seat manifest is not bound to the image")
    check_rehearsal(strict_json(d / NAMES[3]), record["source_commit"], record["runtime_sha256"],
                    record["controller_sha256"], record["seat_image_sha256"], record["seat_manifest_sha256"])
    return {"integrity": "verified", **{k: record[k] for k in ("source_commit", "runtime_sha256", "controller_sha256",
                                                               "seat_image_sha256", "seat_manifest_sha256")},
            "unraid_test_approved": False}


def seal(args):
    key = trusted(args.trusted_key, args.fingerprint)
    d = args.dir.absolute()
    if args.signing_key.resolve().is_relative_to(ROOT) or args.signing_key.resolve().is_relative_to(d):
        raise ValueError("signing key must be outside the repository and attachment")
    run_sign(d / NAMES[1], args.signing_key, SEAT_NAMESPACE)
    run_sign(d / "attachment.json", args.signing_key, ATTACH_NAMESPACE)
    (d / "artifact-signer.pub").write_text(key + "\n")
    names = [*NAMES, NAMES[1] + ".sig", "attachment.json", "attachment.json.sig", "artifact-signer.pub"]
    (d / "SHA256SUMS").write_text("".join(f"{sha_file(d / n)}  {n}\n" for n in names))
    print(json.dumps(verify(args), sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    for flag in ("kit", "image", "seat-json", "rehearsal", "out"):
        p.add_argument("--" + flag, required=True, type=Path)
    for name in ("seal", "verify"):
        c = sub.add_parser(name)
        c.add_argument("--dir", required=True, type=Path)
        c.add_argument("--trusted-key", required=True, type=Path)
        c.add_argument("--fingerprint", required=True)
        if name == "seal":
            c.add_argument("--signing-key", required=True, type=Path)
    args = parser.parse_args()
    try:
        if Path("/etc/unraid-version").exists():
            raise ValueError("off-host only; never on Unraid")
        if args.command == "prepare":
            prepare(args)
        elif args.command == "seal":
            if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
                raise ValueError("CI must not seal owner-trusted artifacts")
            seal(args)
        else:
            print(json.dumps(verify(args), sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        parser.exit(2, f"SEAT ATTACHMENT BLOCKED: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
