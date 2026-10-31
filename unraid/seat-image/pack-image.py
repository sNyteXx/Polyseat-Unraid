#!/usr/bin/env python3
"""Turn a flattened `docker export` stream into an Incus unified image tarball (metadata.yaml + rootfs/).

Deterministic by construction: members keep the export's (sorted) order, mtimes are clamped to SOURCE_DATE_EPOCH,
owners are numeric, only SCHILY.xattr pax records survive, xz settings (fixed block size) are fixed. Docker's own mounts (/.dockerenv,
hosts, hostname, resolv.conf) and device nodes are dropped. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

DROP = {".dockerenv", "etc/hostname", "etc/hosts", "etc/resolv.conf"}
SPECIAL = (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE)


class Sha256Writer:
    """File-like sink that hashes and counts everything written through it."""

    def __init__(self, raw):
        self.raw, self.digest, self.size = raw, hashlib.sha256(), 0

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        self.size += len(data)
        return self.raw.write(data)

    def flush(self) -> None:
        self.raw.flush()


def metadata_yaml(epoch: int, archive_date: str, base: str, sunshine: str) -> bytes:
    release = "archive-" + archive_date.replace("/", "-")
    lines = [
        "architecture: x86_64",
        f"creation_date: {epoch}",
        "properties:",
        "  description: Polyseat desktop seat (Arch Linux Archive snapshot, sway, Sunshine)",
        "  os: Archlinux",
        f"  release: {release}",
        "  variant: polyseat-desktop",
        f"  serial: {archive_date.replace('/', '')}",
        f"  requirements.base: {base}",
        f"  sunshine: {sunshine}",
        "",
    ]
    return "\n".join(lines).encode()


def add_bytes(out: tarfile.TarFile, name: str, data: bytes, epoch: int, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), epoch, mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    out.addfile(info, io.BytesIO(data))


# xz with fixed 32 MiB blocks: multi-threaded, yet the bytes do not depend on the thread count.
XZ = ["xz", "-c", "-6", "-T0", "--block-size=33554432", "--check=crc64"]


def pack(export: Path, image: Path, epoch: int, archive_date: str, base: str, sunshine: str) -> dict:
    """Streams: the export (a path, or "-" for stdin) is read once and the normalised tar goes straight into xz.
    Only the final image touches the disk; a rootfs with Steam's 32 bit userspace is several GB unpacked."""
    stats = {"files": 0, "dirs": 0, "symlinks": 0, "hardlinks": 0, "unpacked_bytes": 0, "dropped": []}
    digest = hashlib.sha256()
    src = sys.stdin.buffer if str(export) == "-" else export.open("rb")
    raw = image.open("wb")
    proc = subprocess.Popen(XZ, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def drain():
        while chunk := proc.stdout.read(1 << 20):
            digest.update(chunk)
            raw.write(chunk)

    reader = threading.Thread(target=drain)
    reader.start()
    ok = False
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as out, \
                tarfile.open(fileobj=src, mode="r|") as tin:
            add_bytes(out, "metadata.yaml", metadata_yaml(epoch, archive_date, base, sunshine), epoch)
            root = tarfile.TarInfo("rootfs")
            root.type, root.mode, root.mtime = tarfile.DIRTYPE, 0o755, epoch
            out.addfile(root)
            for member in tin:
                path = member.name.removeprefix("./").rstrip("/")
                if not path or path in DROP or member.type in SPECIAL:
                    stats["dropped"].append(path or ".")
                    continue
                member.name = "rootfs/" + path
                if member.islnk():
                    member.linkname = "rootfs/" + member.linkname.removeprefix("./")
                member.mtime = min(int(member.mtime), epoch)
                member.uname = member.gname = ""
                member.pax_headers = {k: v for k, v in member.pax_headers.items() if k.startswith("SCHILY.xattr.")}
                if member.isreg():
                    stats["files"] += 1
                    stats["unpacked_bytes"] += member.size
                    out.addfile(member, tin.extractfile(member))
                else:
                    stats["dirs" if member.isdir() else "symlinks" if member.issym() else "hardlinks"] += 1
                    out.addfile(member)
        ok = True
    finally:
        proc.stdin.close()
        reader.join()
        rc = proc.wait()
        raw.close()
        if src is not sys.stdin.buffer:
            src.close()
        if not ok or rc != 0:
            image.unlink(missing_ok=True)
    if rc != 0:
        sys.exit("xz failed")
    return {"sha256": digest.hexdigest(), "size_bytes": image.stat().st_size, **stats}


def parse_pacman(text: str) -> list[dict]:
    packages = []
    for line in text.splitlines():
        name, _, version = line.partition(" ")
        if name and version:
            packages.append({"name": name, "version": version})
    return packages


def cyclonedx(packages: list[dict], epoch: int) -> dict:
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {"type": "operating-system", "name": "polyseat-desktop"}},
        "components": [{"type": "library", "name": p["name"], "version": p["version"],
                        "purl": f"pkg:alpm/arch/{p['name']}@{p['version']}?arch=x86_64"} for p in packages],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True, type=Path, help="docker export tar, or - for stdin")
    ap.add_argument("--pacman-q", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--epoch", required=True, type=int)
    ap.add_argument("--archive-date", required=True)
    ap.add_argument("--base-image", required=True)
    ap.add_argument("--sunshine-version", required=True)
    ap.add_argument("--sunshine-url", required=True)
    ap.add_argument("--sunshine-sha256", required=True)
    ap.add_argument("--build-commit", default="unknown")
    args = ap.parse_args()
    if not re.fullmatch(r"20\d\d/\d\d/\d\d", args.archive_date) or args.epoch <= 0:
        sys.exit("invalid archive date or epoch")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    image = args.out_dir / "polyseat-desktop.tar.xz"
    result = pack(args.export, image, args.epoch, args.archive_date, args.base_image, args.sunshine_version)
    pacman_text = args.pacman_q.read_text()
    packages = parse_pacman(pacman_text)
    info = {
        "schema": 1,
        "image_file": image.name,
        "image_alias": "polyseat-desktop",
        "image_sha256": result["sha256"],
        "image_size_bytes": result["size_bytes"],
        "unpacked_size_bytes": result["unpacked_bytes"],
        "file_counts": {k: result[k] for k in ("files", "dirs", "symlinks", "hardlinks")},
        "dropped_from_export": sorted(set(result["dropped"])),
        "creation_date": args.epoch,
        "base_image": args.base_image,
        "archive_date": args.archive_date,
        "sunshine": {"version": args.sunshine_version, "url": args.sunshine_url, "sha256": args.sunshine_sha256},
        "build_commit": args.build_commit,
        "package_count": len(packages),
        "pacman_q": pacman_text.splitlines(),
    }
    (args.out_dir / "seat-image.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n")
    (args.out_dir / "polyseat-desktop.cdx.json").write_text(
        json.dumps(cyclonedx(packages, args.epoch), indent=2, sort_keys=True) + "\n")
    (args.out_dir / "polyseat-desktop.tar.xz.sha256").write_text(f"{result['sha256']}  {image.name}\n")
    print(f"{image} sha256={result['sha256']} compressed={result['size_bytes']} unpacked={result['unpacked_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
