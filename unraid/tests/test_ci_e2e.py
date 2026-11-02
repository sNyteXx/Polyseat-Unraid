"""Static safety contracts for the real-Incus E2E on disposable CI runners."""

from pathlib import Path
import unittest

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts" / "ci-e2e-controller.sh").read_text()


class HostStateGuardTests(unittest.TestCase):
    def test_nft_snapshot_excludes_only_volatile_counters(self):
        self.assertIn("nft --stateless list ruleset", SCRIPT)
        self.assertIn('> "$d/nft"', SCRIPT)
        self.assertNotIn("{ nft list ruleset", SCRIPT)

    def test_real_firewall_changes_still_fail_closed(self):
        self.assertIn("for item in iptables ip6tables nft sysctl; do", SCRIPT)
        self.assertIn('diff -q "$work/hs.before/$item" "$work/hs.after/$item"', SCRIPT)
        self.assertIn('fail "host state changed: $item', SCRIPT)

    def test_disposable_ci_guard_keeps_unraid_excluded(self):
        self.assertIn('[ -e /etc/unraid-version ]', SCRIPT)
        self.assertIn("RUNNER_ENVIRONMENT", SCRIPT)


if __name__ == "__main__":
    unittest.main()
