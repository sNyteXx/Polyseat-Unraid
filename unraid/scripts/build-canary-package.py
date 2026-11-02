#!/usr/bin/env python3
"""Explicit artifact-only canary packaging, separate from ordinary CI packaging.

The committed controller and the ordinary builder stay closed/test-host. This
command builds a temporary closed package, verifies its exact package layout,
and emits a deterministic gate=canary package outside the repository. Nothing is
installed, approved, published or executed from the generated package.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import lzma
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = "usr/local/emhttp/plugins/polyseat-unraid/scripts/polyseatctl"
BASE = "usr/local/emhttp/plugins/polyseat-unraid"


def require_gate(content: bytes, gate: str) -> None:
    # Count standalone source definitions, not legitimate env -i inheritance or
    # diagnostic literals containing the variable name later in the controller.
    assignments = re.findall(rb"^POLYSEAT_RELEASE_GATE=[^\r\n]*$", content, re.MULTILINE)
    expected = rb'POLYSEAT_RELEASE_GATE="' + re.escape(gate.encode()) + rb'"(?:[ \t]+#[^\r\n]*)?'
    if len(assignments) != 1 or not re.fullmatch(expected, assignments[0]):
        raise ValueError("controller release gate mismatch")


def render_gate(content: bytes, source_gate: str, target_gate: str) -> bytes:
    require_gate(content, source_gate)
    result = re.sub(rb'^POLYSEAT_RELEASE_GATE="' + re.escape(source_gate.encode()) + rb'"',
                    f'POLYSEAT_RELEASE_GATE="{target_gate}"'.encode(), content, count=1, flags=re.MULTILINE)
    require_gate(result, target_gate)
    return result


def inspect_package(data: bytes, gate: str) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("controller package too large")
    entries = []
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
        for member in archive:
            name = member.name
            if len(seen) >= 128 or name in seen:
                raise ValueError("duplicate or excessive package entries")
            seen.add(name)
            allowed_directory = name in ("usr", "usr/local", "usr/local/emhttp", "usr/local/emhttp/plugins", "install")
            allowed = ((allowed_directory and member.isdir()) or (name == "install/slack-desc" and member.isreg())
                       or (name == BASE and member.isdir()) or name.startswith(BASE + "/"))
            if not allowed or ".." in name.split("/") or not re.fullmatch(r"[A-Za-z0-9_./+-]+", name):
                raise ValueError("package path outside plugin ownership")
            if not (member.isdir() or member.isreg()) or member.mode not in (0o755, 0o644):
                raise ValueError("package contains special files or unsafe modes")
            if member.isdir() and member.mode != 0o755:
                raise ValueError("package directories must have normalized 0755 mode")
            if (member.uid, member.gid, member.mtime) != (0, 0, 0):
                raise ValueError("package metadata is not deterministic")
            total += member.size
            if member.size < 0 or total > 4 * 1024 * 1024:
                raise ValueError("package exceeds uncompressed budget")
            content = archive.extractfile(member).read() if member.isreg() else None
            if name == CONTROLLER:
                require_gate(content, gate)
            entries.append((member, content))
    if CONTROLLER not in seen or "install/slack-desc" not in seen:
        raise ValueError("controller package incomplete")
    return entries


def build(version: str, out: Path, *, root: Path = ROOT) -> Path:
    if not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}[0-9A-Za-z.]*", version):
        raise ValueError("invalid package version")
    if not out.is_dir() or out.is_symlink() or out.resolve() != out.absolute():
        raise ValueError("output must be an existing non-symlink directory")
    if out.resolve().is_relative_to(root.resolve()):
        raise ValueError("canary artifact output must be outside the repository")
    name = f"polyseat-unraid-{version}-noarch-1.txz"
    dest = out / name
    if dest.exists() or dest.is_symlink():
        raise ValueError("refusing to overwrite package")
    with tempfile.TemporaryDirectory(prefix="polyseat-canary-package-") as tmp:
        result = subprocess.run([str(root / "unraid/scripts/build-plugin-package.sh"), version, tmp, "closed"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError("closed controller package failed: " + result.stderr.strip())
        entries = inspect_package((Path(tmp) / name).read_bytes(), "closed")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for member, content in sorted(entries, key=lambda item: item[0].name):
            if member.name == CONTROLLER:
                content = render_gate(content, "closed", "canary")
                member.size = len(content)
            archive.addfile(member, io.BytesIO(content) if content is not None else None)
    data = lzma.compress(raw.getvalue(), format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64,
                         preset=9 | lzma.PRESET_EXTREME)
    inspect_package(data, "canary")
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--acknowledge-artifacts-only", action="store_true")
    args = parser.parse_args()
    try:
        if not args.acknowledge_artifacts_only:
            raise ValueError("explicit --acknowledge-artifacts-only required; this is NOT host approval")
        if Path("/etc/unraid-version").exists():
            raise ValueError("never build a canary package on Unraid")
        result = build(args.version, args.out)
        print(f"{hashlib.sha256(result.read_bytes()).hexdigest()}  {result}")
        return 0
    except (ValueError, OSError, tarfile.TarError, lzma.LZMAError, subprocess.SubprocessError) as error:
        parser.exit(2, f"CANARY PACKAGE BLOCKED: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
