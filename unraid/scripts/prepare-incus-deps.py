#!/usr/bin/env python3
"""Apply a reviewed, checksum-pinned Go dependency overlay to a FRESH Incus checkout.

Build host only. No service start or host installation. This deliberately dirties
go.mod/go.sum; Go must retain the truthful `v7.5.1+dirty` version and VCS metadata.
The official release tag is restored/verified by checkout_pinned before this runs.
No broad `go get -u`, local replacement, scanner suppression or version spoofing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
SUM = re.compile(r"h1:[A-Za-z0-9+/]{43}=\Z")
MODULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~/-]*\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key in pins:
            raise ValueError("invalid or duplicate build pin")
        pins[key] = value
    for key in ("INCUS_COMMIT", "INCUS_TAG_OBJECT"):
        if not SHA.fullmatch(pins.get(key, "")):
            raise ValueError(f"missing or invalid {key}")
    if not VERSION.fullmatch(pins.get("INCUS_RELEASE", "")):
        raise ValueError("invalid Incus release pin")
    return pins


def validate_plan(plan: dict, pins: dict[str, str]) -> list[dict]:
    if plan.get("schema") != 1 or plan.get("incus_commit") != pins["INCUS_COMMIT"] or \
            plan.get("incus_release") != pins["INCUS_RELEASE"]:
        raise ValueError("dependency plan does not match the exact pinned Incus release")
    modules = plan.get("modules")
    if not isinstance(modules, list) or not modules:
        raise ValueError("dependency plan is empty")
    seen: set[str] = set()
    for module in modules:
        path = module.get("path", "")
        if not MODULE.fullmatch(path) or path in seen or ".." in path:
            raise ValueError("duplicate or invalid module path")
        seen.add(path)
        if not VERSION.fullmatch(module.get("from", "")) or not VERSION.fullmatch(module.get("version", "")):
            raise ValueError(f"unpinned dependency version: {path}")
        for field in ("sum", "go_mod_sum"):
            if not SUM.fullmatch(module.get(field, "")):
                raise ValueError(f"missing checksum {field}: {path}")
        if not module.get("reason") or not isinstance(module.get("direct", True), bool):
            raise ValueError("dependency plan requires a reason and boolean direct flag")
    return modules


def json_stream(text: str) -> list[dict]:
    result: list[dict] = []
    decoder = json.JSONDecoder()
    remaining = text.strip()
    while remaining:
        item, end = decoder.raw_decode(remaining)
        if not isinstance(item, dict):
            raise ValueError("Go output contains a non-object")
        result.append(item)
        remaining = remaining[end:].lstrip()
    if not result:
        raise ValueError("empty Go metadata")
    return result


def module_graph(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in json_stream(text):
        if item.get("Error") or item.get("Replace"):
            raise ValueError("failed or replaced module in build graph")
        if item.get("Main"):
            if item.get("Path") != "github.com/lxc/incus/v7":
                raise ValueError("unexpected main module")
            continue
        path, version = item.get("Path", ""), item.get("Version", "")
        if path in result or not MODULE.fullmatch(path) or not version.startswith("v"):
            raise ValueError("invalid module graph record")
        result[path] = version
    return result


def check_graph(before: dict[str, str], after: dict[str, str], modules: list[dict]) -> list[dict]:
    expected = {m["path"]: m for m in modules}
    for name, entry in expected.items():
        if before.get(name) != entry["from"]:
            raise ValueError(f"unreviewed baseline module {name}: {before.get(name)!r}")
        if after.get(name) != entry["version"]:
            raise ValueError(f"dependency version drift for {name}: {after.get(name)!r}")
    changes = []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if name not in expected:
            raise ValueError(f"unreviewed transitive module change: {name}: {old} -> {new}")
        changes.append({"path": name, "from": old, "to": new})
    return changes


def check_download(item: dict, expected: dict) -> None:
    for actual, wanted in (("Path", "path"), ("Version", "version"), ("Sum", "sum"), ("GoModSum", "go_mod_sum")):
        if item.get(actual) != expected[wanted]:
            raise ValueError(f"download {actual} mismatch for {expected['path']}")
    if item.get("Error"):
        raise ValueError(f"module download failed: {expected['path']}")


def check_source_evidence(item: dict, evidence: dict) -> None:
    """Validate source-fixed triage against the exact authenticated module files.

    These assertions preserve, not suppress, the advisory. A different source
    version or even a changed regression test invalidates this review.
    """
    check_download(item, evidence)
    source = Path(item["Dir"]).resolve(strict=True)
    for entry in evidence.get("files", []):
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("unsafe source-evidence path")
        target = source / relative
        if target.is_symlink() or source not in target.resolve(strict=True).parents:
            raise ValueError("source-evidence path escaped the authenticated module")
        if sha256(target) != entry["sha256"]:
            raise ValueError(f"source-fixed advisory evidence changed: {evidence['path']}/{relative}")
    if not evidence.get("files"):
        raise ValueError("source-fixed advisory requires actual file evidence")


def materialize_verified_sums(text: str, modules: list[dict]) -> str:
    """Persist only module checksums already verified against Go's sum database.

    An explicit version download outside the old build list can populate the
    module cache without adding its zip sum to go.sum. A subsequent lazy
    `go mod download` need not add it either. Do not wait for a readonly compile
    to discover that: materialize the authenticated exact sums after go.mod edit.
    Existing conflicting sums are an error, never silently overwritten.
    """
    entries: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3:
            raise ValueError("malformed go.sum entry")
        key = fields[0], fields[1]
        if key in entries and entries[key] != fields[2]:
            raise ValueError("conflicting duplicate go.sum entry")
        entries[key] = fields[2]
    for module in modules:
        for suffix, field in (("", "sum"), ("/go.mod", "go_mod_sum")):
            key = module["path"], module["version"] + suffix
            if key in entries and entries[key] != module[field]:
                raise ValueError(f"existing go.sum conflicts with authenticated pin: {key}")
            entries[key] = module[field]
    return "".join(f"{path} {version} {value}\n" for (path, version), value in sorted(entries.items()))


def public_go_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GOTOOLCHAIN": "local", "GOWORK": "off", "GOPROXY": "https://proxy.golang.org",
                "GOSUMDB": "sum.golang.org", "GONOSUMDB": "", "GONOPROXY": "", "GOPRIVATE": "",
                "GOINSECURE": ""})
    return env


def run(source: Path, *args: str) -> str:
    completed = subprocess.run(args, cwd=source, env=public_go_environment(), text=True, capture_output=True)
    if completed.returncode:
        # `go mod download -json` places its useful Error in stdout, not stderr.
        # These are public pinned dependencies; report that diagnostic as well.
        detail = completed.stderr[-2000:] + completed.stdout[-4000:]
        raise ValueError(f"{' '.join(args[:4])} failed (exit {completed.returncode}): {detail}")
    return completed.stdout


def prepare(source: Path, pins: dict, plan_path: Path, report: Path) -> dict:
    if Path("/etc/unraid-version").exists():
        raise ValueError("never prepare a build on Unraid")
    if source.is_symlink() or not (source / ".git").is_dir():
        raise ValueError("a fresh standalone checkout is required")
    if report.exists() or report.is_symlink():
        raise ValueError("refusing to overwrite the dependency report")
    plan = json.loads(plan_path.read_text())
    modules = validate_plan(plan, pins)
    if run(source, "git", "rev-parse", "HEAD").strip() != pins["INCUS_COMMIT"]:
        raise ValueError("wrong Incus source commit")
    tag = f"refs/tags/{pins['INCUS_RELEASE']}"
    if run(source, "git", "rev-parse", tag).strip() != pins["INCUS_TAG_OBJECT"] or \
            run(source, "git", "rev-parse", tag + "^{}").strip() != pins["INCUS_COMMIT"]:
        raise ValueError("official annotated release tag was not restored faithfully")
    if run(source, "git", "status", "--porcelain").strip():
        raise ValueError("source checkout is already dirty; refusing to replace user changes")
    before = module_graph(run(source, "go", "list", "-mod=readonly", "-m", "-json", "all"))
    before_mod = sha256(source / "go.mod")
    before_sum = sha256(source / "go.sum")
    for module in modules:
        if before.get(module["path"]) != module["from"]:
            raise ValueError(f"baseline dependency mismatch: {module['path']}")
        objects = json_stream(run(source, "go", "mod", "download", "-json", f"{module['path']}@{module['version']}"))
        if len(objects) != 1:
            raise ValueError("unexpected module download result")
        check_download(objects[0], module)
    for evidence in plan.get("source_evidence", []):
        if before.get(evidence["path"]) != evidence["version"]:
            raise ValueError("source-fixed advisory applies to a different module version")
        objects = json_stream(run(source, "go", "mod", "download", "-json", f"{evidence['path']}@{evidence['version']}"))
        if len(objects) != 1:
            raise ValueError("unexpected source-evidence download result")
        check_source_evidence(objects[0], evidence)
    # Only exact reviewed requirements are changed. MVS may update the reviewed
    # indirect requirements; the complete graph diff below rejects anything else.
    run(source, "go", "mod", "edit", *(f"-require={m['path']}@{m['version']}" for m in modules if m.get("direct", True)))
    after = module_graph(run(source, "go", "list", "-mod=mod", "-m", "-json", "all"))
    changes = check_graph(before, after, modules)
    # Every value being materialized was authenticated by check_download above;
    # no unauthenticated sum is invented and no module version changes here.
    sumfile = source / "go.sum"
    sumfile.write_text(materialize_verified_sums(sumfile.read_text(), modules), encoding="utf-8")
    run(source, "go", "mod", "download")
    final_graph = module_graph(run(source, "go", "list", "-mod=readonly", "-m", "-json", "all"))
    check_graph(before, final_graph, modules)
    actual_sum_lines = set(sumfile.read_text().splitlines())
    for module in modules:
        for suffix, field in (("", "sum"), ("/go.mod", "go_mod_sum")):
            if f"{module['path']} {module['version']}{suffix} {module[field]}" not in actual_sum_lines:
                raise ValueError(f"verified checksum was not persisted: {module['path']}{suffix}")
    run(source, "go", "mod", "verify")
    changed_files = run(source, "git", "diff", "--name-only").splitlines()
    if not changed_files or set(changed_files) - {"go.mod", "go.sum"}:
        raise ValueError("dependency preparation unexpectedly changed source files")
    document = {
        "schema": 1, "purpose": "reviewed-security-dependency-overlay-not-upstream-release",
        "incus_commit": pins["INCUS_COMMIT"], "incus_release": pins["INCUS_RELEASE"],
        "incus_tag_object": pins["INCUS_TAG_OBJECT"], "plan_sha256": sha256(plan_path),
        "expected_main_module_version": pins["INCUS_RELEASE"] + "+dirty",
        "sumdb": "sum.golang.org", "modules": modules, "graph_changes": changes,
        "source_fixed_advisories": plan.get("source_evidence", []),
        "changed_files": sorted(changed_files),
        "upstream_go_mod_sha256": before_mod, "upstream_go_sum_sha256": before_sum,
        "prepared_go_mod_sha256": sha256(source / "go.mod"), "prepared_go_sum_sha256": sha256(source / "go.sum"),
    }
    with report.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--pins", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        document = prepare(args.source, load_pins(args.pins), args.plan, args.report)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"DEPENDENCY PREPARATION BLOCKED: {exc}\n")
    print(f"Verified {len(document['graph_changes'])} pinned dependency changes; source remains truthfully dirty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
