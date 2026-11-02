#!/usr/bin/env python3
"""Pack a payload directory into a verified, reproducible runtime bundle.

Writes only ``<sha256>.tar.gz`` and ``<sha256>.manifest.json`` into an existing
output directory. Does not sign, install or contact a host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from unraid.lab import bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--name", default="polyseat-incus")
    parser.add_argument("--version", required=True)
    parser.add_argument("--incus-version", required=True)
    parser.add_argument("--min-unraid", default="7.2.0")
    parser.add_argument("--min-glibc", required=True)
    parser.add_argument("--min-kernel", default="6.12")
    parser.add_argument("--built-without", action="append", default=None,
                        choices=["apparmor", "selinux"],
                        help="LSM support deliberately absent from the bundled liblxc "
                             "(default: apparmor and selinux)")
    parser.add_argument("--abi-report", required=True, type=Path,
                        help="JSON from check-bundle-abi.py; must have ok=true")
    args = parser.parse_args()
    report = json.loads(args.abi_report.read_text(encoding="utf-8"))
    if report.get("ok") is not True:
        parser.error("refusing to bundle: ABI report is not ok")
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    archive, manifest = bundle.build(
        args.payload, args.out, name=args.name, version=args.version,
        incus_version=args.incus_version, min_unraid=args.min_unraid,
        min_glibc=args.min_glibc, min_kernel=args.min_kernel,
        built_without=tuple(args.built_without or ('apparmor', 'selinux')), needed_libs=report["host_libraries"],
        source_date_epoch=epoch)
    print(archive)
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
