"""exec-inventory.py classifies traced canary execs against the recorded Unraid tool inventory."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "unraid" / "scripts" / "exec-inventory.py"
TOOLS = ROOT / "unraid" / "abi" / "unraid-7.3.3-x86_64.tools"


class ExecInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-exec-inventory-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.trace = self.dir / "trace"
        self.trace.mkdir()
        self.fixture = "/tmp/polyseat-ctl-canary-AAAA1111"
        self.tower = "/tmp/polyseat-canary-e2e-BBBB2222/tower-path"
        self.ctl = f"{self.fixture}/usr/local/emhttp/plugins/polyseat-unraid/scripts/polyseatctl"
        self.slot = f"{self.fixture}/usr/local/polyseat-unraid/slots/{'a' * 64}"
        (self.dir / "image.txt").write_text("metadata.yaml\nrootfs\nrootfs/bin/busybox\nrootfs/sbin/init\n")

    def record(self, pid, *lines):
        with (self.trace / f"run.{pid}").open("a") as stream:
            for line in lines:
                stream.write(line + "\n")

    @staticmethod
    def ok(path, *argv):
        return f'execve("{path}", {json.dumps(list(argv) or [path])}, 0x7ffd /* 3 vars */) = 0'

    @staticmethod
    def enoent(path):
        return f'execve("{path}", ["x"], 0x7ffd /* 3 vars */) = -1 ENOENT (No such file or directory)'

    def happy_trace(self):
        self.record(100, self.ok("/usr/bin/env"), self.ok(self.ctl), "clone(child_stack=NULL, flags=CLONE) = 101",
                    "clone(child_stack=NULL, flags=CLONE) = 102", "clone(child_stack=NULL, flags=CLONE) = 105")
        self.record(101, self.ok(f"{self.tower}/jq"))
        # The launcher chain stays strict through wrappers and reaches the bundled daemon.
        self.record(102, self.ok(f"{self.tower}/setsid"), self.ok(f"{self.tower}/env"),
                    self.ok(f"{self.tower}/setpriv"), self.ok(f"{self.tower}/unshare"),
                    "clone3({flags=CLONE_PARENT_SETTID}, 88) = 103")
        self.record(103, self.ok(f"{self.tower}/bash"), self.enoent(f"{self.fixture}/var/run/polyseat-unraid/deny-tools/zfs"),
                    self.enoent(f"{self.slot}/payload/tools/zfs"), self.enoent(f"{self.tower}/zfs"),
                    self.ok(f"{self.slot}/payload/bin/incusd"), "clone(child_stack=NULL, flags=CLONE) = 104")
        self.record(104, self.ok("/proc/self/exe"), self.ok("/sbin/init"), self.ok("/bin/busybox"))
        # Ubuntu's ldconfig wrapper re-execs ldconfig.real: an internal of that tool, not a canary dependency.
        self.record(105, self.ok(f"{self.tower}/ldconfig"), self.ok("/sbin/ldconfig.real"),
                    "clone(child_stack=NULL, flags=CLONE) = 106")
        self.record(106, self.ok("/usr/lib/x86_64-linux-gnu/helper-only-on-ubuntu"))

    def run_inventory(self, *extra):
        out = self.dir / "out.json"
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--trace-dir", str(self.trace), "--prefix", "run",
             "--tower-tools", str(TOOLS), "--tower-path", self.tower, "--fixture", self.fixture,
             "--controller", self.ctl, "--image-files", str(self.dir / "image.txt"), "--output", str(out), *extra],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.read_text()) if out.exists() else None
        return result, data

    def test_complete_lifecycle_is_classified_and_passes(self):
        self.happy_trace()
        result, data = self.run_inventory("--require-lifecycle")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(data["ok"])
        self.assertIn(f"/usr/local/polyseat-unraid/slots/{'a' * 64}/payload/bin/incusd", data["bundle"])
        self.assertEqual(sorted(data["container"]), ["/bin/busybox", "/sbin/init"])
        self.assertEqual(data["self-reexec"], 1)
        for name in ("jq", "setsid", "env", "setpriv", "unshare", "bash", "ldconfig"):
            self.assertIn(name, data["tower-path"])
        self.assertIn("/sbin/ldconfig.real", data["tool-internal"])
        self.assertIn("/usr/lib/x86_64-linux-gnu/helper-only-on-ubuntu", data["tool-internal"])
        self.assertIn("zfs", data["missing-everywhere-but-on-tower"])

    def test_lxc_hook_via_daemon_pid_exe_is_self_reexec_but_other_proc_exe_is_not(self):
        self.happy_trace()
        hook = (f'execve("/proc/1/exe", ["/proc/1/exe", "callhook", "{self.fixture}/var/lib/polyseat-unraid-canary/incus", '
                '"default", "polyseat-canary-cpu", "start"], 0x7ffd /* 3 vars */) = 0')
        self.record(104, hook)
        result, data = self.run_inventory("--require-lifecycle")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(data["self-reexec"], 2)
        self.record(103, 'execve("/proc/1/exe", ["/proc/1/exe", "-c", "id"], 0x7ffd /* 3 vars */) = 0')
        result, data = self.run_inventory("--require-lifecycle")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("/proc/1/exe" in item for item in data["violations"]))

    def test_host_program_that_unraid_lacks_is_a_violation(self):
        self.happy_trace()
        self.record(103, self.ok("/usr/bin/unsquashfs"))
        result, data = self.run_inventory("--require-lifecycle")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(data["ok"])
        self.assertTrue(any("/usr/bin/unsquashfs" in item for item in data["violations"]))

    def test_tower_like_path_cannot_smuggle_a_program_tower_lacks(self):
        self.happy_trace()
        self.record(101, self.ok(f"{self.tower}/unsquashfs"))
        result, data = self.run_inventory()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("Tower lacks: unsquashfs" in item for item in data["violations"]))

    def test_lifecycle_coverage_is_required_only_when_requested(self):
        self.record(100, self.ok("/usr/bin/env"), self.ok(self.ctl), "clone(child_stack=NULL, flags=CLONE) = 101")
        self.record(101, self.ok(f"{self.tower}/sha256sum"))
        result, _ = self.run_inventory()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, data = self.run_inventory("--require-lifecycle")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("lifecycle not covered" in item for item in data["violations"]))

    def test_unfinished_or_empty_traces_fail_closed(self):
        result, _ = self.run_inventory()
        self.assertNotEqual(result.returncode, 0)
        self.record(100, f'execve("{self.ctl}", ["polyseatctl"], 0x7ffd /* 3 vars */ <unfinished ...>')
        result, _ = self.run_inventory()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unfinished exec record", result.stderr)

    def test_inventory_baseline_matches_the_tower_observation(self):
        names = [line for line in TOOLS.read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(names), len(set(names)))
        for present in ("ip", "rsync", "setfattr", "tar", "xz", "jq", "ssh-keygen", "unshare", "setpriv", "bash"):
            self.assertIn(present, names)
        for absent in ("unsquashfs", "mksquashfs", "nft", "python3"):
            self.assertNotIn(absent, names)


if __name__ == "__main__":
    unittest.main()
