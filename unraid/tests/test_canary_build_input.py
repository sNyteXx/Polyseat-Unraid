"""Offline pinned-input pipeline tests. Fake DEB/ELF only; no network or execution."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from test_canary_image import fake_binary

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "unraid/scripts/prepare-canary-image.sh"


class PinnedCanaryInput(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-build-input-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.deb = self.root / "fixture.deb"
        self.deb.write_bytes(b"NOT A REAL DEB: offline command fixture")
        self.binary = fake_binary()
        self.payload = self.root / "fixture.tar"
        with tarfile.open(self.payload, "w") as archive:
            info = tarfile.TarInfo("./usr/bin/busybox")
            info.size = len(self.binary)
            archive.addfile(info, io.BytesIO(self.binary))
        self.events = self.root / "events"
        self.stub("curl", """#!/bin/sh
printf 'curl\n' >> "$FIXTURE_EVENTS"
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then shift; cp "$FIXTURE_DEB" "$1"; exit; fi
  shift
done
exit 2
""")
        self.stub("dpkg-deb", """#!/bin/sh
printf 'dpkg %s\n' "$*" >> "$FIXTURE_EVENTS"
if [ "$1" = --fsys-tarfile ]; then cat "$FIXTURE_TAR"; exit; fi
case "$3" in
  Package) printf '%s\n' busybox-static ;;
  Version) printf '%s\n' "${FIXTURE_VERSION:-1:1.36.1-6ubuntu3.1}" ;;
  Architecture) printf '%s\n' amd64 ;;
  *) exit 2 ;;
esac
""")
        self.values = dict(line.split("=", 1) for line in (ROOT / "unraid/build-pins.env").read_text().splitlines()
                           if line and not line.startswith("#"))
        self.values.update({
            "BUSYBOX_DEB_URL": "https://security.ubuntu.com/ubuntu/pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_amd64.deb",
            "BUSYBOX_DEB_VERSION": "1:1.36.1-6ubuntu3.1", "BUSYBOX_VERSION": "1.36.1",
            "BUSYBOX_DEB_SHA256": hashlib.sha256(self.deb.read_bytes()).hexdigest(),
            "BUSYBOX_BINARY_SHA256": hashlib.sha256(self.binary).hexdigest(),
        })
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", TMPDIR=str(self.root),
                        FIXTURE_DEB=str(self.deb), FIXTURE_TAR=str(self.payload), FIXTURE_EVENTS=str(self.events))
        self.image = self.root / "out/busybox.tar.gz"
        self.provenance = self.root / "out/provenance.json"

    def stub(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def run_input(self, **extra_env):
        pins = self.root / "pins.env"
        pins.write_text("".join(f"{key}={value}\n" for key, value in self.values.items()))
        result = subprocess.run(["sh", str(SCRIPT), str(pins), str(self.image), str(self.provenance)],
                                env=dict(self.env, **extra_env), capture_output=True, text=True)
        self.assertEqual(list(self.root.glob("polyseat-busybox-input.*")), [], "temporary input leaked")
        return result

    def test_verified_input_is_bound_into_provenance_without_installation(self):
        result = self.run_input()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.provenance.read_text())
        self.assertEqual(report["binary_sha256"], self.values["BUSYBOX_BINARY_SHA256"])
        self.assertEqual(report["source_sha256"], self.values["BUSYBOX_DEB_SHA256"])
        self.assertEqual(report["source_id"], "ubuntu:busybox-static=1:1.36.1-6ubuntu3.1")
        self.assertEqual(report["image_sha256"], hashlib.sha256(self.image.read_bytes()).hexdigest())

    def test_package_hash_is_checked_before_any_archive_processing(self):
        self.values["BUSYBOX_DEB_SHA256"] = "0" * 64
        result = self.run_input()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("package SHA-256 mismatch", result.stderr)
        self.assertNotIn("dpkg", self.events.read_text())
        self.assertFalse(self.image.exists())

    def test_wrong_package_version_and_binary_hash_are_refused(self):
        result = self.run_input(FIXTURE_VERSION="1:9.9.9-evil")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version mismatch", result.stderr)
        self.assertNotIn("--fsys-tarfile", self.events.read_text())
        self.values["BUSYBOX_BINARY_SHA256"] = "0" * 64
        result = self.run_input()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("binary SHA-256 mismatch", result.stderr)
        self.assertFalse(self.image.exists())

    def test_wrong_archive_host_and_existing_outputs_fail_before_download(self):
        original = self.values["BUSYBOX_DEB_URL"]
        self.values["BUSYBOX_DEB_URL"] = original.replace("security.ubuntu.com", "example.invalid")
        self.assertNotEqual(self.run_input().returncode, 0)
        self.assertFalse(self.events.exists())
        self.values["BUSYBOX_DEB_URL"] = original
        self.image.parent.mkdir()
        self.image.write_bytes(b"keep")
        self.assertNotEqual(self.run_input().returncode, 0)
        self.assertEqual(self.image.read_bytes(), b"keep")
        self.assertFalse(self.events.exists())


if __name__ == "__main__":
    unittest.main()
