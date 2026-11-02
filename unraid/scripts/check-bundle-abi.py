#!/usr/bin/env python3
"""Check a payload directory's ELF closure against the recorded Unraid baseline.

Only runs ``readelf`` (static inspection). Never executes bundle code and never
connects to a host. Exit status 1 when a library or symbol version is missing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from unraid.lab import abi  # noqa: E402

MAGIC = b"\x7fELF"


def _readelf(flag: str, path: Path) -> str:
    return subprocess.run(["readelf", "-W", flag, str(path)], check=True,
                          capture_output=True, text=True).stdout


def collect(payload: Path) -> list[abi.Elf]:
    elfs = []
    for path in sorted(p for p in payload.rglob("*") if p.is_file() and not p.is_symlink()):
        with path.open("rb") as stream:
            if stream.read(4) != MAGIC:
                continue
        dynamic = abi.parse_dynamic(_readelf("-d", path))
        versions = abi.parse_version_needs(_readelf("-V", path))
        elfs.append(abi.Elf(str(path.relative_to(payload)), dynamic["needed"],
                            dynamic["soname"], versions, dynamic["runpath"]))
    return elfs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path,
                        help="path prefix, e.g. unraid/abi/unraid-7.3.3-x86_64")
    parser.add_argument("--max-glibc", default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    sonames = abi.parse_baseline_sonames(
        Path(f"{args.baseline}.sonames").read_text(encoding="utf-8"))
    symvers = abi.parse_baseline_symvers(
        Path(f"{args.baseline}.symvers").read_text(encoding="utf-8"))
    report = abi.check(collect(args.payload), sonames, symvers)
    problems = report.as_dict()
    if args.max_glibc and report.glibc_max:
        if abi._vkey(report.glibc_max) > abi._vkey(args.max_glibc):
            problems["ok"] = False
            problems["glibc_too_new"] = report.glibc_max
    text = json.dumps(problems, indent=2, sort_keys=True)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0 if problems["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
