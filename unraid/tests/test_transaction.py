"""Actual filesystem/rollback tests only in unique throwaway /tmp directories."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from unraid.lab.transaction import (
    CrashInjected,
    HealthCheckFailed,
    LabDenied,
    LabTransaction,
)


def bundle(payload: bytes, path: str = "payload/bin/incusd") -> tuple[bytes, dict]:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        info = tarfile.TarInfo(path)
        info.uid = info.gid = 0
        info.mode = 0o755
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return archive.getvalue(), {"files": [{
        "path": path, "size": len(payload), "mode": 0o755,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }]}


class DisposableFilesystemTransactions(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="polyseat-transaction-", dir="/tmp")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.lab = LabTransaction(self.root)
        self.a = bundle(b"known-good-incus")
        self.b = bundle(b"new-incus-release")

    def stage(self, data: tuple[bytes, dict]) -> str:
        return self.lab.stage(*data)

    def activate(self, digest: str, **kwargs) -> str | None:
        return self.lab.activate(digest, health=lambda slot: True, **kwargs)

    def test_stage_verify_and_activate_idempotently(self):
        digest = self.stage(self.a)
        self.assertEqual(digest, self.stage(self.a))
        self.assertEqual(self.lab.active(), None)
        with self.assertRaisesRegex(LabDenied, "approval"):
            self.activate(digest, approved=False)
        with self.assertRaisesRegex(LabDenied, "health check"):
            self.lab.activate(digest, approved=True)
        self.assertEqual(self.activate(digest, approved=True), None)
        self.assertEqual(self.lab.active(), digest)
        self.assertEqual(self.activate(digest, approved=True), digest)
        self.assertEqual((self.root / "slots" / digest / "payload/bin/incusd").read_bytes(),
                         b"known-good-incus")

    def test_failure_injected_upgrade_restores_previous_slot_and_data(self):
        prior = self.stage(self.a)
        self.activate(prior, approved=True)
        newer = self.stage(self.b)
        outside = self.root.parent / (self.root.name + "-external-gaming-vm-and-docker")
        outside.write_bytes(b"protected external data")
        self.addCleanup(outside.unlink)
        with self.assertRaises(CrashInjected):
            self.activate(newer, approved=True, crash_at="after_switch")
        with self.assertRaisesRegex(LabDenied, "recovery"):
            self.lab.active()  # interrupted switch must never look healthy
        with self.assertRaisesRegex(LabDenied, "recovery"):
            self.activate(prior, approved=True)
        recovered = LabTransaction(self.root).recover()
        self.assertEqual(recovered, prior)
        self.assertEqual(self.lab.active(), prior)
        self.assertEqual(outside.read_bytes(), b"protected external data")
        self.assertFalse((self.root / "state/journal.json").exists())

    def test_crash_after_journal_keeps_old_pointer(self):
        prior = self.stage(self.a)
        self.activate(prior, approved=True)
        newer = self.stage(self.b)
        with self.assertRaises(CrashInjected):
            self.activate(newer, approved=True, crash_at="after_journal")
        with self.assertRaisesRegex(LabDenied, "recovery"):
            self.lab.active()
        self.assertEqual(LabTransaction(self.root).recover(), prior)

    def test_failed_health_check_rolls_back_even_if_callback_throws(self):
        prior = self.stage(self.a)
        self.activate(prior, approved=True)
        newer = self.stage(self.b)
        with self.assertRaises(HealthCheckFailed):
            self.lab.activate(newer, approved=True, health=lambda slot: False)
        self.assertEqual(self.lab.active(), prior)
        with self.assertRaises(HealthCheckFailed):
            self.lab.activate(newer, approved=True, health=lambda slot: 1 / 0)
        self.assertEqual(self.lab.active(), prior)

    def test_explicit_rollback_restores_previous_and_first_install_absent(self):
        prior = self.stage(self.a)
        self.activate(prior, approved=True)
        newer = self.stage(self.b)
        self.activate(newer, approved=True)
        with self.assertRaisesRegex(LabDenied, "approval"):
            self.lab.rollback(approved=False)
        self.assertEqual(self.lab.rollback(approved=True), prior)
        self.assertEqual(self.lab.active(), prior)
        self.assertEqual(self.lab.rollback(approved=True), None)
        self.assertIsNone(self.lab.active())

    def test_crash_during_rollback_is_recovered(self):
        prior = self.stage(self.a)
        self.activate(prior, approved=True)
        newer = self.stage(self.b)
        self.activate(newer, approved=True)
        with self.assertRaises(CrashInjected):
            self.lab.rollback(approved=True, crash_at="after_journal")
        self.assertEqual(LabTransaction(self.root).recover(), prior)
        self.assertEqual(self.lab.active(), prior)

    def test_tampered_or_unknown_state_blocks(self):
        digest = self.stage(self.a)
        self.activate(digest, approved=True)
        payload = self.root / "slots" / digest / "payload/bin/incusd"
        payload.write_bytes(b"altered code")
        with self.assertRaisesRegex(LabDenied, "mismatch"):
            self.lab.active()
        with self.assertRaisesRegex(LabDenied, "mismatch"):
            self.activate(digest, approved=True)
        with self.assertRaisesRegex(LabDenied, "mismatch"):
            self.lab.rollback(approved=True)
        state = self.root / "state/active.json"
        state.write_text('{"current":"garbage","previous":null}', encoding="utf-8")
        with self.assertRaises(LabDenied):
            self.lab.active()

    def test_special_archives_and_symlinks_are_rejected(self):
        evil = bundle(b"not-for-etc", path="payload/../../etc/passwd")
        with self.assertRaises(LabDenied):
            self.stage(evil)
        prior = self.stage(self.a)
        self.activate(prior, approved=True)
        state = self.root / "state/active.json"
        state.unlink()
        foreign = self.root / "external.json"
        foreign.write_text(json.dumps({"current": prior, "previous": None}))
        state.symlink_to(foreign)
        with self.assertRaisesRegex(LabDenied, "untrusted"):
            self.lab.active()

    def test_untrusted_roots_and_permissions_are_rejected(self):
        with self.assertRaises(LabDenied):
            LabTransaction("/")
        with self.assertRaises(LabDenied):
            LabTransaction(self.root / "child")
        private_mode = self.root.stat().st_mode & 0o777
        try:
            self.root.chmod(0o755)
            with self.assertRaises(LabDenied):
                LabTransaction(self.root)
        finally:
            self.root.chmod(private_mode)
        target = Path(tempfile.mkdtemp(prefix="polyseat-transaction-", dir="/tmp"))
        self.addCleanup(target.rmdir)
        link = Path(tempfile.mkdtemp(prefix="polyseat-transaction-", dir="/tmp"))
        link.rmdir()
        link.symlink_to(target, target_is_directory=True)
        self.addCleanup(link.unlink)
        with self.assertRaises(LabDenied):
            LabTransaction(link)


if __name__ == "__main__":
    unittest.main()
