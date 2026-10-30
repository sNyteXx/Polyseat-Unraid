"""Deterministic runtime-bundle builder (archive + external manifest).

Builder only: reads a payload directory and writes ``<sha256>.tar.gz`` and
``<sha256>.manifest.json``. Output is byte-reproducible for identical input
and ``source_date_epoch``. It never installs or touches a host. The archive
contains regular files only, owned by 0:0, with modes 0644/0755; the manifest
stays outside the archive so it can be signed independently.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import stat
import tarfile
from pathlib import Path

from .archive import MAX_BYTES, MAX_FILES, RejectedArchive, verify

SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SAFE_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,40}$")
SAFE_SONAME = re.compile(r"^[A-Za-z0-9_.+-]+$")
LSM_NAMES = ("apparmor", "selinux")


class BundleError(ValueError):
    """Payload cannot be turned into a safe bundle."""


def _entries(payload: Path) -> list[tuple[str, Path, int]]:
    if payload.is_symlink() or not payload.is_dir():
        raise BundleError("payload must be a real directory")
    entries = []
    for path in sorted(payload.rglob("*")):
        relative = path.relative_to(payload).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise BundleError(f"only regular files are allowed: {relative}")
        entries.append((f"payload/{relative}", path, 0o755 if mode & 0o111 else 0o644))
    if not entries or len(entries) > MAX_FILES:
        raise BundleError("payload is empty or has too many files")
    return entries


def build(payload: Path, out_dir: Path, *, name: str, version: str,
          incus_version: str, min_unraid: str, min_glibc: str,
          needed_libs: list[str], min_kernel: str = "6.12",
          built_without: tuple[str, ...] = ("apparmor", "selinux"),
          source_date_epoch: int = 0) -> tuple[Path, Path]:
    if not SAFE_NAME.fullmatch(name) or not SAFE_VERSION.fullmatch(version):
        raise BundleError("unsafe bundle name or version")
    for label, value in (("incus_version", incus_version), ("min_unraid", min_unraid),
                         ("min_glibc", min_glibc), ("min_kernel", min_kernel)):
        if not SAFE_VERSION.fullmatch(value):
            raise BundleError(f"unsafe {label}")
    if any(not SAFE_SONAME.fullmatch(lib) for lib in needed_libs):
        raise BundleError("unsafe needed library name")
    if any(item not in LSM_NAMES for item in built_without) or len(set(built_without)) != len(built_without):
        raise BundleError("built_without may list apparmor and selinux only, once each")
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise BundleError("output directory must exist and not be a symlink")

    files = []
    total = 0
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0,
                       compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for member_name, path, mode in _entries(payload):
                data = path.read_bytes()
                total += len(data)
                if total > MAX_BYTES:
                    raise BundleError("payload exceeds the allowed size")
                info = tarfile.TarInfo(member_name)
                info.size = len(data)
                info.mode = mode
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = source_date_epoch
                tar.addfile(info, io.BytesIO(data))
                files.append({"path": member_name, "sha256": hashlib.sha256(data).hexdigest(),
                              "size": len(data), "mode": mode})
    archive_bytes = buffer.getvalue()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    manifest = {
        "schema": 1,
        "name": name,
        "version": version,
        "incus_version": incus_version,
        "min_unraid": min_unraid,
        "min_glibc": min_glibc,
        "min_kernel": min_kernel,
        "built_without": sorted(built_without),
        "needed_libs": sorted(set(needed_libs)),
        "archive_sha256": digest,
        "source_date_epoch": source_date_epoch,
        "files": files,
    }
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as check:
            verify(check, manifest)
    except (tarfile.TarError, RejectedArchive) as exc:
        raise BundleError("built archive failed its own verification") from exc

    archive_path = out_dir / f"{digest}.tar.gz"
    manifest_path = out_dir / f"{digest}.manifest.json"
    for path in (archive_path, manifest_path):
        if path.exists() or path.is_symlink():
            raise BundleError(f"refusing to overwrite {path.name}")
    archive_path.write_bytes(archive_bytes)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return archive_path, manifest_path
