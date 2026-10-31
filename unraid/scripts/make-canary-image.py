#!/usr/bin/env python3
"""Build a deterministic disk-only BusyBox Incus image; never starts a process.

The binary is an explicitly pinned, static x86_64 build input. Its package/source
provenance is supplied by the build job, not guessed from PATH. The result belongs
at payload/share/canary/busybox.tar.gz before make-runtime-bundle.py signs its hash.
This program neither contacts a host nor creates a device, network, key or approval.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import tarfile
from urllib.parse import urlparse


INIT = b"#!/bin/busybox sh\nexec /bin/busybox init\n"
INITTAB = b"::sysinit:/bin/busybox true\n::respawn:/bin/busybox sleep 3600\n"
# Incus treats creation_date == 0 as missing. Keep a fixed, positive Unix second
# for image metadata; tar and gzip timestamps remain normalized to zero.
IMAGE_CREATION_DATE = 1
DIRECTORIES = ("rootfs", "rootfs/bin", "rootfs/dev", "rootfs/etc", "rootfs/proc",
               "rootfs/root", "rootfs/sbin", "rootfs/sys", "rootfs/tmp")
LINKS = {"rootfs/bin/sh": "busybox"}
FIXED_FILES = {"rootfs/sbin/init": (INIT, 0o755),
               "rootfs/etc/inittab": (INITTAB, 0o644)}


def static_busybox(data: bytes, expected_hash: str, version: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("binary digest must be lowercase SHA-256")
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("BusyBox binary SHA-256 mismatch")
    if not 65536 <= len(data) <= 8 * 1024 * 1024:
        raise ValueError("BusyBox binary outside the small canary input budget")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("version must be the upstream BusyBox x.y.z version")
    if data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("only little-endian ELF64 is supported")
    hdr = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    if hdr[1] not in (2, 3) or hdr[2] != 62 or hdr[3] != 1 or hdr[8] != 64:
        raise ValueError("BusyBox must be an x86_64 executable")
    offset, entry_size, count = hdr[5], hdr[9], hdr[10]
    if entry_size != 56 or not 1 <= count <= 64 or offset < 64 or offset + count * 56 > len(data):
        raise ValueError("invalid ELF program header table")
    has_load = False
    for idx in range(count):
        ph = struct.unpack_from("<IIQQQQQQ", data, offset + idx * 56)
        if ph[0] in (2, 3):
            raise ValueError("BusyBox must be static: PT_DYNAMIC/PT_INTERP are forbidden")
        if ph[2] + ph[5] > len(data):
            raise ValueError("ELF segment outside the binary")
        has_load |= ph[0] == 1
    if not has_load:
        raise ValueError("ELF has no loadable segment")
    banners = set(re.findall(rb"BusyBox v([0-9]+\.[0-9]+\.[0-9]+)(?:[ (\x00])", data))
    if banners != {version.encode("ascii")}:
        raise ValueError("BusyBox version does not match the embedded version banner")


def build_image(binary: bytes, *, binary_sha256: str, version: str) -> bytes:
    static_busybox(binary, binary_sha256, version)
    metadata = (f"architecture: x86_64\ncreation_date: {IMAGE_CREATION_DATE}\nproperties:\n"
                "  os: BusyBox\n  description: Polyseat CPU-only canary (no network or devices)\n"
                f"  release: '{version}'\n").encode("ascii")
    files = {**FIXED_FILES, "metadata.yaml": (metadata, 0o644),
             "rootfs/bin/busybox": (binary, 0o755)}
    out = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for name in sorted(set(DIRECTORIES) | set(LINKS) | set(files)):
                info = tarfile.TarInfo(name)
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                if name in DIRECTORIES:
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o1777 if name == "rootfs/tmp" else 0o755
                    archive.addfile(info)
                elif name in LINKS:
                    info.type = tarfile.SYMTYPE
                    info.mode = 0o777
                    info.linkname = LINKS[name]
                    archive.addfile(info)
                else:
                    content, info.mode = files[name]
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
    return out.getvalue()


def validate_image(image: bytes, *, binary_sha256: str | None = None,
                   version: str | None = None) -> dict:
    """Inspect without extracting. Accept exactly the image format built above."""
    if len(image) > 8 * 1024 * 1024:
        raise ValueError("canary image compressed size limit exceeded")
    # Bound decompression before tar.getmembers(): an 8 MiB compressed input can
    # otherwise force allocation/scanning of gigabytes before member-size checks.
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(image), mode="rb") as compressed:
            raw = compressed.read(10 * 1024 * 1024 + 1)
    except (OSError, EOFError) as error:
        raise ValueError("invalid or trailing canary gzip content") from error
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("canary image decompressed size limit exceeded")
    expected_names = set(DIRECTORIES) | set(LINKS) | set(FIXED_FILES) | {"metadata.yaml", "rootfs/bin/busybox"}
    blobs = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != len(expected_names) or {m.name for m in members} != expected_names:
            raise ValueError("canary image file allowlist mismatch (or duplicate entries)")
        if sum(m.size for m in members) > 9 * 1024 * 1024:
            raise ValueError("canary image uncompressed size limit exceeded")
        for member in members:
            name = member.name
            if (member.uid, member.gid, member.mtime, member.uname, member.gname) != (0, 0, 0, "", ""):
                raise ValueError("canary image metadata is not normalized")
            if name in DIRECTORIES:
                expected_mode = 0o1777 if name == "rootfs/tmp" else 0o755
                if not member.isdir() or member.mode != expected_mode or member.size:
                    raise ValueError("invalid image directory")
            elif name in LINKS:
                if not member.issym() or member.linkname != LINKS[name] or member.mode != 0o777 or member.size:
                    raise ValueError("unsafe image link")
            else:
                if not member.isreg() or member.size > 8 * 1024 * 1024:
                    raise ValueError("non-regular or oversized image payload")
                blobs[name] = archive.extractfile(member).read()
                expected_mode = 0o755 if name in ("rootfs/bin/busybox", "rootfs/sbin/init") else 0o644
                if member.mode != expected_mode:
                    raise ValueError("unexpected image file mode")
                if name in FIXED_FILES and blobs[name] != FIXED_FILES[name][0]:
                    raise ValueError("canary init differs from the fixed no-network program")
    if blobs["metadata.yaml"].splitlines().count(f"creation_date: {IMAGE_CREATION_DATE}".encode("ascii")) != 1:
        raise ValueError("canary image requires the fixed positive creation_date")
    binary = blobs["rootfs/bin/busybox"]
    actual_sha = hashlib.sha256(binary).hexdigest()
    banners = set(re.findall(rb"BusyBox v([0-9]+\.[0-9]+\.[0-9]+)(?:[ (\x00])", binary))
    if len(banners) != 1:
        raise ValueError("invalid BusyBox version banner")
    actual_version = banners.pop().decode("ascii")
    static_busybox(binary, binary_sha256 or actual_sha, version or actual_version)
    # A byte comparison also rejects extra tar/gzip trailers, noncanonical metadata,
    # unexpected links, and arbitrary YAML keys without a YAML runtime dependency.
    if image != build_image(binary, binary_sha256=actual_sha, version=actual_version):
        raise ValueError("image is not the canonical disk-only canary image")
    return {"image_sha256": hashlib.sha256(image).hexdigest(), "binary_sha256": actual_sha,
            "busybox_version": actual_version, "source_date_epoch": 0,
            "image_creation_date": IMAGE_CREATION_DATE,
            "files": sorted(expected_names)}


def write_new(path: Path, data: bytes) -> None:
    if not path.parent.is_dir() or path.parent.resolve() != path.parent.absolute():
        raise ValueError("output parent must be an existing non-symlink directory")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--source-sha256", default=None,
                        help="SHA-256 of the verified source package (.deb), if supplied")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    args = parser.parse_args()
    try:
        if Path("/etc/unraid-version").exists():
            raise ValueError("image creation is forbidden on Unraid")
        if not args.binary.is_file() or args.binary.is_symlink() or not stat.S_ISREG(args.binary.stat().st_mode):
            raise ValueError("input binary must be a regular, non-symlink file")
        source = urlparse(args.source_uri)
        if source.scheme != "https" or not source.netloc or source.username or source.password or source.fragment:
            raise ValueError("source URI must be a public HTTPS provenance URL")
        if not re.fullmatch(r"[A-Za-z0-9:_.=+@/-]{1,200}", args.source_id):
            raise ValueError("unsafe source identifier")
        if args.source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", args.source_sha256):
            raise ValueError("source package digest must be lowercase SHA-256")
        if args.out.exists() or args.out.is_symlink() or args.provenance.exists() or args.provenance.is_symlink():
            raise ValueError("refusing to overwrite image or provenance")
        if args.out.absolute() == args.provenance.absolute():
            raise ValueError("image and provenance destinations must differ")
        image = build_image(args.binary.read_bytes(), binary_sha256=args.binary_sha256, version=args.version)
        result = validate_image(image, binary_sha256=args.binary_sha256, version=args.version)
        result.update({"schema": 1, "source_id": args.source_id, "source_uri": args.source_uri,
                       "payload_path": "payload/share/canary/busybox.tar.gz",
                       "limitations": "CPU-only image input; not Unraid, GPU, network or production approval"})
        if args.source_sha256 is not None:
            result["source_sha256"] = args.source_sha256
        write_new(args.out, image)
        write_new(args.provenance, (json.dumps(result, sort_keys=True, indent=2) + "\n").encode())
        print(f"{result['image_sha256']}  {args.out}")
        return 0
    except (ValueError, OSError, tarfile.TarError, struct.error) as error:
        parser.exit(2, f"CANARY IMAGE BLOCKED: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
