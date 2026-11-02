#!/usr/bin/env python3
"""Content-addressed key for the pinned Incus runtime build (CI reuse; never a trust anchor on its own).

The key covers everything that determines the runtime bytes: the build scripts and libraries they import, the
dependency overlay, the ABI baseline, the runtime pins (seat-image pins excluded), the whole workflow file (apt build
dependencies, toolchain action pins, env pins), this script and the reuse verifier, and the GitHub runner image
(ImageOS/ImageVersion/arch), because the apt build dependencies are not version-pinned. Any change gives a new key,
so a reuse candidate can only be the output of an identical build recipe on an identical runner image.

  ci-runtime-key.py [--repo DIR] [--json-out FILE]     -> prints the key
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

SCHEMA = 1
INPUT_FILES = (
    "unraid/scripts/lib/build-core.sh",
    "unraid/scripts/build-incus-artifact.sh",
    "unraid/scripts/prepare-incus-deps.py",
    "unraid/dependency-overrides.json",
    "unraid/scripts/check-bundle-abi.py",
    "unraid/lab/abi.py",
    "unraid/scripts/make-runtime-bundle.py",
    "unraid/lab/bundle.py",
    "unraid/lab/__init__.py",
    "unraid/scripts/make-runtime-sbom.py",
    "unraid/scripts/make_proof_sbom.py",
    "unraid/scripts/prepare-canary-image.sh",
    "unraid/scripts/make-canary-image.py",
    ".github/workflows/incus-feasibility.yml",
    "unraid/scripts/ci-runtime-key.py",
    "unraid/scripts/ci-runtime-reuse.py",
)
INPUT_DIRS = ("unraid/abi", "unraid/patches")
NON_RUNTIME_PIN_PREFIXES = ("SEAT_", "SUNSHINE_")
RUNNER_ENV = ("ImageOS", "ImageVersion", "RUNNER_ARCH", "RUNNER_OS")


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_pins(text: str) -> dict:
    pins = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not key.startswith(NON_RUNTIME_PIN_PREFIXES):
            pins[key] = value
    return dict(sorted(pins.items()))


def inputs(repo: Path, env: dict) -> dict:
    files = {}
    for rel in INPUT_FILES:
        path = repo / rel
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"runtime build input missing or not a regular file: {rel}")
        files[rel] = sha_file(path)
    for rel in INPUT_DIRS:
        for path in sorted((repo / rel).rglob("*")):
            if path.is_symlink():
                raise ValueError(f"symlink in runtime build input: {path}")
            if path.is_file():
                files[path.relative_to(repo).as_posix()] = sha_file(path)
    return {"schema": SCHEMA,
            "files": dict(sorted(files.items())),
            "pins": runtime_pins((repo / "unraid/build-pins.env").read_text(encoding="utf-8")),
            "runner": {name: env.get(name, "unknown") for name in RUNNER_ENV}}


def key_of(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    try:
        data = inputs(args.repo, dict(os.environ))
    except (OSError, ValueError) as error:
        parser.exit(2, f"RUNTIME KEY BLOCKED: {error}\n")
    key = key_of(data)
    if args.json_out:
        args.json_out.write_text(json.dumps({"key": key, "inputs": data}, sort_keys=True, indent=1) + "\n")
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
