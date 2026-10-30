#!/usr/bin/env python3
"""Report-only vulnerability lookup for the pinned runtime inputs via the public OSV database.

Reads the Go module list and the pinned source commits, asks https://api.osv.dev for known advisories and
writes a JSON + text report. It contacts only api.osv.dev, only on a build or CI host (never on Unraid), and
never fails a build by itself: a human reads the report. A clean report is not a vulnerability review.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

OSV = "https://api.osv.dev/v1/querybatch"
COMMIT_REPOS = {
    "INCUS_COMMIT": "https://github.com/lxc/incus",
    "LXC_COMMIT": "https://github.com/lxc/lxc",
    "RAFT_COMMIT": "https://github.com/cowsql/raft",
    "COWSQL_COMMIT": "https://github.com/cowsql/cowsql",
}
MODULE = re.compile(r"^(\S+)\s+(v\S+)(?:\s+=>.*)?$")


def parse_modules(text: str) -> list[tuple[str, str]]:
    modules = []
    for line in text.splitlines():
        match = MODULE.match(line.strip())
        if match and not line.startswith("#"):
            parts = line.split()
            if "=>" in parts:
                replacement = parts[parts.index("=>") + 1:]
                if len(replacement) != 2 or not replacement[1].startswith("v"):
                    raise ValueError("cannot scan an unversioned/local module replacement")
                modules.append((replacement[0], replacement[1]))
            else:
                modules.append((match.group(1), match.group(2)))
    return sorted(set(modules))


def parse_buildinfo(text: str) -> list[tuple[str, str]]:
    """Modules actually linked into a Go binary (`go version -m`), replacements resolved."""
    deps: list[tuple[str, str]] = []
    replace_index: int | None = None
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in ("dep", "mod"):
            replace_index = None
            if len(fields) >= 3 and fields[2].startswith("v"):
                deps.append((fields[1], fields[2]))
                replace_index = len(deps) - 1
        elif fields and fields[0] == "=>":
            if replace_index is None or len(fields) < 3 or not fields[2].startswith("v"):
                raise ValueError("cannot scan an unversioned/local or orphan module replacement")
            deps[replace_index] = (fields[1], fields[2])
            replace_index = None
        elif fields:
            replace_index = None
    return sorted(set(deps))


def query(queries: list[dict], timeout: int = 60) -> list[dict]:
    results: list[dict] = []
    for start in range(0, len(queries), 500):
        body = json.dumps({"queries": queries[start:start + 500]}).encode()
        request = urllib.request.Request(OSV, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
            results.extend(json.load(response)["results"])
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modules", required=True, type=Path, help="go list -m all (whole module graph)")
    parser.add_argument("--buildinfo", required=True, type=Path, help="go version -m of the shipped binary")
    parser.add_argument("--cli-buildinfo", type=Path, help="go version -m of the other shipped binary (incus)")
    parser.add_argument("--pins", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-text", required=True, type=Path)
    args = parser.parse_args()
    if Path("/etc/unraid-version").exists():
        parser.error("never run on Unraid")

    try:
        graph = parse_modules(args.modules.read_text(encoding="utf-8"))
        linked = parse_buildinfo(args.buildinfo.read_text(encoding="utf-8"))
        if args.cli_buildinfo:
            linked = sorted(set(linked) | set(parse_buildinfo(args.cli_buildinfo.read_text(encoding="utf-8"))))
    except ValueError as exc:
        parser.error(str(exc))
    if not linked:
        parser.error("empty binary module inventory; refusing a misleading clean scan")
    pins = dict(line.split("=", 1) for line in args.pins.read_text().splitlines() if line and not line.startswith("#"))
    scopes = []  # (scope, label, query)
    for name, version in linked:
        scopes.append(("linked", f"{name}@{version}", {"package": {"ecosystem": "Go", "name": name}, "version": version}))
    for name, version in graph:
        if (name, version) not in set(linked):
            scopes.append(("graph-only", f"{name}@{version}", {"package": {"ecosystem": "Go", "name": name}, "version": version}))
    for key, repo in COMMIT_REPOS.items():
        scopes.append(("pinned-source", f"{repo}@{pins[key][:12]} ({key})", {"commit": pins[key]}))
    try:
        answers = query([q for _, _, q in scopes])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"OSV unreachable: {exc}", file=sys.stderr)
        return 3
    if len(answers) != len(scopes) or any(not isinstance(answer, dict) or answer.get("error") or
                                        answer.get("next_page_token") for answer in answers):
        print("incomplete or invalid OSV response; refusing a misleading clean report", file=sys.stderr)
        return 3

    findings = []
    for (scope, label, _), answer in zip(scopes, answers):
        for vuln in answer.get("vulns", []):
            findings.append({"scope": scope, "component": label, "id": vuln["id"]})
    counts = {scope: sum(1 for s_, _, _ in scopes if s_ == scope) for scope in ("linked", "graph-only", "pinned-source")}
    report = {"source": "api.osv.dev", "components_checked": counts, "findings": findings,
              "binary_inventory_coverage": "incusd-and-incus" if args.cli_buildinfo else "incusd-only",
              "note": "report only; OSV matches by version, not by reachability; absence of findings is not a security review"}
    args.out_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [f"OSV lookup: linked={counts['linked']} graph-only={counts['graph-only']} pinned-sources={counts['pinned-source']}",
             "(report only; matches by version, not reachability; Go standard-library issues need govulncheck)"]
    for scope in ("linked", "pinned-source", "graph-only"):
        chosen = [f for f in findings if f["scope"] == scope]
        lines.append(f"{scope}: {len(chosen)} advisory references")
        for finding in chosen:
            lines.append(f"  {finding['id']}  {finding['component']}")
    args.out_text.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
