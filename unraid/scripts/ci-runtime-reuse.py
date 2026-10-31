#!/usr/bin/env python3
"""Snapshot, seal and verify a reusable pinned-runtime build for GitHub CI (never used on a host).

A full build run takes a snapshot right after the runtime build (before any rehearsal writes evidence), and only
after the whole job is green seals it with the producer run and its uploaded proof artifact, then saves it to the
Actions cache under the content-addressed key from ci-runtime-key.py. A later run may reuse it ONLY if `verify`
passes; any failure or doubt means a complete rebuild:
  - key and key inputs equal the ones computed now (same recipe, same runner image),
  - every snapshot file (and symlink) equals the sealed list, nothing missing or extra,
  - the proof's own SHA256SUMS hold, the bundle is named by its hash, the manifest names the same archive,
  - the producer run exists in THIS repository, ran incus-feasibility.yml at the recorded commit/attempt and
    concluded success, and its proof artifact still exists with the recorded digest and is not expired.
Trust boundary (documented, not hidden): cache entries for this branch can only be written by workflow runs on this
branch, i.e. by code that already has write access. The checks catch corruption, stale or failed producers, recipe
or runner drift and expired provenance; they are not a defence against a malicious committer. Every Tower kit is
still qualified from the candidate run's own GitHub artifacts (digest-checked) and its own rehearsals.

  snapshot --proof DIR --stage DIR --out DIR
  seal     --snapshot DIR --key-json FILE --repository R --run-id N --run-attempt N --head-sha SHA
           --artifact-id N --artifact-digest sha256:...
  verify   --snapshot DIR --key-json FILE --repository R [--api-fixture FILE]   (exit 0 reusable, 3 not)
  restore  --snapshot DIR --proof-out DIR --stage-out DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

SCHEMA = 1
META = "runtime-reuse.json"
WORKFLOW = ".github/workflows/incus-feasibility.yml"
ARTIFACT = "incus-build-proof-NOT-FOR-UNRAID"
STAGE_PARTS = ("go-bin", "prefix/lib", "prefix/tools", "incus/test/deps/import-busybox")
SHA = re.compile(r"[0-9a-f]{64}")


class NotReusable(Exception):
    pass


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def listing(root: Path) -> dict:
    """path -> sha256 for files, 'symlink:<target>' for symlinks that stay inside root."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == META:
            continue
        if path.is_symlink():
            target = os.readlink(path)
            resolved = (path.parent / target).resolve()
            if os.path.isabs(target) or not resolved.is_relative_to(root.resolve()):
                raise NotReusable(f"symlink leaves the snapshot: {rel} -> {target}")
            out[rel] = "symlink:" + target
        elif path.is_file():
            out[rel] = sha_file(path)
        elif not path.is_dir():
            raise NotReusable(f"unexpected file type: {rel}")
    return out


def copy_tree(src: Path, dst: Path):
    shutil.copytree(src, dst, symlinks=True)


def snapshot(args):
    out = args.out
    if out.exists():
        raise SystemExit("snapshot output exists")
    out.mkdir(parents=True)
    copy_tree(args.proof, out / "proof")
    for part in STAGE_PARTS:
        src = args.stage / part
        dst = out / "stage" / part
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            copy_tree(src, dst)
        else:
            shutil.copy2(src, dst)
    print(out)


def seal(args):
    key = json.loads(args.key_json.read_text())
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.artifact_digest):
        raise SystemExit("artifact digest must be sha256:<hex>")
    bundle = (args.snapshot / "proof/bundle.env").read_text().split("BUNDLE_SHA256=", 1)[1].split()[0]
    meta = {"schema": SCHEMA, "key": key["key"], "inputs": key["inputs"], "bundle_sha256": bundle,
            "producer": {"repository": args.repository, "run_id": args.run_id, "run_attempt": args.run_attempt,
                         "head_sha": args.head_sha, "workflow": WORKFLOW, "artifact": ARTIFACT,
                         "artifact_id": args.artifact_id, "artifact_digest": args.artifact_digest},
            "files": listing(args.snapshot)}
    (args.snapshot / META).write_text(json.dumps(meta, sort_keys=True, indent=1) + "\n")
    print(json.dumps({"sealed": True, "key": meta["key"], "bundle_sha256": bundle, "files": len(meta["files"])}))


def api_get(path: str, fixture: dict | None):
    if fixture is not None:
        if path not in fixture:
            raise NotReusable(f"API {path}: not found")
        return fixture[path]
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(base + path, headers={"Accept": "application/vnd.github+json",
                                                           "X-GitHub-Api-Version": "2022-11-28",
                                                           "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception as error:  # any lookup problem means: rebuild
        raise NotReusable(f"API {path}: {error}") from error


def check(args) -> dict:
    snap = args.snapshot
    meta_path = snap / META
    if not meta_path.is_file() or meta_path.is_symlink():
        raise NotReusable("no sealed reuse record (producer job did not finish green)")
    meta = json.loads(meta_path.read_text())
    now = json.loads(args.key_json.read_text())
    if meta.get("schema") != SCHEMA or meta.get("key") != now["key"] or meta.get("inputs") != now["inputs"]:
        raise NotReusable("key or key inputs differ from the ones computed now")
    if listing(snap) != meta.get("files"):
        raise NotReusable("snapshot content differs from the sealed file list")
    proof = snap / "proof"
    for line in (proof / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(None, 1)
        name = name.lstrip("*")
        target = proof / name
        if not target.is_file() or target.is_symlink() or sha_file(target) != digest:
            raise NotReusable(f"proof SHA256SUMS mismatch: {name}")
    bundle = meta["bundle_sha256"]
    archive = proof / "bundle" / f"{bundle}.tar.gz"
    manifest = proof / "bundle" / f"{bundle}.manifest.json"
    if (not SHA.fullmatch(bundle) or f"BUNDLE_SHA256={bundle}" not in (proof / "bundle.env").read_text()
            or sha_file(archive) != bundle or json.loads(manifest.read_text()).get("archive_sha256") != bundle):
        raise NotReusable("bundle identity mismatch")
    producer = meta["producer"]
    if producer.get("repository") != args.repository or producer.get("workflow") != WORKFLOW:
        raise NotReusable("producer is not this repository's pinned build workflow")
    fixture = json.loads(args.api_fixture.read_text()) if args.api_fixture else None
    run = api_get(f"/repos/{args.repository}/actions/runs/{producer['run_id']}", fixture)
    if (run.get("repository", {}).get("full_name") != args.repository or run.get("path") != WORKFLOW
            or run.get("head_sha") != producer["head_sha"] or run.get("run_attempt") != producer["run_attempt"]
            or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise NotReusable("producer run is not a completed, successful run of the recorded commit/attempt")
    art = api_get(f"/repos/{args.repository}/actions/artifacts/{producer['artifact_id']}", fixture)
    if (art.get("name") != ARTIFACT or art.get("expired") is not False
            or art.get("workflow_run", {}).get("id") != producer["run_id"] or art.get("digest") != producer["artifact_digest"]):
        raise NotReusable("producer proof artifact missing, expired or with another digest")
    return {"reusable": True, "key": meta["key"], "bundle_sha256": bundle, "producer": producer}


def verify(args):
    try:
        verdict = check(args)
    except (NotReusable, OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
        print(json.dumps({"reusable": False, "reason": str(error)}))
        return 3
    print(json.dumps(verdict, sort_keys=True))
    return 0


def restore(args):
    for dst in (args.proof_out, args.stage_out):
        if dst.exists() and any(dst.iterdir()):
            raise SystemExit(f"refusing to restore into a non-empty {dst}")
    copy_tree(args.snapshot / "proof", args.proof_out)
    args.stage_out.mkdir(parents=True, exist_ok=True)
    for part in STAGE_PARTS:
        src = args.snapshot / "stage" / part
        dst = args.stage_out / part
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy_tree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
    print(args.proof_out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot")
    for flag in ("proof", "stage", "out"):
        s.add_argument("--" + flag, required=True, type=Path)
    s = sub.add_parser("seal")
    s.add_argument("--snapshot", required=True, type=Path)
    s.add_argument("--key-json", required=True, type=Path)
    s.add_argument("--repository", required=True)
    for flag in ("run-id", "run-attempt", "artifact-id"):
        s.add_argument("--" + flag, required=True, type=int)
    s.add_argument("--head-sha", required=True)
    s.add_argument("--artifact-digest", required=True)
    s = sub.add_parser("verify")
    s.add_argument("--snapshot", required=True, type=Path)
    s.add_argument("--key-json", required=True, type=Path)
    s.add_argument("--repository", required=True)
    s.add_argument("--api-fixture", type=Path)
    s = sub.add_parser("restore")
    for flag in ("snapshot", "proof-out", "stage-out"):
        s.add_argument("--" + flag, required=True, type=Path)
    args = parser.parse_args()
    if Path("/etc/unraid-version").exists():
        parser.exit(2, "never on Unraid\n")
    return {"snapshot": snapshot, "seal": seal, "verify": verify, "restore": restore}[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
