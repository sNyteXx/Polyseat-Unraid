"""Sandbox harness for driving polyseatctl against throwaway /tmp roots.

Every path lives below ``/tmp/polyseat-ctl-*``. Bundles contain stub
``incus``/``incusd`` programs; nothing here starts a real Incus daemon.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from unraid.lab import bundle

CTL = Path(__file__).resolve().parents[1] / "plugin" / "src" / "emhttp" / "scripts" / "polyseatctl"
DATA_LOGICAL = "/mnt/cache/appdata/polyseat"

STUB_INCUS = """#!/bin/bash
VER="@VERSION@"
case "${1:-}" in
  --version) echo "$VER"; exit 0 ;;
  info) [ -e "$INCUS_DIR/FAIL_HEALTH" ] && exit 1; exit 0 ;;
  config) [ "${2:-}" = get ] && { cat "$INCUS_DIR/HTTPS_ADDR" 2>/dev/null; exit 0; } ;;
  admin)
    if [ "${2:-}" = shutdown ]; then
      echo "shutdown-requested $VER" >> "$INCUS_DIR/events.log"
      kill -TERM "$(cat "$INCUS_DIR/stub.pid")" 2>/dev/null
      exit 0
    fi ;;
esac
exit 0
"""

STUB_INCUSD = """#!/usr/bin/env python3
import os, signal, socket, sys, time
VER = "@VERSION@"
if "--version" in sys.argv:
    if @FAIL_SMOKE@:
        sys.stderr.write("error while loading shared libraries: libmissing.so.1\\n")
        sys.exit(127)
    print(VER)
    sys.exit(0)
d = os.environ["INCUS_DIR"]
if @FAIL_START@ or os.path.exists(d + "/FAIL_START"):
    sys.exit(1)
open(d + "/stub.pid", "w").write(str(os.getpid()))
with open(d + "/events.log", "a") as log:
    log.write("start %s\\n" % VER)
sock = d + "/unix.socket"
try:
    os.unlink(sock)
except FileNotFoundError:
    pass
s = socket.socket(socket.AF_UNIX)
s.bind(sock)
s.listen(1)
def term(*_):
    with open(d + "/events.log", "a") as log:
        log.write("stop %s\\n" % VER)
    try:
        os.unlink(sock)
    except FileNotFoundError:
        pass
    sys.exit(0)
signal.signal(signal.SIGTERM, term)
while True:
    time.sleep(0.2)
"""


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


class Sandbox:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="polyseat-ctl-", dir="/tmp"))
        os.chmod(self.root, 0o700)
        self.flash = self.root / "boot/config/plugins/polyseat-unraid"
        self.bundles = self.flash / "bundles"
        self.state = self.flash / "state"
        self.ram = self.root / "usr/local/polyseat-unraid"
        self.run_dir = self.root / "var/run/polyseat-unraid"
        self.log_file = self.root / "var/log/polyseat-unraid/polyseat-unraid.log"
        self.data = self.root / DATA_LOGICAL.lstrip("/")
        self.keydir = self.root / "keys"
        self.work = self.root / "work"
        for path in (self.flash, self.keydir, self.work, self.root / "etc"):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "etc/unraid-version").write_text('version="7.3.3"\n')
        (self.root / "etc/subuid").write_text("")
        (self.root / "etc/subgid").write_text("")
        for part in (self.root / "mnt", self.root / "mnt/cache", self.root / "mnt/cache/appdata"):
            part.mkdir(exist_ok=True)
            part.chmod(0o755)
        self.data.mkdir(exist_ok=True)
        self.data.chmod(0o755)
        self.write_cfg()
        self.key = self.make_key("good")
        self.install_signer(self.key)

    def cleanup(self):
        # Stop any stub daemon we started, then remove the private temp tree.
        pid_file = self.run_dir / "incusd.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 15)
            except (OSError, ValueError):
                pass
        for pid in (self.data / "incus" / "stub.pid",):
            if pid.exists():
                try:
                    os.kill(int(pid.read_text().strip()), 15)
                except (OSError, ValueError):
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    # -- configuration -----------------------------------------------------
    def write_cfg(self, **overrides):
        values = {"SERVICE": "disabled", "DATA_DIR": DATA_LOGICAL, "HEALTH_TIMEOUT": "6"}
        values.update(overrides)
        text = "".join(f'{k}="{v}"\n' for k, v in values.items() if v is not None)
        (self.flash / "polyseat-unraid.cfg").write_text(text)

    def make_key(self, name: str) -> Path:
        key = self.keydir / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        return key

    def install_signer(self, key: Path):
        public = key.with_suffix(".pub").read_text().split()
        line = f'polyseat-unraid-release namespaces="polyseat-unraid-bundle" {public[0]} {public[1]}\n'
        (self.flash / "allowed_signers").write_text(line)

    def sign(self, manifest: Path, key: Path | None = None) -> Path:
        key = key or self.key
        sig = Path(str(manifest) + ".sig")
        sig.unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n",
                        "polyseat-unraid-bundle", str(manifest)], check=True)
        return sig

    # -- bundles -----------------------------------------------------------
    def make_bundle(self, version: str, *, fail_start=False, fail_smoke=False,
                    needed_libs=None, sign_key=None, extra=None) -> dict:
        payload = self.work / f"payload-{version}-{len(list(self.work.iterdir()))}"
        (payload / "bin").mkdir(parents=True)
        (payload / "lib").mkdir()
        incus = STUB_INCUS.replace("@VERSION@", version)
        incusd = (STUB_INCUSD.replace("@VERSION@", version)
                  .replace("@FAIL_SMOKE@", str(fail_smoke)).replace("@FAIL_START@", str(fail_start)))
        (payload / "bin/incus").write_text(incus)
        (payload / "bin/incusd").write_text(incusd)
        for name in ("incus", "incusd"):
            (payload / "bin" / name).chmod(0o755)
        (payload / "lib/libstub.so.1").write_bytes(b"stub-" + version.encode())
        for name, data in (extra or {}).items():
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        out = self.work / f"out-{version}-{len(list(self.work.iterdir()))}"
        out.mkdir()
        archive, manifest = bundle.build(
            payload, out, name="polyseat-incus", version=version, incus_version=version,
            min_unraid="7.2.0", min_glibc="2.31", min_kernel="5.4",
            needed_libs=needed_libs if needed_libs is not None else ["libc.so.6"])
        sig = self.sign(manifest, sign_key)
        return {"archive": archive, "manifest": manifest, "sig": sig,
                "sha": archive.name.removesuffix(".tar.gz"), "version": version}

    def craft_bundle(self, members: list[tuple[tarfile.TarInfo, bytes | None]],
                     files: list[dict], *, version="1.0.0") -> dict:
        """Build an archive from raw members and a hand-written manifest, signed validly."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
            for info, data in members:
                tar.addfile(info, io.BytesIO(data) if data is not None else None)
        archive_bytes = buffer.getvalue()
        sha = hashlib.sha256(archive_bytes).hexdigest()
        out = self.work / f"craft-{sha[:12]}"
        out.mkdir(exist_ok=True)
        archive = out / f"{sha}.tar.gz"
        archive.write_bytes(archive_bytes)
        manifest = out / f"{sha}.manifest.json"
        manifest.write_text(json.dumps({
            "schema": 1, "name": "polyseat-incus", "version": version, "incus_version": version,
            "min_unraid": "7.2.0", "min_glibc": "2.31", "min_kernel": "5.4",
            "needed_libs": ["libc.so.6"], "archive_sha256": sha, "source_date_epoch": 0,
            "files": files}, sort_keys=True))
        return {"archive": archive, "manifest": manifest, "sig": self.sign(manifest), "sha": sha}

    # -- controller --------------------------------------------------------
    def ctl(self, *args, env=None, timeout=120, root=True) -> Result:
        environment = {"PATH": os.environ["PATH"], "LANG": "C"}
        if root:
            environment["POLYSEAT_ROOT"] = str(self.root)
        environment.update(env or {})
        proc = subprocess.run([str(CTL), *map(str, args)], capture_output=True, text=True,
                              env=environment, timeout=timeout)
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def stage(self, b: dict, **kwargs) -> Result:
        return self.ctl("stage", b["archive"], b["manifest"], b["sig"], "--approve", **kwargs)

    def install(self, b: dict, **kwargs) -> Result:
        first = self.stage(b)
        assert first.code == 0, first.text
        return self.ctl("activate", b["sha"], "--approve", **kwargs)

    def status(self) -> dict:
        result = self.ctl("status", "--json")
        return json.loads(result.out) if result.out.strip() else {}

    def pointer(self) -> dict:
        return json.loads((self.state / "pointer.json").read_text())

    def events(self) -> list[str]:
        path = self.data / "incus" / "events.log"
        return path.read_text().splitlines() if path.exists() else []

    def current_link(self) -> str:
        return os.readlink(self.ram / "current")

    def slot_dirs(self) -> list[str]:
        slots = self.ram / "slots"
        return sorted(p.name for p in slots.iterdir()) if slots.exists() else []

    def bundle_files(self) -> list[str]:
        return sorted(p.name for p in self.bundles.iterdir()) if self.bundles.exists() else []

    def tree_fingerprint(self, path: Path) -> str:
        entries = []
        for item in sorted(path.rglob("*")):
            st = item.lstat()
            digest = hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else ""
            entries.append(f"{item.relative_to(path)}|{st.st_mode:o}|{st.st_size}|{digest}")
        return hashlib.sha256("\n".join(entries).encode()).hexdigest()
