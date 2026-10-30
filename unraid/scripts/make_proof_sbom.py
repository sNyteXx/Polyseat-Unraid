#!/usr/bin/env python3
"""Make an explicitly incomplete CycloneDX Go *build-source* inventory.

This does not scan vulnerabilities or enumerate required Unraid runtime libs.
No network or host changes. Only reads Go module output and writes a JSON file.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def create(modules: str, release: str, revisions: dict[str, str]) -> dict:
    components = []
    seen = set()
    for line in modules.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0].startswith("#"):
            continue
        if "=>" in fields:
            replacement = fields.index("=>")
            if len(fields) != replacement + 3:
                raise ValueError("unversioned Go module replacement cannot be pinned")
            path, version = fields[replacement + 1:]
        else:
            path, version = fields[:2]
        if version.startswith("=>") or version == "(devel)":
            continue
        if (path, version) in seen:
            continue
        seen.add((path, version))
        components.append({
            "type": "library", "name": path, "version": version,
            "bom-ref": f"golang:{path}@{version}",
            "purl": f"pkg:golang/{path}@{version}",
        })
    for component in ("raft", "cowsql"):
        components.append({
            "type": "library", "name": f"cowsql/{component}",
            "version": revisions[component],
            "bom-ref": f"git:cowsql/{component}@{revisions[component]}",
        })
    components.sort(key=lambda item: item["bom-ref"])
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "incus-build-proof-NOT-FOR-UNRAID",
                          "version": release},
            "properties": [
                {"name": "polyseat:purpose", "value": "source-inventory-only-not-runtime"},
                {"name": "polyseat:incus-commit", "value": revisions["incus"]},
                {"name": "polyseat:unraid-certified", "value": "false"},
            ],
        },
        "components": components,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modules", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if (os.environ.get("CI") != "true" or os.environ.get("RUNNER_OS") != "Linux"
            or Path("/etc/unraid-version").exists()):
        parser.error("only an isolated Linux CI build can write this inventory")
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "/nonexistent")).resolve()
    allowed = workspace / "incus-build-proof"
    if (args.output.is_symlink() or args.modules.is_symlink()
            or args.output.resolve().parent != allowed
            or args.modules.resolve().parent != allowed):
        parser.error("inventory files must remain inside the CI proof output")
    revisions = {name: os.environ[name.upper() + "_COMMIT"]
                 for name in ("incus", "raft", "cowsql")}
    report = create(args.modules.read_text(encoding="utf-8"),
                    os.environ["INCUS_RELEASE"], revisions)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
