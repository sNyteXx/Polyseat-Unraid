#!/usr/bin/env python3
"""CycloneDX 1.6 SBOM of the RUNTIME bundle (what would ship), not just the build inputs.

Sources: the bundle manifest (exact files with SHA-256), the Go build info of the shipped binary (modules actually
linked), the pinned C sources, the ABI report (libraries the host must provide) and, optionally, an OSV report that
is recorded as `in_triage` vulnerabilities. It never claims a vulnerability review or Unraid certification.
Build or CI host only; refuses on Unraid and never overwrites.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEP = re.compile(r"^\s*(?:dep|=>)\s+(\S+)\s+(v\S+)")


def pins_from(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#") and "=" in line)


def linked_modules(buildinfo: str) -> list[tuple[str, str]]:
    return sorted({(m.group(1), m.group(2)) for m in map(DEP.match, buildinfo.splitlines()) if m})


def build(manifest: dict, abi: dict, buildinfo: str, pins: dict, label: str, osv: dict | None) -> dict:
    archive_sha = manifest["archive_sha256"]
    components: list[dict] = []
    refs: set[str] = set()

    def add(component: dict) -> None:
        if component["bom-ref"] in refs:
            return
        refs.add(component["bom-ref"])
        components.append(component)

    for entry in manifest["files"]:
        add({"type": "file", "bom-ref": f"file:{entry['path']}", "name": entry["path"],
             "hashes": [{"alg": "SHA-256", "content": entry["sha256"]}],
             "properties": [{"name": "polyseat:mode", "value": oct(entry["mode"])},
                            {"name": "polyseat:size", "value": str(entry["size"])}]})
    for name, repo, commit, version in (
            ("incus", "lxc/incus", pins["INCUS_COMMIT"], pins["INCUS_RELEASE"]),
            ("lxc", "lxc/lxc", pins["LXC_COMMIT"], pins["LXC_RELEASE"]),
            ("raft", "cowsql/raft", pins["RAFT_COMMIT"], pins.get("RAFT_RELEASE", "unknown")),
            ("cowsql", "cowsql/cowsql", pins["COWSQL_COMMIT"], pins.get("COWSQL_RELEASE", "unknown"))):
        add({"type": "library" if name != "incus" else "application", "bom-ref": f"git:{repo}@{commit}", "name": name,
             "version": version, "purl": f"pkg:github/{repo}@{commit}",
             "properties": [{"name": "polyseat:source-pin", "value": commit}]})
    add({"type": "library", "bom-ref": f"golang:stdlib@{pins['GO_VERSION']}", "name": "go-stdlib",
         "version": pins["GO_VERSION"], "purl": f"pkg:golang/stdlib@{pins['GO_VERSION']}"})
    for name, version in linked_modules(buildinfo):
        add({"type": "library", "bom-ref": f"golang:{name}@{version}", "name": name, "version": version,
             "purl": f"pkg:golang/{name}@{version}"})
    for soname in abi.get("host_libraries", []):
        add({"type": "library", "bom-ref": f"host:{soname}", "name": soname,
             "properties": [{"name": "polyseat:supplied-by", "value": "the Unraid host (not shipped, version not controlled)"}]})

    vulnerabilities = []
    if osv:
        for finding in osv.get("findings", []):
            if finding["scope"] not in ("linked", "pinned-source"):
                continue
            ref = f"golang:{finding['component']}"
            vulnerabilities.append({
                "id": finding["id"], "source": {"name": "OSV", "url": "https://osv.dev/vulnerability/" + finding["id"]},
                "analysis": {"state": "in_triage",
                             "detail": "matched by version only; reachability not assessed"},
                "affects": [{"ref": ref if ref in refs else finding["component"]}]})

    return {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {
            "component": {"type": "application", "bom-ref": f"bundle:{archive_sha}", "name": manifest["name"],
                          "version": manifest["version"], "hashes": [{"alg": "SHA-256", "content": archive_sha}]},
            "properties": [
                {"name": "polyseat:purpose", "value": "runtime-bundle-inventory"},
                {"name": "polyseat:build-label", "value": label},
                {"name": "polyseat:unraid-certified", "value": "false"},
                {"name": "polyseat:vulnerability-review", "value": "not-performed"},
                {"name": "polyseat:signed", "value": "false"},
                {"name": "polyseat:built-without", "value": ",".join(manifest.get("built_without", []))},
            ],
        },
        "components": sorted(components, key=lambda c: c["bom-ref"]),
        **({"vulnerabilities": vulnerabilities} if vulnerabilities else {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--abi-report", required=True, type=Path)
    parser.add_argument("--buildinfo", required=True, type=Path)
    parser.add_argument("--pins", required=True, type=Path)
    parser.add_argument("--osv-report", type=Path, default=None)
    parser.add_argument("--label", required=True, choices=("ci", "localdev"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if Path("/etc/unraid-version").exists():
        parser.error("never run on Unraid")
    if args.output.exists() or args.output.is_symlink():
        parser.error("refusing to overwrite the output")
    osv = json.loads(args.osv_report.read_text()) if args.osv_report else None
    document = build(json.loads(args.manifest.read_text()), json.loads(args.abi_report.read_text()),
                     args.buildinfo.read_text(), pins_from(args.pins.read_text()), args.label, osv)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
