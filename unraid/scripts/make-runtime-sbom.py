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
import sys
from pathlib import Path
from urllib.parse import quote

def pins_from(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#") and "=" in line)


def linked_modules(buildinfo: str) -> list[tuple[str, str]]:
    # The line following => replaces its preceding dep/mod entry. Reporting both
    # would falsely claim the old vulnerable module also ships in the binary.
    modules: list[tuple[str, str]] = []
    replace_index: int | None = None
    for line in buildinfo.splitlines():
        fields = line.split()
        if fields and fields[0] in ("dep", "mod"):
            replace_index = None
            if len(fields) >= 3 and fields[2].startswith("v"):
                modules.append((fields[1], fields[2]))
                replace_index = len(modules) - 1
        elif fields and fields[0] == "=>":
            if replace_index is None or len(fields) < 3 or not fields[2].startswith("v"):
                raise ValueError("unversioned/local or orphan replacement cannot be inventoried")
            modules[replace_index] = (fields[1], fields[2])
            replace_index = None
        elif fields:
            replace_index = None
    return sorted(set(modules))


def build(manifest: dict, abi: dict, buildinfo: str, pins: dict, label: str, osv: dict | None,
          cli_buildinfo: str | None = None) -> dict:
    archive_sha = manifest["archive_sha256"]
    components: list[dict] = []
    refs: set[str] = set()
    source_refs: dict[str, str] = {}

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
    for name, repo, pin_key, version in (
            ("incus", "lxc/incus", "INCUS_COMMIT", pins["INCUS_RELEASE"]),
            ("lxc", "lxc/lxc", "LXC_COMMIT", pins["LXC_RELEASE"]),
            ("raft", "cowsql/raft", "RAFT_COMMIT", pins.get("RAFT_RELEASE", "unknown")),
            ("cowsql", "cowsql/cowsql", "COWSQL_COMMIT", pins.get("COWSQL_RELEASE", "unknown"))):
        commit = pins[pin_key]
        source_ref = f"git:{repo}@{commit}"
        # Match the explicit labels emitted by osv-check.py; a source finding
        # must point to this real component, never an unbound display string.
        source_refs[f"https://github.com/{repo}@{commit[:12]} ({pin_key})"] = source_ref
        source_properties = [{"name": "polyseat:source-pin", "value": commit}]
        if name == "incus" and "INCUS_TAG_OBJECT" in pins:
            source_properties.append({"name": "polyseat:official-tag-object", "value": pins["INCUS_TAG_OBJECT"]})
        add({"type": "library" if name != "incus" else "application", "bom-ref": source_ref, "name": name,
             "version": version, "purl": f"pkg:github/{repo}@{commit}",
             "properties": source_properties})
    add({"type": "library", "bom-ref": f"golang:stdlib@{pins['GO_VERSION']}", "name": "go-stdlib",
         "version": pins["GO_VERSION"], "purl": f"pkg:golang/stdlib@{pins['GO_VERSION']}"})
    if any(entry["path"] == "payload/share/canary/busybox.tar.gz" for entry in manifest["files"]):
        # This is the exact Ubuntu package used for the offline test fixture,
        # not an assertion that its code is globally vulnerability-free.
        required = ("BUSYBOX_DEB_VERSION", "BUSYBOX_DEB_SHA256", "BUSYBOX_BINARY_SHA256", "BUSYBOX_DEB_URL")
        if any(not pins.get(key) for key in required):
            raise ValueError("BusyBox test image is present without pinned source evidence")
        package_version = pins["BUSYBOX_DEB_VERSION"]
        add({"type": "application", "bom-ref": f"deb:ubuntu/busybox-static@{package_version}",
             "name": "busybox-static", "version": package_version,
             "purl": f"pkg:deb/ubuntu/busybox-static@{quote(package_version, safe='')}?arch=amd64&distro=noble",
             "hashes": [{"alg": "SHA-256", "content": pins["BUSYBOX_DEB_SHA256"]}],
             "externalReferences": [{"type": "distribution", "url": pins["BUSYBOX_DEB_URL"]}],
             "properties": [{"name": "polyseat:scope", "value": "offline-no-network-canary-image"},
                            {"name": "polyseat:binary-sha256", "value": pins["BUSYBOX_BINARY_SHA256"]}]})
    inventory = set(linked_modules(buildinfo))
    if not inventory:
        raise ValueError("empty daemon binary inventory")
    if cli_buildinfo is not None:
        cli_inventory = set(linked_modules(cli_buildinfo))
        if not cli_inventory:
            raise ValueError("empty CLI binary inventory")
        inventory.update(cli_inventory)
    for name, version in sorted(inventory):
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
            if finding["scope"] == "pinned-source":
                ref = source_refs.get(finding["component"])
                detail = "matched by pinned source commit; reachability not assessed"
            else:
                ref = f"golang:{finding['component']}"
                detail = "matched by version only; reachability not assessed"
            if ref not in refs:
                raise ValueError(f"OSV {finding['scope']} finding does not match runtime inventory: {finding['component']}")
            vulnerabilities.append({
                "id": finding["id"], "source": {"name": "OSV", "url": "https://osv.dev/vulnerability/" + finding["id"]},
                "analysis": {"state": "in_triage", "detail": detail},
                "affects": [{"ref": ref}]})

    return {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {
            "component": {"type": "application", "bom-ref": f"bundle:{archive_sha}", "name": manifest["name"],
                          "version": manifest["version"], "hashes": [{"alg": "SHA-256", "content": archive_sha}]},
            "properties": [
                {"name": "polyseat:purpose", "value": "runtime-bundle-inventory"},
                {"name": "polyseat:binary-inventory-coverage", "value": "incusd-and-incus" if cli_buildinfo is not None else "incusd-only"},
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
    parser.add_argument("--cli-buildinfo", type=Path, help="also inventory the shipped incus CLI")
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
                     args.buildinfo.read_text(), pins_from(args.pins.read_text()), args.label, osv,
                     args.cli_buildinfo.read_text() if args.cli_buildinfo else None)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
