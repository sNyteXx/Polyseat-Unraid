#!/usr/bin/env python3
"""Write the strict, signable manifest that binds one seat image tarball.

The controller (`polyseatctl seat-image-stage`) accepts exactly these keys and nothing
else. Signing happens separately: `ssh-keygen -Y sign -n polyseat-unraid-seat-image`.
Does not sign, upload, install or contact a host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

MAX_IMAGE_BYTES = 1024 * 1024 * 1024
MAX_UNPACKED_BYTES = 3 * 1024 * 1024 * 1024
ALIAS = "polyseat-desktop"
SELFTEST = "/usr/local/bin/polyseat-selftest"


def build(image: Path, unpacked_bytes: int, source_commit: str, profiles: list[str], created_at: int) -> dict:
    if not image.is_file() or image.is_symlink():
        raise ValueError("image must be a regular file")
    size = image.stat().st_size
    if not 0 < size <= MAX_IMAGE_BYTES:
        raise ValueError("image size outside 1 byte .. 1 GiB")
    if not 0 < unpacked_bytes < MAX_UNPACKED_BYTES:
        raise ValueError("unpacked size must be below the desktop canary's 3 GiB data budget")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be a full lowercase SHA-1")
    if "cpu" not in profiles or any(p not in ("cpu", "nvidia") for p in profiles) or len(set(profiles)) != len(profiles):
        raise ValueError("profiles must include cpu and may only add nvidia")
    if created_at <= 0:
        raise ValueError("created_at must be a positive epoch")
    digest = hashlib.sha256()
    with image.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return {"schema": 1, "name": ALIAS, "alias": ALIAS, "selftest": SELFTEST,
            "image_sha256": digest.hexdigest(), "image_size": size, "unpacked_bytes": unpacked_bytes,
            "source_commit": source_commit, "profiles": profiles, "created_at": created_at}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--unpacked-bytes", required=True, type=int)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--profiles", default="cpu,nvidia")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    created = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    try:
        manifest = build(args.image, args.unpacked_bytes, args.source_commit, args.profiles.split(","), created)
    except ValueError as error:
        parser.error(str(error))
    if args.out.exists() or args.out.is_symlink():
        parser.error("refusing to overwrite an existing manifest")
    args.out.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(manifest["image_sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
