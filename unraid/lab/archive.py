"""Read-only archive verifier; NEVER extracts or executes archive entries.

Strict checks are intended for a future reviewed manifest format. The build
proof is NOT a runtime package, even if an archive passes these checks.
"""

from __future__ import annotations

import hashlib
import re
import tarfile
from pathlib import PurePosixPath


MAX_FILES = 128
MAX_BYTES = 512 * 1024 * 1024
SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./+-]*$")


class RejectedArchive(ValueError):
    """Archive is incomplete, malicious, unbounded or not as declared."""


def _safe_path(name: str) -> bool:
    if not isinstance(name, str) or not name:
        return False
    p = PurePosixPath(name)
    return (not name.startswith("/") and "//" not in name
            and all(part and not part.startswith(".") for part in name.split("/"))
            and bool(SAFE_PATH.fullmatch(name)) and p.parts[0] == "payload")


def verify(tar: tarfile.TarFile, manifest: dict) -> None:
    """Verify sha256, type, path, size and mode before any possible extraction.

    Manifest shape: {'files': [{'path': 'payload/bin/...', 'sha256': ...,
                                'size': 123, 'mode': 0o755}, ...]}
    Archives with any extra entries, including directories, are rejected.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise RejectedArchive("invalid manifest")
    entries = manifest["files"]
    if not entries or len(entries) > MAX_FILES:
        raise RejectedArchive("missing or excessive manifest entries")
    expected = {}
    for item in entries:
        if not isinstance(item, dict) or not _safe_path(item.get("path", "")):
            raise RejectedArchive("unsafe manifest path")
        path, digest, size, mode = (item.get(k) for k in ("path", "sha256", "size", "mode"))
        if path in expected or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RejectedArchive("duplicate path or invalid sha256")
        if type(size) is not int or size < 0 or size > MAX_BYTES:
            raise RejectedArchive("invalid file size")
        if type(mode) is not int or mode not in (0o644, 0o755):
            raise RejectedArchive("unexpected file permissions")
        expected[path] = (digest, size, mode)

    seen = set()
    total = 0
    for member in tar:
        name = member.name
        if (not _safe_path(name) or name in seen or name not in expected
                or not member.isfile() or member.uid != 0 or member.gid != 0
                or (member.mode & 0o7777) != expected[name][2]):
            raise RejectedArchive("unsafe, undeclared or special archive entry")
        seen.add(name)
        want_hash, want_size, _ = expected[name]
        if member.size != want_size:
            raise RejectedArchive("file size mismatch")
        total += member.size
        if total > MAX_BYTES:
            raise RejectedArchive("archive exceeds allowed uncompressed size")
        source = tar.extractfile(member)
        if source is None:
            raise RejectedArchive("missing file bytes")
        hasher = hashlib.sha256()
        with source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        if hasher.hexdigest() != want_hash:
            raise RejectedArchive("file checksum mismatch")

    if seen != set(expected):
        raise RejectedArchive("archive entries missing from manifest")
