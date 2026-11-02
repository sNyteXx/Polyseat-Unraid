"""Seat attachment: prepare/seal/verify with disposable keys only; binding and tamper refusal."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "unraid/scripts/seat-attachment.py"
sys.path.insert(0, str(REPO))


def run(*args):
    return subprocess.run([sys.executable, "-I", str(TOOL), *map(str, args)], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")})


class SeatAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-seat-attach-")
        self.addCleanup(self.tmp.cleanup)
        d = self.d = Path(self.tmp.name)
        self.commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                                     check=True).stdout.strip()
        epoch = int(subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%ct", self.commit],
                                   capture_output=True, text=True, check=True).stdout)
        kit = d / "kit"
        kit.mkdir()
        (kit / "ctl.txz").write_bytes(b"controller")
        self.runtime = "a" * 64
        (kit / "canary-kit.json").write_text(json.dumps({"source_commit": self.commit, "controller": "ctl.txz",
                                                          "runtime": {"sha256": self.runtime}}))
        self.image = d / "polyseat-desktop.tar.xz"
        self.image.write_bytes(os.urandom(2048))
        image_sha = hashlib.sha256(self.image.read_bytes()).hexdigest()
        (d / "seat-image.json").write_text(json.dumps({"image_sha256": image_sha, "unpacked_size_bytes": 1234567}))
        manifest = {"schema": 1, "name": "polyseat-desktop", "alias": "polyseat-desktop",
                    "selftest": "/usr/local/bin/polyseat-selftest", "image_sha256": image_sha, "image_size": 2048,
                    "unpacked_bytes": 1234567, "source_commit": self.commit, "profiles": ["cpu", "nvidia"],
                    "created_at": epoch}
        mbytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        self.rehearsal = {"schema": 1, "status": "passed", "platform": "disposable-linux", "commit": self.commit,
                          "archive_sha256": self.runtime,
                          "controller_sha256": hashlib.sha256(b"controller").hexdigest(),
                          "seat_image_sha256": image_sha, "seat_manifest_sha256": hashlib.sha256(mbytes).hexdigest(),
                          "seat_manifest": manifest,
                          "steps": ["stage-activate", "seat-image-stage", "desktop-smoke", "cleanup",
                                    "host-state-unchanged"],
                          "selftest": {"start": {"verdict": "PASS"}, "restart": {"verdict": "PASS"}}}
        (d / "rehearsal.json").write_text(json.dumps(self.rehearsal))
        self.key = d / "keys/artifact"
        self.key.parent.mkdir()
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True)
        self.fp = subprocess.run(["ssh-keygen", "-lf", str(self.key) + ".pub"], capture_output=True, text=True,
                                 check=True).stdout.split()[1]
        self.out = d / "attachment"

    def prepare(self):
        return run("prepare", "--kit", self.d / "kit", "--image", self.image, "--seat-json", self.d / "seat-image.json",
                   "--rehearsal", self.d / "rehearsal.json", "--out", self.out)

    def seal(self):
        return run("seal", "--dir", self.out, "--trusted-key", str(self.key) + ".pub", "--fingerprint", self.fp,
                   "--signing-key", self.key)

    def verify(self):
        return run("verify", "--dir", self.out, "--trusted-key", str(self.key) + ".pub", "--fingerprint", self.fp)

    def test_prepare_seal_verify_round_trip(self):
        res = self.prepare()
        self.assertEqual(res.returncode, 0, res.stderr)
        res = self.seal()
        self.assertEqual(res.returncode, 0, res.stderr)
        res = self.verify()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["unraid_test_approved"], False)
        # The host controller's check: same namespace and identity as polyseatctl verify_seat_signature.
        signers = self.d / "allowed"
        signers.write_text('polyseat-unraid-release namespaces="polyseat-unraid-seat-image" '
                           + " ".join(Path(str(self.key) + ".pub").read_text().split()[:2]) + "\n")
        with (self.out / "seat.manifest.json").open("rb") as handle:
            check = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", "polyseat-unraid-release",
                                    "-n", "polyseat-unraid-seat-image", "-s", str(self.out / "seat.manifest.json.sig")],
                                   stdin=handle, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_rehearsal_for_another_controller_or_image_is_refused(self):
        for key, value in (("controller_sha256", "b" * 64), ("seat_image_sha256", "c" * 64), ("commit", "d" * 40),
                           ("status", "failed")):
            with self.subTest(key=key):
                bad = dict(self.rehearsal, **{key: value})
                (self.d / "rehearsal.json").write_text(json.dumps(bad))
                self.assertEqual(self.prepare().returncode, 2)
                self.assertFalse(self.out.exists())

    def test_tampered_image_or_wrong_fingerprint_fails_verification(self):
        self.prepare()
        self.seal()
        res = run("verify", "--dir", self.out, "--trusted-key", str(self.key) + ".pub", "--fingerprint", "SHA256:x")
        self.assertEqual(res.returncode, 2)
        with (self.out / "polyseat-desktop.tar.xz").open("ab") as handle:
            handle.write(b"x")
        self.assertEqual(self.verify().returncode, 2)


if __name__ == "__main__":
    unittest.main()
