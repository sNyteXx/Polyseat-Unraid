"""Owner approval mode (owner decision 2026-11-01): an Unraid admin's click replaces the signed per-session token.

Sandbox directories under /tmp only; no Incus daemon, cgroup or host path is touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL, Sandbox
from unraid.tests.test_desktop_canary import make_manifest, source_ctl

ARCHIVE = b"archive"
BUNDLE = hashlib.sha256(ARCHIVE).hexdigest()  # a stage binds the bytes, so the pointer names their hash
HOST = "c" * 64
ACTIONS = ["activate", "daemon-stage", "polyseatd-play", "seat-image-stage", "stage"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class OwnerFixture(unittest.TestCase):
    """A sandbox root with a flash pointer, a run directory and an owner approval bound to three byte strings."""
    IMAGE = b"seat-image-bytes"
    DAEMON = b"daemon-bundle-bytes"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-owner"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.dir.chmod(0o700)
        self.flash = self.dir / "boot/config/plugins/polyseat-unraid"
        (self.flash / "state").mkdir(parents=True)
        (self.flash / "state/pointer.json").write_text(json.dumps({"current": BUNDLE, "previous": None}))
        (self.flash / "state/pointer.json").chmod(0o600)
        self.run_dir = self.dir / "var/run/polyseat-unraid"
        self.run_dir.mkdir(parents=True)
        self.run_dir.chmod(0o700)
        self.image = self.dir / "image.tar.xz"
        self.image.write_bytes(self.IMAGE)
        self.daemon_file = self.dir / "daemon.tar"
        self.daemon_file.write_bytes(self.DAEMON)
        self.archive = self.dir / "bundle.tar.gz"
        self.archive.write_bytes(ARCHIVE)
        now = int(time.time())
        self.approval = {"schema": 1, "mode": "owner", "host_sha256": HOST, "archive_sha256": BUNDLE,
                         "seat_image_sha256": sha(self.IMAGE), "daemon_sha256": sha(self.DAEMON),
                         "issued_at": now - 5, "expires_at": now + 6 * 3600, "actions": ACTIONS}
        self.stage_runtime()

    def stage_runtime(self, image=None, daemon_sha=None):
        seat = self.dir / "var/lib/polyseat-unraid-seat-image"
        seat.mkdir(parents=True, exist_ok=True)
        (seat / "image").write_bytes(image if image is not None else self.IMAGE)
        dmn = self.dir / "var/lib/polyseat-unraid-daemon"
        dmn.mkdir(parents=True, exist_ok=True)
        (dmn / ".bundle-sha256").write_text((daemon_sha or sha(self.DAEMON)) + "\n")

    def write(self, mode=0o600):
        path = self.run_dir / "owner-approval.json"
        path.unlink(missing_ok=True)
        path.write_text(json.dumps(self.approval))
        path.chmod(mode)
        return path

    def check(self, *args, mode="owner", gate="canary"):
        body = r'''
          POLYSEAT_ROOT="$1"; shift
          POLYSEAT_RELEASE_GATE=$1 POLYSEAT_APPROVAL_MODE=$2; shift 2
          init_paths
          canary_host_sha256() { printf '%s\n' 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'; }
          canary_approval_for "$@"
        '''
        return source_ctl(body, str(self.dir), gate, mode, *map(str, args)).returncode

    def scope(self):
        return [("stage", self.archive), ("activate", BUNDLE), ("seat-image-stage", self.image),
                ("daemon-stage", self.daemon_file), ("polyseatd-play",)]


class OwnerApprovalTests(OwnerFixture):
    def test_the_local_approval_covers_exactly_the_five_actions_with_their_bindings(self):
        self.write()
        for args in self.scope():
            with self.subTest(args=args[0]):
                self.assertEqual(self.check(*args), 0)
        for action in ("start", "canary-smoke", "persist-smoke", "polyseatd-session", "config-set", "uninstall", "boot"):
            with self.subTest(action=action):
                self.assertEqual(self.check(action), 2)

    def test_every_binding_is_by_hash(self):
        self.write()
        other = self.dir / "other"
        other.write_bytes(b"something else")
        self.assertEqual(self.check("stage", other), 2)
        self.assertEqual(self.check("activate", "d" * 64), 2)
        self.assertEqual(self.check("seat-image-stage", other), 2)
        self.assertEqual(self.check("daemon-stage", other), 2)
        self.stage_runtime(image=b"tampered")  # the RAM copy is what a session runs: it is checked again
        self.assertEqual(self.check("polyseatd-play"), 2)
        self.stage_runtime(daemon_sha="e" * 64)
        self.assertEqual(self.check("polyseatd-play"), 2)
        self.stage_runtime()
        self.assertEqual(self.check("polyseatd-play"), 0)
        (self.flash / "state/pointer.json").write_text(json.dumps({"current": "f" * 64, "previous": None}))
        (self.flash / "state/pointer.json").chmod(0o600)
        for args in self.scope()[2:]:  # the runtime bundle in use must be the approved one
            self.assertEqual(self.check(*args), 2, args)

    def test_host_expiry_and_window_are_enforced(self):
        now = int(time.time())
        cases = {"another host": {"host_sha256": "d" * 64}, "expired": {"expires_at": now - 1},
                 "from the future": {"issued_at": now + 60, "expires_at": now + 120},
                 "wrong schema": {"schema": 2}, "wrong mode": {"mode": "signed"},
                 "fractional": {"expires_at": now + 100.5},
                 "bad sha": {"archive_sha256": "ABC"}, "extra scope": {"actions": ACTIONS + ["start"]},
                 "missing scope": {"actions": ACTIONS[:-1]}, "other order": {"actions": list(reversed(ACTIONS))}}
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                self.approval.update(change)
                self.write()
                self.assertEqual(self.check("polyseatd-play"), 2)
        self.setUp()
        for key in list(self.approval):
            with self.subTest(missing=key):
                full = dict(self.approval)
                del self.approval[key]
                self.write()
                self.assertEqual(self.check("polyseatd-play"), 2)
                self.approval = full
        self.approval["extra"] = 1
        self.write()
        self.assertEqual(self.check("polyseatd-play"), 2)

    def test_owner_window_is_the_24_hour_setting_maximum_and_signed_stays_at_six(self):
        now = int(time.time())
        self.approval.update(issued_at=now - 5, expires_at=now - 5 + 86400)
        self.write()
        self.assertEqual(self.check("polyseatd-play"), 0)
        self.approval["expires_at"] = now - 5 + 86401
        self.write()
        self.assertEqual(self.check("polyseatd-play"), 2)
        window = source_ctl('POLYSEAT_APPROVAL_MODE=$1; play_max_window', "signed")
        self.assertEqual(window.stdout.strip(), "21600")
        window = source_ctl('POLYSEAT_APPROVAL_MODE=$1; play_max_window', "owner")
        self.assertEqual(window.stdout.strip(), "86400")

    def test_the_file_must_be_private_to_root_and_a_real_file(self):
        for mode in (0o644, 0o640, 0o660):
            with self.subTest(mode=oct(mode)):
                self.write(mode)
                self.assertEqual(self.check("polyseatd-play"), 2)
        path = self.write()
        real = self.run_dir / "real.json"
        path.rename(real)
        path.symlink_to(real)
        self.assertEqual(self.check("polyseatd-play"), 2)
        path.unlink()
        real.unlink()
        self.assertEqual(self.check("polyseatd-play"), 2)  # none at all: nothing is approved
        self.write()
        self.assertEqual(self.check("polyseatd-play"), 0)

    def test_the_signed_path_ignores_an_owner_approval_and_owner_mode_ignores_a_signed_token(self):
        self.write()
        self.assertEqual(self.check("polyseatd-play", mode="signed"), 2)  # no canary-approval.json: the signed path is unchanged
        os.remove(self.run_dir / "owner-approval.json")
        (self.flash / "canary-approval.json").write_text(json.dumps({"schema": 1}))
        (self.flash / "canary-approval.json").chmod(0o600)
        (self.flash / "canary-approval.json.sig").write_text("sig")
        (self.flash / "canary-approval.json.sig").chmod(0o600)
        (self.flash / "canary_allowed_signers").write_text("x")
        self.assertEqual(self.check("polyseatd-play", mode="owner"), 2)

    def test_the_approval_reaches_every_reader_of_the_budget_through_one_variable(self):
        res = source_ctl('POLYSEAT_ROOT=$1 POLYSEAT_APPROVAL_MODE=$2; init_paths; printf "%s\\n" "$CANARY_APPROVAL"',
                         str(self.dir), "owner")
        self.assertEqual(res.stdout.strip(), f"{self.run_dir}/owner-approval.json")
        res = source_ctl('POLYSEAT_ROOT=$1 POLYSEAT_APPROVAL_MODE=$2; init_paths; printf "%s\\n" "$CANARY_APPROVAL"',
                         str(self.dir), "signed")
        self.assertEqual(res.stdout.strip(), f"{self.flash}/canary-approval.json")


class ModeGateTests(unittest.TestCase):
    def gate(self, mode, gate, cmd="stage", *extra):
        script = ('POLYSEAT_APPROVAL_MODE=$1 POLYSEAT_RELEASE_GATE=$2 ROOT=""; id() { printf "0\\n"; }; '
                  'canary_approval_for() { echo APPROVAL-CHECKED; }; shift 2; require_mutation_allowed "$@"; echo ALLOWED')
        return source_ctl(script, mode, gate, cmd, *extra)

    def test_owner_mode_exists_only_with_the_canary_gate(self):
        for gate in ("closed", "test-host", "open", "bogus"):
            with self.subTest(gate=gate):
                res = self.gate("owner", gate, "polyseatd-status")
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertIn("not allowed with release gate", res.stderr)
        for mode in ("", "Owner", "owner ", "both"):
            with self.subTest(mode=mode):
                self.assertEqual(self.gate(mode, "canary", "polyseatd-status").returncode, 2)
        self.assertIn("ALLOWED", self.gate("owner", "canary", "polyseatd-status").stdout)
        self.assertIn("ALLOWED", self.gate("signed", "canary", "polyseatd-status").stdout)

    def test_token_free_verbs_and_the_ones_that_still_need_an_approval(self):
        for cmd in ("play-status", "play-stop", "polyseatd-status", "polyseatd-stop", "stop", "recover", "uninstall"):
            for mode in ("signed", "owner"):
                with self.subTest(cmd=cmd, mode=mode):
                    out = self.gate(mode, "canary", cmd).stdout
                    self.assertIn("ALLOWED", out)
                    self.assertNotIn("APPROVAL-CHECKED", out)
        for cmd in ("stage", "activate", "seat-image-stage", "daemon-stage", "polyseatd-play", "polyseatd-session",
                    "start", "restart", "update", "rollback"):
            for mode in ("signed", "owner"):
                with self.subTest(cmd=cmd, mode=mode):
                    self.assertIn("APPROVAL-CHECKED", self.gate(mode, "canary", cmd).stdout)

    def test_play_start_exists_only_in_owner_mode_and_checks_no_approval(self):
        res = self.gate("owner", "canary", "play-start")
        self.assertIn("ALLOWED", res.stdout)
        self.assertNotIn("APPROVAL-CHECKED", res.stdout)
        res = self.gate("signed", "canary", "play-start")
        self.assertEqual(res.returncode, 2)
        self.assertIn("owner-mode build", res.stderr)

    def test_settings_changes_need_no_token_in_owner_mode_but_keep_the_whitelist(self):
        for cmd in ("config-set", "config-reset"):
            self.assertIn("APPROVAL-CHECKED", self.gate("signed", "canary", cmd, "SERVICE=disabled").stdout)
        self.assertNotIn("APPROVAL-CHECKED", self.gate("owner", "canary", "config-set", "SERVICE=disabled").stdout)
        self.assertNotIn("APPROVAL-CHECKED", self.gate("owner", "canary", "config-reset").stdout)
        script = ('POLYSEAT_APPROVAL_MODE=owner POLYSEAT_RELEASE_GATE=canary ROOT=""; id() { printf "0\\n"; }; '
                  'SCHEMA=$1; shift; require_mutation_allowed config-set "$@"; echo ALLOWED')
        schema = str(CTL.parent.parent / "schema.json")
        for good in (["SERVICE=disabled"], ["ACCEPT_NO_LSM=yes"], ["PLAY_CPU_PERCENT=50"], ["LIBRARY_PATH=/mnt/a/b"]):
            with self.subTest(good=good):
                self.assertIn("ALLOWED", source_ctl(script, schema, *good).stdout)
        for bad in (["SERVICE=enabled"], ["HEALTH_TIMEOUT=30"], ["DATA_DIR=/mnt/cache/x"], ["PLAY_CPU_PERCENT=500"],
                    ["SERVICE=disabled", "SUBID_START=100000"]):
            with self.subTest(bad=bad):
                res = source_ctl(script, schema, *bad)
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertNotIn("ALLOWED", res.stdout)

    def test_boot_in_owner_mode_needs_no_approval(self):
        self.assertNotIn("APPROVAL-CHECKED", self.gate("owner", "canary", "boot").stdout)
        self.assertIn("APPROVAL-CHECKED", self.gate("signed", "canary", "boot").stdout)

    def test_status_names_the_mode(self):
        text = CTL.read_text()
        self.assertEqual(len(re.findall(r"^POLYSEAT_APPROVAL_MODE=", text, re.M)), 1)
        self.assertIn("approval_mode:$mode", text)


class SettingsTests(unittest.TestCase):
    def test_a_value_may_contain_an_equals_sign_and_survives_other_changes(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        value = "seat1=0-7;seat2=8-15"
        self.assertEqual(sb.ctl("config-set", f"PLAY_SEAT_CPUS={value}").code, 0)
        self.assertEqual(sb.ctl("config-set", "PLAY_SESSION_HOURS=12").code, 0)

        def entry(key):
            return next(e for e in json.loads(sb.ctl("config-get").out) if e["key"] == key)

        self.assertEqual(entry("PLAY_SEAT_CPUS")["value"], value)
        self.assertEqual(entry("PLAY_SESSION_HOURS")["value"], "12")
        self.assertEqual((entry("PLAY_CPU_PERFORMANCE")["value"], entry("PLAY_SESSION_HOURS")["default"]), ("no", "6"))
        res = source_ctl('init_paths; CFG=$2; cfg_get PLAY_SEAT_CPUS x', "-", str(sb.flash / "polyseat-unraid.cfg"))
        self.assertEqual(res.stdout.strip(), value)

    def test_new_play_settings_are_locked_while_a_session_runs(self):
        schema = json.loads((CTL.parent.parent / "schema.json").read_text())
        for key in ("PLAY_SESSION_HOURS", "PLAY_CPU_PERFORMANCE", "PLAY_SEAT_CPUS"):
            self.assertTrue(schema[key]["locked_while_running"], key)
            self.assertEqual(schema[key]["tab"], "play")


UNSQUASHFS = b"#!/bin/sh\necho 'unsquashfs version 4.6'\nexit 1\n"


class PlayStartTests(unittest.TestCase):
    """play-start against a real sandbox: signed files on the flash, real verification, activation and staging.
    Only the things that need a host (GPU preflight, the LAN, the detached launch) are replaced."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        public = self.sb.key.with_suffix(".pub").read_text().split()
        (self.sb.flash / "allowed_signers").write_text(
            f'polyseat-unraid-release namespaces="polyseat-unraid-bundle,polyseat-unraid-seat-image,polyseat-unraid-daemon" '
            f'{public[0]} {public[1]}\n')
        self.bundle = self.put_bundle("1.0.0")
        self.seat_image = self.sb.flash / "seat/polyseat-desktop.tar.xz"
        self.seat_image.parent.mkdir()
        self.seat_image.write_bytes(os.urandom(4096))
        manifest = self.sb.flash / "seat/seat.manifest.json"
        self.assertEqual(make_manifest(self.seat_image, manifest).returncode, 0)
        self.sign(manifest, "polyseat-unraid-seat-image")
        self.put_daemon()
        self.launches = self.sb.root / "launches.log"

    def sign(self, path, namespace):
        Path(str(path) + ".sig").unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.sb.key), "-n", namespace, str(path)],
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def put_bundle(self, version, **kwargs):
        b = self.sb.make_bundle(version, extra={"tools/unsquashfs": UNSQUASHFS}, executable=("tools/unsquashfs",), **kwargs)
        self.sb.bundles.mkdir(parents=True, exist_ok=True)
        shutil.copy(b["archive"], self.sb.bundles / f"{b['sha']}.tar.gz")
        shutil.copy(b["manifest"], self.sb.bundles / f"{b['sha']}.manifest.json")
        shutil.copy(b["sig"], self.sb.bundles / f"{b['sha']}.manifest.json.sig")
        return b

    def put_daemon(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
            binary = b"daemon-binary"
            for name, data, mode in (("MANIFEST", (sha(binary) + "  bin/polyseatd\n").encode(), 0o644),
                                     ("bin/polyseatd", binary, 0o755)):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
        self.daemon = self.sb.flash / "daemon/polyseat-daemon.tar"
        self.daemon.parent.mkdir(exist_ok=True)
        self.daemon.write_bytes(raw.getvalue())
        self.sign(self.daemon, "polyseat-unraid-daemon")
        self.daemon_sha = sha(raw.getvalue())

    def start(self, pre="", **env):
        script = r"""
          POLYSEAT_ROOT="$1"; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner
          init_paths; SCHEMA="$2"; LAUNCHES="$3"
          mkdir -p "$RUN_DIR" "$LOG_DIR"; LOG_ENABLED=1
          me=$(id -u); stat() { local out; out=$(command stat "$@") || return; printf '%s\n' "${out//$me /0 }"; }
          canary_host_sha256() { printf '%s\n' 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'; }
          polyseatd_precheck() { return "${PRECHECK_RC:-0}"; }
          gpu_preflight_run() { GPU_RES=("FAIL	gpu busy"); return "${GPU_RC:-0}"; }
          play_launch_detached() { echo "launched approval=$([[ -f $OWNER_APPROVAL ]] && echo yes)" >> "$LAUNCHES"; }
          ensure_state_dirs
          """ + pre + r"""
          POLYSEATD_SHA256=""
          cmd_play_start
        """
        environment = {"PATH": os.environ["PATH"], **env}
        proc = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "play", str(CTL), str(self.sb.root),
                               str(CTL.parent.parent / "schema.json"), str(self.launches)],
                              env=environment, capture_output=True, text=True)
        return proc

    def approval(self):
        return json.loads((self.sb.run_dir / "owner-approval.json").read_text())

    def log(self):
        return self.sb.log_file.read_text() if self.sb.log_file.exists() else ""

    def launched(self):
        return self.launches.read_text().splitlines() if self.launches.exists() else []

    def assert_refused(self, proc, needle):
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(needle, proc.stderr)
        self.assertEqual(self.launched(), [])
        self.assertFalse((self.sb.run_dir / "owner-approval.json").exists(), "a refused start leaves no approval")

    def test_first_start_verifies_activates_stages_approves_and_launches(self):
        self.sb.write_cfg(PLAY_SESSION_HOURS="12")
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("play session starting (up to 12 h)", proc.stdout)
        self.assertEqual(self.launched(), ["launched approval=yes"])
        a = self.approval()
        self.assertEqual((a["archive_sha256"], a["seat_image_sha256"], a["daemon_sha256"]),
                         (self.bundle["sha"], sha(self.seat_image.read_bytes()), self.daemon_sha))
        self.assertEqual((a["schema"], a["mode"], a["actions"]), (1, "owner", ACTIONS))
        self.assertEqual(a["expires_at"] - a["issued_at"], 12 * 3600)
        self.assertEqual(oct((self.sb.run_dir / "owner-approval.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.sb.pointer()["current"], self.bundle["sha"])
        self.assertEqual(self.sb.current_link(), f"slots/{self.bundle['sha']}")
        self.assertEqual((self.sb.root / "var/lib/polyseat-unraid-seat-image/image").read_bytes(), self.seat_image.read_bytes())
        self.assertEqual((self.sb.root / "var/lib/polyseat-unraid-daemon/.bundle-sha256").read_text().strip(), self.daemon_sha)
        self.assertEqual(self.sb.status().get("state"), "ACTIVE")  # nothing was started by the controller itself

    def test_the_default_is_six_hours_and_the_setting_is_capped_at_the_schema_maximum(self):
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.approval()["expires_at"] - self.approval()["issued_at"], 6 * 3600)
        (self.sb.run_dir / "owner-approval.json").unlink()
        self.sb.write_cfg(PLAY_SESSION_HOURS="24")
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.approval()["expires_at"] - self.approval()["issued_at"], 86400)
        (self.sb.run_dir / "owner-approval.json").unlink()
        for bad in ("0", "25", "6h", ""):
            self.sb.write_cfg(PLAY_SESSION_HOURS=bad)
            proc = self.start()
            if bad == "":
                self.assertEqual(proc.returncode, 0)  # an empty value falls back to the default
                (self.sb.run_dir / "owner-approval.json").unlink()
                continue
            self.assert_refused_keep_launches(proc, "PLAY_SESSION_HOURS")

    def assert_refused_keep_launches(self, proc, needle):
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(needle, proc.stderr)
        self.assertFalse((self.sb.run_dir / "owner-approval.json").exists())

    def test_a_second_start_reuses_what_is_current_and_staged_and_clears_the_records_of_the_last_session(self):
        self.assertEqual(self.start().returncode, 0)
        (self.sb.run_dir / "owner-approval.json").unlink()  # owner_session_end
        for name in ("canary-session.json", "canary-guardian.pid", "canary-emergency-stop", "polyseatd-stop", "polyseatd.pid"):
            (self.sb.run_dir / name).write_text("x")
        evidence = self.sb.run_dir / "play-evidence"
        evidence.mkdir()
        (evidence / "status-start.json").write_text("{}")
        before = self.sb.log_file.read_text().count("activated bundle")
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sb.log_file.read_text().count("activated bundle"), before)  # not activated again
        self.assertIn("seat image already staged", self.log())
        self.assertIn("daemon bundle already staged", self.log())
        for name in ("canary-session.json", "canary-guardian.pid", "canary-emergency-stop", "polyseatd-stop", "polyseatd.pid"):
            self.assertFalse((self.sb.run_dir / name).exists(), name)
        self.assertTrue((self.sb.run_dir / "play-evidence.last/status-start.json").exists())
        self.assertFalse(evidence.exists())
        self.assertEqual(len(self.launched()), 2)

    def test_after_a_reboot_the_ram_runtime_is_rebuilt_from_the_flash_and_nothing_else_is_assumed(self):
        self.assertEqual(self.start().returncode, 0)
        (self.sb.run_dir / "owner-approval.json").unlink()
        shutil.rmtree(self.sb.ram)  # RAM is gone; the pointer on the flash stays
        shutil.rmtree(self.sb.root / "var/lib/polyseat-unraid-seat-image")
        shutil.rmtree(self.sb.root / "var/lib/polyseat-unraid-daemon")
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sb.current_link(), f"slots/{self.bundle['sha']}")
        self.assertTrue((self.sb.root / "var/lib/polyseat-unraid-seat-image/image").exists())
        self.assertTrue((self.sb.root / "var/lib/polyseat-unraid-daemon/bin/polyseatd").exists())

    def test_the_newest_complete_bundle_wins_and_an_ambiguous_one_refuses(self):
        self.assertEqual(self.start().returncode, 0)
        (self.sb.run_dir / "owner-approval.json").unlink()
        newer = self.put_bundle("1.10.0")  # 1.10.0 is newer than 1.9.0 and 1.0.0 (version sort, not text)
        self.put_bundle("1.9.0")
        proc = self.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sb.pointer()["current"], newer["sha"])
        self.assertEqual(self.sb.pointer()["previous"], self.bundle["sha"])
        (self.sb.run_dir / "owner-approval.json").unlink()
        twin = self.sb.make_bundle("1.10.0", extra={"tools/unsquashfs": UNSQUASHFS + b"#twin\n"}, executable=("tools/unsquashfs",))
        for suffix, src in ((".tar.gz", "archive"), (".manifest.json", "manifest"), (".manifest.json.sig", "sig")):
            shutil.copy(twin[src], self.sb.bundles / f"{twin['sha']}{suffix}")
        self.assertIn("claim the newest version", self.start().stderr)

    def test_an_incomplete_triple_is_not_a_candidate(self):
        (self.sb.bundles / f"{self.bundle['sha']}.manifest.json.sig").unlink()
        self.assert_refused(self.start(), "no complete runtime bundle")

    def test_nothing_is_started_over_a_running_daemon_or_unrestored_host_changes(self):
        self.assert_refused(self.start(pre="svc_pid() { echo 4242; }"), "Incus is already running")
        for name, needle in (("play-host.json", "host changes not restored"), ("polyseatd-status.json", "earlier session left"),
                             ("play-console.json", "earlier session left")):
            (self.sb.run_dir).mkdir(parents=True, exist_ok=True)
            (self.sb.run_dir / name).write_text("{}")
            self.assert_refused(self.start(), needle)
            (self.sb.run_dir / name).unlink()
        (self.sb.run_dir / "canary-session.json.reason").write_text("expired\n")
        self.assert_refused(self.start(), "watchdog ended the previous session (expired)")
        (self.sb.run_dir / "canary-session.json.reason").unlink()
        self.assert_refused(self.start(pre='CANARY_CGROUP=$ROOT'), "cgroup still exists")
        self.assert_refused(self.start(pre='play_evidence_rotate() { return 1; }'), "evidence directory")
        self.assertEqual(self.start().returncode, 0)

    def test_a_running_polyseatd_is_refused_but_a_stale_pid_file_is_not(self):
        self.sb.run_dir.mkdir(parents=True, exist_ok=True)
        (self.sb.run_dir / "polyseatd.pid").write_text("4242\n")
        self.assert_refused(self.start(pre="polyseatd_alive() { return 0; }"), "polyseatd is already running (pid 4242)")
        self.assertEqual(self.start(pre="polyseatd_alive() { return 1; }").returncode, 0)

    def test_every_flash_file_is_required_and_the_signatures_are_the_release_keys(self):
        for path in (self.seat_image, self.seat_image.parent / "seat.manifest.json", self.seat_image.parent / "seat.manifest.json.sig",
                     self.daemon):
            data = path.read_bytes()
            path.unlink()
            self.assert_refused(self.start(), "missing on the flash drive")
            path.write_bytes(data)
        archive = self.sb.bundles / f"{self.bundle['sha']}.tar.gz"
        data = archive.read_bytes()
        archive.write_bytes(data + b"x")
        self.assert_refused(self.start(), "failed verification")
        archive.write_bytes(data)
        # the bundle's signature is no seat image signature, and a changed image is no signed image
        manifest = self.seat_image.parent / "seat.manifest.json"
        self.sign(manifest, "polyseat-unraid-bundle")
        proc = self.start()
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertIn("staging the seat image failed", proc.stderr)
        self.assertFalse((self.sb.run_dir / "owner-approval.json").exists())
        self.sign(manifest, "polyseat-unraid-seat-image")
        Path(str(self.daemon) + ".sig").write_bytes(b"not a signature")
        proc = self.start()
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertIn("staging the daemon bundle failed", proc.stderr)
        self.assertEqual(self.launched(), [])

    def test_the_prechecks_of_the_session_refuse_before_anything_is_launched(self):
        self.assert_refused(self.start(PRECHECK_RC="1"), "polyseatd prechecks failed")
        self.assert_refused(self.start(GPU_RC="1"), "GPU is not available")
        self.assertIn("gpu-preflight: gpu busy", self.log())
        self.assert_refused(self.start(pre="play_limits() { return 1; }"), "PLAY_CPU_PERCENT or PLAY_MEMORY_MB")
        self.assertEqual(self.start().returncode, 0)

    def test_an_unfinished_transaction_blocks_the_start(self):
        (self.sb.state).mkdir(parents=True, exist_ok=True)
        (self.sb.state / "journal.json").write_text(json.dumps(
            {"phase": "activating", "target": self.bundle["sha"], "previous": None, "was_running": False}))
        (self.sb.state / "journal.json").chmod(0o600)
        self.assert_refused(self.start(), "unfinished transaction")

    def test_a_failure_after_the_approval_took_it_back(self):
        self.assert_refused(self.start(pre="cmd_seat_image_stage() { return 1; }"), "staging the seat image failed")
        self.assertTrue((self.sb.state / "pointer.json").exists())  # the bundle was activated; that is not undone

    def test_the_session_is_launched_detached_with_the_lock_handed_over(self):
        fake = self.sb.root / "fake-ctl"
        fake.write_text('#!/bin/bash\n{ echo "args=$*"; echo "detached=$POLYSEAT_DETACHED inherited=$POLYSEAT_LOCK_INHERITED"; '
                        '[[ /proc/self/fd/9 -ef "$LOCK" ]] && echo fd9-is-the-lock; sleep 1.5; echo finished; } >> "$OUT" 2>&1\n')
        fake.chmod(0o755)
        out = self.sb.root / "fake.out"
        script = r"""
          POLYSEAT_ROOT="$1"; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner
          init_paths; mkdir -p "$RUN_DIR" "$LOG_DIR"; CTL_SELF=$2; export OUT=$3 LOCK=$LOCKFILE
          exec 9> "$LOCKFILE"; flock 9
          play_launch_detached; echo "parent-done rc=$?"
        """
        proc = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "launch", str(CTL), str(self.sb.root),
                               str(fake), str(out)], capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=20)
        self.assertIn("parent-done rc=0", proc.stdout, proc.stderr)
        lock = self.sb.run_dir / "lock"
        busy = subprocess.run(["flock", "-n", str(lock), "true"]).returncode  # the parent has exited; the child holds it
        self.assertNotEqual(busy, 0)
        time.sleep(2)
        self.assertEqual(subprocess.run(["flock", "-n", str(lock), "true"]).returncode, 0)
        text = out.read_text()
        self.assertIn("args=polyseatd-play --approve", text)
        self.assertIn("detached=1 inherited=1", text)
        self.assertIn("fd9-is-the-lock", text)
        self.assertIn("finished", text)

    def test_the_lock_is_taken_over_only_from_the_lock_file_itself(self):
        script = r"""
          POLYSEAT_ROOT="$1"; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner
          init_paths; mkdir -p "$RUN_DIR" "$LOG_DIR"
          if [[ $2 == lock ]]; then exec 9> "$LOCKFILE"; else exec 9> "$RUN_DIR/other"; fi
          flock -n "$LOCKFILE" true && echo free-before
          acquire_lock && echo acquired
        """
        env = {"PATH": os.environ["PATH"], "POLYSEAT_LOCK_INHERITED": "1"}
        ok = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(self.sb.root), "lock"],
                            capture_output=True, text=True, env=env, timeout=20)
        self.assertIn("acquired", ok.stdout, ok.stderr)
        # descriptor 9 pointing elsewhere is not our lock: the controller reopens the lock file as usual
        holder = subprocess.Popen(["flock", str(self.sb.run_dir / "lock"), "sleep", "6"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        time.sleep(0.5)
        blocked = subprocess.run(["timeout", "2", "bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL),
                                  str(self.sb.root), "other"], capture_output=True, text=True, env=env)
        self.assertEqual(blocked.returncode, 124, blocked.stdout)
        self.assertNotIn("acquired", blocked.stdout)


class PlayStopTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.sb.run_dir.mkdir(parents=True)
        self.sb.log_file.parent.mkdir(parents=True)

    def stop(self, *args, timeout=30):
        script = r"""
          POLYSEAT_ROOT="$1"; shift; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner
          init_paths; cmd_play_stop "$@"; echo "rc=$?"
        """
        return subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(self.sb.root), *args],
                              capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=timeout)

    def test_it_is_cmd_polyseatd_stop_without_a_wait(self):
        res = self.stop()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no polyseatd session is running", res.stderr)
        (self.sb.run_dir / "polyseatd-status.json").write_text("{}")
        self.assertEqual(self.stop().returncode, 0)
        self.assertTrue((self.sb.run_dir / "polyseatd-stop").exists())

    def test_the_wait_form_takes_exactly_two_arguments_in_range(self):
        for args in (("--wait",), ("--wait", "0"), ("--wait", "601"), ("--wait", "x"), ("--wait", "5", "6"), ("5",), ("--now",)):
            with self.subTest(args=args):
                res = self.stop(*args)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("usage: play-stop", res.stderr)

    def test_nothing_running_returns_at_once(self):
        start = time.time()
        res = self.stop("--wait", "30")
        self.assertIn("rc=0", res.stdout)
        self.assertLess(time.time() - start, 5)
        self.assertIn("no session or controller operation is running", self.sb.log_file.read_text())

    def test_a_running_session_is_asked_to_stop_and_waited_for(self):
        lock = self.sb.run_dir / "lock"
        # a stand-in session: holds the lock until it sees the stop request, then needs a moment to restore the host
        session = subprocess.Popen(["flock", str(lock), "bash", "-c",
                                    f'until [[ -e {self.sb.run_dir}/polyseatd-stop ]]; do sleep 0.2; done; sleep 1.5'])
        self.addCleanup(lambda: (session.kill(), session.wait()))
        time.sleep(0.5)
        start = time.time()
        res = self.stop("--wait", "30")
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertGreaterEqual(time.time() - start, 1.5)
        self.assertEqual(session.wait(timeout=5), 0)
        text = self.sb.log_file.read_text()
        self.assertIn("asking it to stop and waiting up to 30s", text)
        self.assertIn("play-stop: ended after", text)

    def test_the_wait_is_bounded(self):
        holder = subprocess.Popen(["flock", str(self.sb.run_dir / "lock"), "sleep", "30"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        time.sleep(0.5)
        start = time.time()
        res = self.stop("--wait", "3")
        self.assertIn("rc=1", res.stdout)
        self.assertGreaterEqual(time.time() - start, 3)
        self.assertLess(time.time() - start, 12)
        self.assertIn("the emergency stop follows", self.sb.log_file.read_text())


class CpuSettingsTests(unittest.TestCase):
    """Owner GO 2026-11-01: the energy profile may be `performance` during a session and is restored exactly; seats may be
    pinned to CPUs (i7-14700K: P-cores 0-15, E-cores 16-27)."""
    EPP = {0: "balance_performance", 1: "balance_performance", 2: "power", 3: "default"}

    def host(self, perf="yes", epp=None, sysrq="1"):
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-epp"))
        self.addCleanup(shutil.rmtree, d)
        (d / "proc/sys/kernel").mkdir(parents=True)
        (d / "proc/sys/kernel/sysrq").write_text(sysrq + "\n")
        (d / "etc/udev/rules.d").mkdir(parents=True)
        (d / "usr/local/lib").mkdir(parents=True)
        cpu = d / "sys/devices/system/cpu"
        for name in ("cpufreq", "cpuidle"):  # neighbours of the cpuN directories that must never be touched
            (cpu / name).mkdir(parents=True)
            (cpu / name / "energy_performance_preference").write_text("do-not-touch\n")
        for n, value in (self.EPP if epp is None else epp).items():
            (cpu / f"cpu{n}/cpufreq").mkdir(parents=True)
            (cpu / f"cpu{n}/cpufreq/energy_performance_preference").write_text(value + "\n")
        (cpu / "cpu9").mkdir()  # an offline CPU without cpufreq
        bundle = d / "bundle"
        (bundle / "bin").mkdir(parents=True); (bundle / "rules").mkdir(); (bundle / "lib/polyseat").mkdir(parents=True)
        (bundle / "bin/polyseat-console").write_text('#!/bin/sh\n[ "$1" = off ] && echo "{}" > "$3"; [ "$1" = restore ] && rm -f "$2"; exit 0\n')
        (bundle / "bin/polyseat-console").chmod(0o755)
        (bundle / "rules/72-polyseat-hide.rules").write_text("# rule\n")
        (bundle / "lib/polyseat/device_owner.udev").write_text("#!@BUNDLE@/x\n")
        (d / "run").mkdir()
        (d / "cfg").write_text(f'PLAY_CPU_PERFORMANCE="{perf}"\n')
        return d

    def run_host(self, d, body):
        script = r"""
          POLYSEAT_RELEASE_GATE=canary ROOT="$1" RUN_DIR="$1/run" POLYSEATD_DIR="$1/bundle" CFG="$1/cfg" SCHEMA="$2" LOG_DIR="$1/log"
          sandbox() { return 0; }; ci_real_system() { return 1; }
          epps() { for n in 0 1 2 3; do printf '%s=%s ' "$n" "$(cat "$1/sys/devices/system/cpu/cpu$n/cpufreq/energy_performance_preference" 2>/dev/null)"; done; echo; }
        """ + body
        return source_ctl(script, str(d), str(CTL.parent.parent / "schema.json"), env={"PATH": "/usr/bin:/bin"})

    def test_the_profile_is_recorded_set_to_performance_and_put_back_exactly(self):
        d = self.host()
        res = self.run_host(d, r"""
          play_host_on || exit 7
          epps "$1"; jq -c .epp "$RUN_DIR/play-host.json"
          play_host_off || exit 9
          epps "$1"; if [[ -e $RUN_DIR/play-host.json ]]; then echo STATE-LEFT; fi
        """)
        self.assertEqual(res.returncode, 0, res.stderr)
        on, recorded, off = (line.strip() for line in res.stdout.splitlines()[:3])
        self.assertEqual(on, "0=performance 1=performance 2=performance 3=performance")
        self.assertEqual(json.loads(recorded), {str(n): v for n, v in self.EPP.items()})  # CPU 9 and the neighbours: absent
        self.assertEqual(off, "0=balance_performance 1=balance_performance 2=power 3=default")
        self.assertNotIn("STATE-LEFT", res.stdout)
        cpu = d / "sys/devices/system/cpu"
        self.assertEqual((cpu / "cpufreq/energy_performance_preference").read_text(), "do-not-touch\n")
        self.assertEqual((cpu / "cpuidle/energy_performance_preference").read_text(), "do-not-touch\n")
        self.assertFalse((cpu / "cpu9/cpufreq").exists())

    def test_off_by_default_touches_nothing_and_records_nothing(self):
        for perf in ("no", None):
            d = self.host(perf=perf or "no")
            if perf is None:
                (d / "cfg").unlink()  # no settings file at all: the schema default
            res = self.run_host(d, r"""
              play_host_on || exit 7
              epps "$1"; jq -c .epp "$RUN_DIR/play-host.json"; play_host_off || exit 9
            """)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout.splitlines()[0], "0=balance_performance 1=balance_performance 2=power 3=default ")
            self.assertEqual(res.stdout.splitlines()[1], "{}")

    def test_an_unreadable_or_odd_value_refuses_before_anything_is_changed(self):
        for label, change in (("not a profile name", lambda f: f.write_text("Perf ormance\n")),
                              ("empty", lambda f: f.write_text("\n")),
                              ("a link", lambda f: (f.unlink(), f.symlink_to("/etc/hostname"))),
                              ("a directory", lambda f: (f.unlink(), f.mkdir()))):
            with self.subTest(label):
                d = self.host()
                change(d / "sys/devices/system/cpu/cpu2/cpufreq/energy_performance_preference")
                res = self.run_host(d, 'play_host_on && echo STARTED; cat "$1/proc/sys/kernel/sysrq"; [[ -e $RUN_DIR/play-host.json ]] && echo STATE')
                self.assertNotIn("STARTED", res.stdout)
                self.assertNotIn("STATE", res.stdout)
                self.assertEqual(res.stdout.strip().splitlines()[-1], "1")  # sysrq untouched
                self.assertIn("cannot read the CPUs' energy profile", res.stderr)
                self.assertEqual((d / "sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference").read_text(),
                                 "balance_performance\n")

    def test_a_host_without_the_files_is_skipped_cleanly_and_a_bad_setting_refuses(self):
        d = self.host(epp={})
        res = self.run_host(d, "play_host_on || exit 7; jq -c .epp \"$RUN_DIR/play-host.json\"; play_host_off || exit 9")
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "{}"), res.stderr)
        self.assertIn("the profile is left alone", res.stderr)
        d = self.host(perf="maybe")
        res = self.run_host(d, "play_host_on")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("PLAY_CPU_PERFORMANCE 'maybe' is not usable", res.stderr)

    def test_a_restore_that_fails_keeps_the_record_and_a_tampered_one_writes_nowhere_else(self):
        d = self.host()
        res = self.run_host(d, r"""
          play_host_on || exit 7
          rm -r "$1/sys/devices/system/cpu/cpu2/cpufreq"       # the CPU went away while the session ran
          play_host_off && echo RESTORED
          [[ -e $RUN_DIR/play-host.json ]] && echo STATE-KEPT; epps "$1"
        """)
        self.assertNotIn("RESTORED", res.stdout)
        self.assertIn("STATE-KEPT", res.stdout)
        self.assertIn("0=balance_performance 1=balance_performance 2= 3=default", res.stdout)  # the others went back anyway
        self.assertIn("energy profile was not restored exactly", res.stderr)
        d = self.host()
        res = self.run_host(d, r"""
          play_host_on || exit 7
          jq -c '.epp = {"../../../../outside": "x", "1": "a b", "x": "power", "3": "power"}' "$RUN_DIR/play-host.json" > "$RUN_DIR/t" && mv "$RUN_DIR/t" "$RUN_DIR/play-host.json"
          play_host_off && echo RESTORED; epps "$1"
        """)
        self.assertNotIn("RESTORED", res.stdout)
        self.assertIn("3=power", res.stdout)  # the one well-formed entry was honoured, the others were not
        self.assertFalse(list(d.glob("**/outside*")))

    # -- pinning ------------------------------------------------------------------------------------------------
    def parse(self, value, cpuset="0-27"):
        res = source_ctl(r"""
          CANARY_HOST_CPUSET=$3 SCHEMA="$2"
          play_seat_cpus_parse "$1" || { echo REFUSED; exit 0; }
          printf 'all=%s\n' "$PLAY_PIN_ALL"; for k in $(printf '%s\n' "${!PLAY_PIN_SEAT[@]}" | sort); do printf '%s=%s\n' "$k" "${PLAY_PIN_SEAT[$k]}"; done
        """, value, str(CTL.parent.parent / "schema.json"), cpuset)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.strip().splitlines()

    def test_seat_cpus_are_checked_against_the_hosts_cpus_and_normalised_like_the_kernel_prints_them(self):
        self.assertEqual(self.parse(""), ["all="])
        self.assertEqual(self.parse("0-15"), ["all=0-15"])
        self.assertEqual(self.parse("16-27"), ["all=16-27"])
        self.assertEqual(self.parse("3,1,2,0,8,9,11,5"), ["all=0-3,5,8-9,11"])
        self.assertEqual(self.parse("007-010"), ["all=7-10"])
        self.assertEqual(self.parse("4,5"), ["all=4-5"])
        self.assertEqual(self.parse("5"), ["all=5"])
        self.assertEqual(self.parse("seat1=0-7;seat2=8-15"), ["all=", "seat1=0-7", "seat2=8-15"])
        self.assertEqual(self.parse("seat-2=16,17;a=0"), ["all=", "a=0", "seat-2=16-17"])
        self.assertEqual(self.parse("0-3,8", cpuset="0-3,8-11"), ["all=0-3,8"])
        for bad, cpuset in (("0-28", "0-27"), ("28", "0-27"), ("27-0", "0-27"), ("0-99", "0-27"), ("4", "0-3,8-11"),
                            ("0-4", "0-3,8-11"), ("seat1=0-7;seat1=8-9", "0-27"), ("seat1=0-7;seat2=20-30", "0-27"),
                            ("seat1=", "0-27"), ("0-", "0-27"), ("a b", "0-27"), ("seat1=0-7;", "0-27"), ("0-7;seat1=1", "0-27"),
                            ("Seat1=1", "0-27"), ("0,,1", "0-27"), ("0-15", "garbage"), ("12345", "0-27")):
            with self.subTest(bad=bad, cpuset=cpuset):
                self.assertEqual(self.parse(bad, cpuset), ["REFUSED"])

    def test_config_set_refuses_cpus_the_host_does_not_have(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        for good in ("", "0-15", "seat1=0-7;seat2=8-15", "16-27"):
            self.assertEqual(sb.ctl("config-set", f"PLAY_SEAT_CPUS={good}").code, 0, good)
        for bad in ("0-28", "seat1=0-7;seat1=8-9", "seat1=30", "27-0"):
            res = sb.ctl("config-set", f"PLAY_SEAT_CPUS={bad}")
            self.assertEqual(res.code, 1, bad)
            self.assertIn("names CPUs this host does not have", res.err)
        self.assertEqual(next(e for e in json.loads(sb.ctl("config-get").out) if e["key"] == "PLAY_SEAT_CPUS")["value"],
                         "16-27")  # a refused change leaves the stored value as it was

    def proc(self, layout, mode):
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-pin"))
        self.addCleanup(shutil.rmtree, d)
        for pid, (comm, cgroup, threads) in layout.items():
            base = d / "proc" / str(pid)
            base.mkdir(parents=True)
            (base / "comm").write_text(comm + "\n")
            (base / "cgroup").write_text(cgroup + "\n")
            for tid, allowed in threads.items():
                (base / "task" / str(tid)).mkdir(parents=True)
                (base / "task" / str(tid) / "status").write_text(f"Name:\t{comm}\nCpus_allowed:\tffffff\nCpus_allowed_list:\t{allowed}\nMems_allowed:\t1\n")
        return d

    def pin(self, d, value, passes=1):
        res = source_ctl(r"""
          SCHEMA="$3"; PLAY_PROC="$1/proc"
          # the kernel would answer by changing the thread's list; do the same so a second pass can be idempotent
          taskset() { printf '%s\n' "$*" >> "$1/calls"; local list=${2} tid=${3}; sed -i "s/^Cpus_allowed_list:.*/Cpus_allowed_list:\t$list/" "$PLAY_PROC"/*/task/"$tid"/status; }
          play_seat_cpus_parse "$2" || { echo REFUSED; exit 0; }
          for ((n = 0; n < $4; n++)); do play_seat_pin; echo "pass $n done" >> "$1/calls"; done
        """.replace('"$1/calls"', f'"{d}/calls"'), str(d), value, str(CTL.parent.parent / "schema.json"), str(passes))
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = (d / "calls").read_text().split("\n")[:-1] if (d / "calls").exists() else []
        return calls

    LAYOUT = {
        100: ("sway", "0::/polyseat-unraid-canary/lxc.payload.seat1/init.scope", {100: "0-27", 101: "0-27", 102: "0-7"}),
        200: ("steam", "0::/polyseat-unraid-canary/lxc.payload.seat2", {200: "0-27"}),
        300: ("sshd", "0::/system.slice/sshd.service", {300: "0-27"}),
        400: ("lxc-monitor", "0::/polyseat-unraid-canary/lxc.monitor.seat1", {400: "0-27"}),
        500: ("incusd", "0::/polyseat-unraid-canary/daemon", {500: "0-27"}),
        600: ("game", "0::/polyseat-unraid-canary/lxc.payload.seat3", {600: "0-27", 601: "0-27"}),
    }

    def test_one_list_pins_every_thread_of_every_seat_and_nothing_else(self):
        d = self.proc(self.LAYOUT, "all")
        calls = sorted(c for c in self.pin(d, "0-15") if not c.startswith("pass"))
        self.assertEqual(calls, ["-pc 0-15 100", "-pc 0-15 101", "-pc 0-15 102", "-pc 0-15 200", "-pc 0-15 600", "-pc 0-15 601"])
        for pid in (300, 400, 500):  # a host service, the container's monitor and the daemon: not a seat payload
            self.assertIn("0-27", (d / f"proc/{pid}/task/{pid}/status").read_text())

    def test_per_seat_lists_pin_only_the_named_seats(self):
        d = self.proc(self.LAYOUT, "map")
        calls = sorted(c for c in self.pin(d, "seat1=0-7;seat2=8-15") if not c.startswith("pass"))
        # thread 102 already has seat1's list: left alone
        self.assertEqual(calls, ["-pc 0-7 100", "-pc 0-7 101", "-pc 8-15 200"])
        self.assertIn("0-27", (d / "proc/600/task/600/status").read_text())  # seat3 is not in the map

    def test_pinning_is_idempotent_and_off_without_a_setting(self):
        d = self.proc(self.LAYOUT, "idem")
        calls = self.pin(d, "16-27", passes=2)
        first = calls[:calls.index("pass 0 done")]
        second = calls[calls.index("pass 0 done") + 1:calls.index("pass 1 done")]
        self.assertEqual(len(first), 6)
        self.assertEqual(second, [])
        d = self.proc(self.LAYOUT, "off")
        self.assertEqual([c for c in self.pin(d, "") if not c.startswith("pass")], [])

    def test_the_session_checks_and_applies_the_setting(self):
        text = CTL.read_text()
        body = re.search(r"^cmd_polyseatd_play\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(body.index("play_pin_precheck ||"), body.index('mkdir -- "$dir"'))  # a bad value starts nothing
        self.assertIn("play_seat_priorities; play_seat_pin; fi", body)
        start = re.search(r"^cmd_play_start\(\) \(.*?^\)", text, re.MULTILINE | re.DOTALL).group(0)
        self.assertLess(start.index("play_pin_precheck ||"), start.index("owner_approval_for polyseatd-play"))
        pin = re.search(r"^play_seat_pin\(\) \{.*?^\}", text, re.MULTILINE | re.DOTALL).group(0)
        self.assertEqual(pin.count("taskset"), 1)
        seat = re.search(r"^play_payload_seat\(\) \{.*?^\}", text, re.MULTILINE | re.DOTALL).group(0)
        self.assertIn("play_payload_seat", pin)   # the one place that decides what a seat is (play's own group only)
        self.assertIn(r"lxc\\.payload\\.", seat)
        self.assertNotIn("lxc.monitor", pin + seat)


class DispatchAndEventTests(unittest.TestCase):
    def test_the_operator_verbs_are_dispatched_before_the_lock_and_play_start_after_a_probe(self):
        main = CTL.read_text().split("\nmain() {", 1)[1]
        early = main[:main.index("  acquire_lock\n")]
        self.assertIn("polyseatd-status | play-status) cmd_polyseatd_status; return ;;", early)
        self.assertIn('play-stop) cmd_play_stop "${args[@]}"; return ;;', early)
        self.assertIn("play-start) play_lock_free ||", early)  # refused at once, never queued behind a session
        self.assertIn('play-start) cmd_play_start "${args[@]}" ;;', main[main.index("  acquire_lock\n"):])
        self.assertIn('polyseatd-play) cmd_polyseatd_play "${args[@]}"; owner_session_end $? ;;', main)

    def test_play_start_is_not_an_unattended_boot_path(self):
        text = CTL.read_text()
        boot = re.search(r"^cmd_boot_owner\(\) \{.*?^\}", text, re.M | re.S).group(0)
        for forbidden in ("svc_start", "play-start", "cmd_play_start", "setsid", "cmd_activate", "polyseatd-play"):
            self.assertNotIn(forbidden, boot)
        self.assertIn("nothing is started", boot)

    def test_boot_in_owner_mode_only_verifies_and_logs(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        b = sb.make_bundle("1.0.0")
        shutil.copy(b["archive"], sb.bundles.joinpath(f"{b['sha']}.tar.gz")) if sb.bundles.exists() else None
        sb.bundles.mkdir(parents=True, exist_ok=True)
        for suffix, key in ((".tar.gz", "archive"), (".manifest.json", "manifest"), (".manifest.json.sig", "sig")):
            shutil.copy(b[key], sb.bundles / f"{b['sha']}{suffix}")
        sb.state.mkdir(parents=True, exist_ok=True)
        (sb.state / "pointer.json").write_text(json.dumps({"current": b["sha"], "previous": None}))
        (sb.state / "pointer.json").chmod(0o600)
        sb.write_cfg(SERVICE="enabled")  # even an enabled service does not start by itself
        script = r"""
          POLYSEAT_ROOT="$1"; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner
          init_paths; svc_start() { echo STARTED >> "$1/started"; }; main_boot() { cmd_boot; }
          ensure_state_dirs; acquire_lock; cmd_boot; echo "rc=$?"
        """
        run = lambda: subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(sb.root)],
                                     capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        res = run()
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertIn("nothing is started", res.stderr)
        self.assertFalse((sb.root / "started").exists())
        self.assertFalse((sb.ram / "current").exists())  # not even the RAM runtime is rebuilt: play-start does that
        archive = sb.bundles / f"{b['sha']}.tar.gz"
        archive.write_bytes(archive.read_bytes() + b"x")
        res = run()
        self.assertNotIn("rc=0", res.stdout)
        self.assertIn("failed verification", res.stderr)

    def test_the_stop_event_asks_for_the_session_first_and_the_start_event_starts_nothing(self):
        base = Path(CTL).parents[1] / "event"
        stopping = (base / "stopping_svcs").read_text()
        self.assertLess(stopping.index('"$CTL" play-stop --wait 300'), stopping.index('"$CTL" stop'))
        # the permanent workloads daemon stops alongside, bounded, and the event waits for it before it returns
        self.assertIn('"$CTL" workloads-stop --wait 600 > /dev/null 2>&1 &', stopping)
        self.assertLess(stopping.index("workloads-stop"), stopping.index('"$CTL" play-stop'))
        self.assertLess(stopping.index('"$CTL" stop'), stopping.index('wait "$workloads"'))
        mounted = (base / "disks_mounted").read_text()
        for forbidden in ("play-start", "polyseatd-play", "--approve", "activate"):
            self.assertNotIn(forbidden, mounted)
        for name in ("stopping_svcs", "disks_mounted"):
            self.assertEqual(subprocess.run(["bash", "-n", str(base / name)]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
