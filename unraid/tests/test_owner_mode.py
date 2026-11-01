"""Owner approval mode (owner decision 2026-11-01): an Unraid admin's click replaces the signed per-session token.

Sandbox directories under /tmp only; no Incus daemon, cgroup or host path is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL
from unraid.tests.test_desktop_canary import source_ctl

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


if __name__ == "__main__":
    unittest.main()
