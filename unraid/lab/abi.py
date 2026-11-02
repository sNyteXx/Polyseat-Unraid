"""Offline ELF/ABI closure check against a recorded host library baseline.

Pure parsing plus set arithmetic; the CLI wrapper runs ``readelf`` on files in
a build directory. Nothing is executed from the inspected bundle (no ``ldd``).
A pass is evidence about the recorded baseline only, never a certification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

NEEDED = re.compile(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]")
SONAME = re.compile(r"\(SONAME\)\s+Library soname: \[([^\]]+)\]")
RUNPATH = re.compile(r"\((?:RUNPATH|RPATH)\)\s+Library (?:runpath|rpath): \[([^\]]*)\]")
NEED_FILE = re.compile(r"File:\s+(\S+)\s+Cnt:\s+\d+")
NEED_NAME = re.compile(r"Name:\s+(\S+)\s+Flags:")
SAFE_SONAME = re.compile(r"^[A-Za-z0-9_.+-]+$")


class AbiError(ValueError):
    """Baseline or ELF metadata is malformed."""


def parse_dynamic(text: str) -> dict:
    needed = NEEDED.findall(text)
    soname = SONAME.findall(text)
    runpath = RUNPATH.findall(text)
    for name in needed + soname:
        if not SAFE_SONAME.fullmatch(name):
            raise AbiError(f"unsafe library name: {name!r}")
    return {"needed": needed, "soname": soname[0] if soname else None,
            "runpath": runpath}


def parse_version_needs(text: str) -> dict[str, set[str]]:
    """Return {library soname: {required symbol versions}}."""
    result: dict[str, set[str]] = {}
    section = re.search(r"Version needs section.*?(?=\nVersion \w+ section|\Z)", text, re.S)
    if not section:
        return result
    current = None
    for line in section.group(0).splitlines():
        file_match = NEED_FILE.search(line)
        if file_match:
            current = file_match.group(1)
            result.setdefault(current, set())
            continue
        name_match = NEED_NAME.search(line)
        if name_match and current is not None:
            result[current].add(name_match.group(1))
    return result


def parse_baseline_sonames(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines()
            if line.strip() and not line.startswith("#")}


def parse_baseline_symvers(text: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise AbiError(f"bad symvers line: {line!r}")
        soname, version = parts
        bucket = result.setdefault(soname, set())
        if version != "-":
            bucket.add(version)
    return result


@dataclass
class Elf:
    path: str
    needed: list[str]
    soname: str | None
    versions: dict[str, set[str]]
    runpath: list[str] = field(default_factory=list)


@dataclass
class Report:
    missing_libraries: list[str]
    missing_symbol_versions: dict[str, list[str]]
    host_libraries: list[str]
    bundled_libraries: list[str]
    glibc_max: str | None
    unsafe_runpaths: list[str]

    @property
    def ok(self) -> bool:
        return not (self.missing_libraries or self.missing_symbol_versions
                    or self.unsafe_runpaths)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "missing_libraries": self.missing_libraries,
            "missing_symbol_versions": self.missing_symbol_versions,
            "host_libraries": self.host_libraries,
            "bundled_libraries": self.bundled_libraries,
            "glibc_max": self.glibc_max,
            "unsafe_runpaths": self.unsafe_runpaths,
        }


def _vkey(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def glibc_max(versions: Iterable[dict[str, set[str]]]) -> str | None:
    best: tuple[int, ...] | None = None
    best_name = None
    for table in versions:
        for names in table.values():
            for name in names:
                if name.startswith("GLIBC_") and name != "GLIBC_PRIVATE":
                    key = _vkey(name)
                    if best is None or key > best:
                        best, best_name = key, name[len("GLIBC_"):]
    return best_name


def check(elfs: list[Elf], host_sonames: set[str],
          host_symvers: dict[str, set[str]]) -> Report:
    """Every NEEDED library must be bundled or on the host, with all symbol versions."""
    bundled = {elf.soname for elf in elfs if elf.soname}
    missing: set[str] = set()
    host_used: set[str] = set()
    bad_versions: dict[str, set[str]] = {}
    unsafe: list[str] = []
    for elf in elfs:
        for entry in elf.runpath:
            for part in entry.split(":"):
                if part and part not in ("$ORIGIN", "$ORIGIN/../lib"):
                    unsafe.append(f"{elf.path}: {part}")
        for lib in elf.needed:
            if lib in bundled:
                continue
            if lib not in host_sonames:
                missing.add(lib)
                continue
            host_used.add(lib)
        for lib, wanted in elf.versions.items():
            if lib in bundled:
                continue
            if lib not in host_symvers:
                # Library known to exist but not profiled: cannot prove versions.
                if lib in host_sonames:
                    bad_versions.setdefault(lib, set()).update(
                        f"unprofiled:{v}" for v in wanted)
                continue
            for version in wanted:
                if version not in host_symvers[lib]:
                    bad_versions.setdefault(lib, set()).add(version)
    return Report(
        missing_libraries=sorted(missing),
        missing_symbol_versions={k: sorted(v) for k, v in sorted(bad_versions.items())},
        host_libraries=sorted(host_used),
        bundled_libraries=sorted(bundled),
        glibc_max=glibc_max(elf.versions for elf in elfs),
        unsafe_runpaths=sorted(set(unsafe)),
    )
