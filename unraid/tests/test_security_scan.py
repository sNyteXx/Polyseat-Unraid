"""Ensure a broken govulncheck cannot silently greenlight the WIP CI job."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "unraid/scripts/ci-govulncheck.sh"


class SecurityScan(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="polyseat-scan-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.binary = self.directory / "incusd"
        self.binary.write_bytes(b"build artifact placeholder")
        self.scanner = self.directory / "fake-govulncheck"
        self.report = self.directory / "report.txt"

    def scan(self, status: int, content: str):
        output = f"printf '%s\\n' '{content}'\n" if content else ""
        self.scanner.write_text("#!/bin/sh\n" + output + f"exit {status}\n")
        self.scanner.chmod(0o700)
        return subprocess.run(["sh", str(WRAPPER), str(self.scanner), str(self.binary), str(self.report)],
                              capture_output=True, text=True, timeout=10)

    def test_successful_scan(self):
        self.assertEqual(self.scan(0, "No vulnerabilities found.").returncode, 0)
        self.assertIn("No vulnerabilities found", self.report.read_text())

    def test_known_vulnerabilities_are_reported_for_manual_triage(self):
        self.assertEqual(self.scan(3, "Vulnerability #1: GO-2026-6866").returncode, 0)
        self.assertIn("GO-2026-6866", self.report.read_text())

    def test_unrecognized_binary_is_a_hard_failure(self):
        self.assertNotEqual(self.scan(1, "govulncheck: unrecognized binary format").returncode, 0)

    def test_other_scanner_failure_is_a_hard_failure(self):
        self.assertNotEqual(self.scan(2, "cannot fetch vulnerability database").returncode, 0)

    def test_success_status_with_format_error_still_fails(self):
        self.assertNotEqual(self.scan(0, "unrecognized binary format").returncode, 0)

    def test_empty_report_is_a_hard_failure(self):
        self.assertNotEqual(self.scan(0, "").returncode, 0)

    def test_missing_binary_fails_before_scanner(self):
        self.binary.unlink()
        self.assertNotEqual(self.scan(0, "No vulnerabilities found.").returncode, 0)


if __name__ == "__main__":
    unittest.main()
