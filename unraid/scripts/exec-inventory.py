#!/usr/bin/env python3
"""Classify every program a traced CI canary run executed (strace -ff output).

Read-only analysis of a disposable-runner trace; never runs anything. Fails (exit 1) when canary code, the
controller, Incus or LXC executed a host program by absolute path that does not exist on the recorded
Unraid host, when the trace does not cover a real lifecycle, or when it cannot be parsed. Programs found
through the Tower-like PATH exist on Tower by construction. What a host tool does internally (for example
Ubuntu's ldconfig wrapper exec'ing ldconfig.real) is reported, not judged: Tower has its own build of it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXEC = re.compile(r'^execve(?:at)?\((?:[^,"]+, )?"((?:[^"\\]|\\.)*)", \[(?:"(?:[^"\\]|\\.)*")?(?:, "((?:[^"\\]|\\.)*)")?')
# incusd re-executes itself for these internal subcommands; liblxc calls hooks via /proc/<daemon pid>/exe.
SELF_SUBCOMMAND = re.compile(r"^(callhook|fork[a-z]+)$")
RESULT = re.compile(r"\)\s+=\s+(-?\d+)(?:\s+([A-Z][A-Z0-9_]*))?")
CHILD = re.compile(r"^(?:clone3?|fork|vfork)\(.*\)\s+=\s+(\d+)\s*$")
NAME = re.compile(r"^(\[|[A-Za-z0-9][A-Za-z0-9._+-]*)$")


def load_tools(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            if not NAME.fullmatch(line):
                raise ValueError(f"invalid tool name in inventory: {line!r}")
            names.add(line)
    if len(names) < 100:
        raise ValueError("Tower tool inventory is implausibly small")
    return names


def parse(trace_dir: Path, prefix: str):
    """Return {pid: [(path, ok, errno)]} and {child: parent} from per-process strace files."""
    execs: dict[int, list[tuple[str, bool, str, str]]] = {}
    parents: dict[int, int] = {}
    files = sorted(p for p in trace_dir.iterdir() if p.name.startswith(prefix + "."))
    if not files:
        raise ValueError("no trace files")
    for path in files:
        pid = int(path.name.rsplit(".", 1)[1])
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            child = CHILD.match(line)
            if child:
                parents[int(child.group(1))] = pid
                continue
            match = EXEC.match(line)
            if not match:
                continue
            result = RESULT.search(line[match.end():])
            if not result:
                raise ValueError(f"unfinished exec record in {path.name}: {line[:120]}")
            program = bytes(match.group(1), "utf-8").decode("unicode_escape")
            argv1 = match.group(2) or ""
            execs.setdefault(pid, []).append((program, result.group(1) == "0", result.group(2) or "", argv1))
    return execs, parents


# Programs that only exec their argument (or interpret our own scripts). After them the traced process still
# runs controller/Incus code, so classification stays strict instead of becoming "tool-internal".
WRAPPERS = {"bash", "sh", "env", "setsid", "setpriv", "unshare", "timeout", "nice", "nohup", "nsenter",
            "stdbuf", "flock", "chroot", "ionice", "taskset"}


def classify(args) -> dict:
    tools = load_tools(args.tower_tools)
    fixture = str(args.fixture).rstrip("/")
    ram = f"{fixture}/usr/local/polyseat-unraid/"
    deny = f"{fixture}/var/run/polyseat-unraid/deny-tools/"
    tower_path = str(args.tower_path).rstrip("/") + "/"
    image = {"/" + line.split("/", 1)[1] for line in args.image_files.read_text().splitlines()
             if line.startswith("rootfs/") and "/" in line}
    harness = {str(args.controller), "/usr/bin/env"}
    execs, parents = parse(args.trace_dir, args.prefix)
    report = {"bundle": set(), "self-reexec": 0, "denied": set(), "harness": set(), "container": set(),
              "tower-path": set(), "host-absolute-known": set(), "tool-internal": set(),
              "missing-everywhere": set(), "violations": []}

    def host_tool(program: str) -> str | None:
        """Name of a host program (Tower-like PATH or absolute path known on Tower), else None."""
        name = program.rsplit("/", 1)[-1]
        if program.startswith(tower_path) or (program.startswith("/") and name in tools and not (
                program.startswith(ram) or program.startswith(deny) or program in harness or program in image
                or program.startswith("/proc/"))):
            return name
        return None

    def own_tool(pid: int) -> bool:
        return any(ok and (name := host_tool(program)) is not None and name not in WRAPPERS
                   for program, ok, _, _ in execs.get(pid, []))

    memo: dict[int, bool] = {}

    def inherited_tool(pid: int) -> bool:
        """True when an ancestor ran a (non-wrapper) host tool: then this process is that tool's internals."""
        if pid in memo:
            return memo[pid]
        parent = parents.get(pid)
        memo[pid] = False  # cycle guard for corrupt traces
        memo[pid] = parent is not None and (own_tool(parent) or inherited_tool(parent))
        return memo[pid]

    succeeded_names = set()
    strict_failures: list[tuple[int, str, str]] = []
    for pid in sorted(execs):
        internal = inherited_tool(pid)
        for program, ok, errno, argv1 in execs[pid]:
            name = program.rsplit("/", 1)[-1]
            if not ok:
                if not internal:
                    strict_failures.append((pid, name, errno))
                continue
            succeeded_names.add(name)
            # Whatever ran it, nothing reached through the Tower-like PATH may be missing on Tower.
            if program.startswith(tower_path) and name not in tools:
                report["violations"].append(f"Tower-like PATH contains a program Tower lacks: {name}")
            if internal:
                report["tool-internal"].add(program)
                continue
            if program.startswith(ram):
                report["bundle"].add(program[len(fixture):])
            elif program == "/proc/self/exe" or (re.fullmatch(r"/proc/[0-9]+/exe", program)
                                                 and SELF_SUBCOMMAND.fullmatch(argv1)):
                report["self-reexec"] += 1
            elif program.startswith(deny):
                report["denied"].add(name)
            elif program in harness:
                report["harness"].add(program)
            elif program in image:
                report["container"].add(program)
            elif program.startswith(tower_path):
                report["tower-path"].add(name)
                internal = name not in WRAPPERS
            elif program.startswith("/") and name in tools:
                report["host-absolute-known"].add(program)
                internal = name not in WRAPPERS
            else:
                report["violations"].append(f"pid {pid} executed {program!r}, which the Unraid host does not have")
    for pid, name, errno in strict_failures:
        if errno == "ENOENT" and name not in succeeded_names:
            report["missing-everywhere"].add(name)
    if args.require_lifecycle:
        if not any(path.endswith("/payload/bin/incusd") for path in report["bundle"]):
            report["violations"].append("trace does not contain the bundled incusd: lifecycle not covered")
        if not report["container"]:
            report["violations"].append("trace does not contain a container process: lifecycle not covered")
    result = {key: sorted(value) if isinstance(value, set) else value for key, value in report.items()}
    result["missing-everywhere-but-on-tower"] = sorted(n for n in report["missing-everywhere"] if n in tools)
    result["schema"] = 1
    result["processes"] = len(execs)
    result["ok"] = not report["violations"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("trace-dir", "tower-tools", "tower-path", "fixture", "controller", "image-files", "output"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--prefix", default="exec")
    parser.add_argument("--require-lifecycle", action="store_true",
                        help="also fail unless the trace covers the bundled daemon and a container process")
    args = parser.parse_args()
    try:
        result = classify(args)
    except (ValueError, OSError, UnicodeError) as error:
        print(f"EXEC INVENTORY FAILED: {error}", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("ok", "processes", "violations", "tower-path", "denied",
                                             "missing-everywhere-but-on-tower")}, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
