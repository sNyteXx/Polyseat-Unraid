"""The memory floor of a play session (owner, 2026-11-01): below 4 GiB the session is asked to end like play-stop does,
the hard cgroup kill follows only below 2 GiB or after the grace time. Fake meminfo in a /tmp sandbox root; the cgroup
paths are ordinary directories, so the supervisor can only write "1" into a fixture kill file."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL
from unraid.tests.test_desktop_canary import source_ctl

GIB_KB = 1024 * 1024


class VerdictTests(unittest.TestCase):
    def verdict(self, value):
        return source_ctl('canary_memory_floor_verdict "$1"', value).stdout.strip()

    def test_the_two_floors_and_their_edges(self):
        cases = {str(8 * GIB_KB): "ok", str(4 * GIB_KB): "ok", str(4 * GIB_KB - 1): "soft", str(3 * GIB_KB): "soft",
                 str(2 * GIB_KB): "soft", str(2 * GIB_KB - 1): "hard", "1048576": "hard", "0": "hard",
                 "": "hard", "unreadable": "hard", "-5": "hard", "1e9": "hard", "0000" + str(8 * GIB_KB): "ok"}
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self.verdict(value), want)

    def test_the_thresholds_are_the_documented_ones_and_fixed(self):
        text = CTL.read_text()
        self.assertIn("CANARY_HOST_MIN_AVAILABLE_KB=4194304\n", text)
        self.assertIn("CANARY_HOST_HARD_AVAILABLE_KB=2097152\n", text)
        self.assertIn("CANARY_FLOOR_GRACE_SECONDS=90\n", text)
        # not a setting: nothing in the schema or in the config whitelist names them
        schema = (CTL.parent.parent / "schema.json").read_text()
        for name in ("HARD_AVAILABLE", "FLOOR_GRACE", "MIN_AVAILABLE"):
            self.assertNotIn(name, schema)

    def test_the_single_floor_check_of_startup_and_other_operations_is_unchanged(self):
        script = r"""
          CANARY_OPERATION=$1 CANARY_HOST_MIN_AVAILABLE_KB=$2 CANARY_HOST_MAX_LOAD=0
          canary_host_pressure_check
        """
        self.assertEqual(source_ctl(script, "8", "1").returncode, 0)
        self.assertNotEqual(source_ctl(script, "8", str(2 ** 50)).returncode, 0)
        self.assertNotEqual(source_ctl(script, "4", "1").returncode, 0)


class MeminfoTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-floor"))
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "proc").mkdir()

    def read(self, text):
        if text is None:
            (self.dir / "proc/meminfo").unlink(missing_ok=True)
        else:
            (self.dir / "proc/meminfo").write_text(text)
        res = source_ctl('ROOT=$1; canary_mem_available_kb', str(self.dir))
        return res.returncode, res.stdout.strip()

    def test_only_a_well_formed_memavailable_line_counts(self):
        self.assertEqual(self.read("MemTotal:       82116948 kB\nMemAvailable:   19866060 kB\nSwapTotal: 0 kB\n"), (0, "19866060"))
        for label, text in (("absent", None), ("empty", ""), ("no such line", "MemFree: 5 kB\n"),
                            ("no unit", "MemAvailable: 5\n"), ("other unit", "MemAvailable: 5 MB\n"),
                            ("not a number", "MemAvailable: lots kB\n"), ("negative", "MemAvailable: -5 kB\n")):
            with self.subTest(label):
                self.assertEqual(self.read(text)[0], 1)


class EndReasonTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-floor"))
        self.addCleanup(shutil.rmtree, self.dir)

    def reason(self, reason=None, stop=False):
        for name in ("play-end-reason", "polyseatd-stop"):
            (self.dir / name).unlink(missing_ok=True)
        if reason is not None:
            (self.dir / "play-end-reason").write_text(reason)
        if stop:
            (self.dir / "polyseatd-stop").write_text("")
        return source_ctl('RUN_DIR=$1; play_end_reason', str(self.dir)).stdout.strip()

    def test_only_the_watchdogs_word_is_believed(self):
        self.assertEqual(self.reason("memory-floor\n", stop=True), "memory-floor")
        self.assertEqual(self.reason("memory-floor\n"), "memory-floor")
        self.assertEqual(self.reason(None, stop=True), "stop-requested")
        self.assertEqual(self.reason(None), "expired")
        for odd in ("memory-floor-hard\n", "Memory-Floor\n", "<script>\n", "\n", "x" * 5000, "memory-floor extra\n"):
            with self.subTest(odd=odd[:20]):
                self.assertEqual(self.reason(odd, stop=True), "stop-requested")


class WatchdogFloorTests(unittest.TestCase):
    """The real canary_watchdog_loop with operation 8, fake meminfo, fixture cgroup directories."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-floor"))
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "proc").mkdir()
        self.run_dir = self.dir / "run"
        self.run_dir.mkdir()
        (self.dir / "workload").mkdir()
        (self.dir / "supervisor").mkdir()
        self.procs = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=5)

    def meminfo(self, kb):
        tmp = self.dir / "proc/meminfo.new"
        tmp.write_text(f"MemTotal: 83886080 kB\nMemAvailable: {kb} kB\n")
        tmp.rename(self.dir / "proc/meminfo")

    def start(self, kb, grace=90, operation=8, extra=""):
        if kb is not None:
            self.meminfo(kb)
        script = r'''
          source <(sed '$d' "$1")
          ROOT="$2" LOG_ENABLED=0 RUN_DIR="$2/run" CANARY_OPERATION=$4 CANARY_FLOOR_GRACE_SECONDS=$3
          CANARY_CGROUP="$2/workload" CANARY_WATCHDOG_CGROUP="$2/supervisor" CANARY_SESSION="$2/session.json"
          canary_cgroup_check() { return 0; }
          canary_budget_check() { return 0; }
          canary_process_starttime() { printf '12345\n'; }
          sync_path() { :; }
        ''' + extra + r'''
          canary_watchdog_loop $(( $(date -u +%s) + 600 )) $(( $(canary_monotonic_seconds) + 600 ))
        '''
        proc = subprocess.Popen(["bash", "-c", script, "watchdog-unit", str(CTL), str(self.dir), str(grace), str(operation)],
                                env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def wait_for(self, path, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if Path(path).exists():
                return True
            time.sleep(0.05)
        return False

    kill = property(lambda self: self.dir / "workload/cgroup.kill")
    reason = property(lambda self: self.dir / "session.json.reason")
    stop = property(lambda self: self.run_dir / "polyseatd-stop")
    why = property(lambda self: self.run_dir / "play-end-reason")

    def test_enough_memory_touches_nothing(self):
        proc = self.start(8 * GIB_KB)
        time.sleep(2.5)
        self.assertIsNone(proc.poll())
        for path in (self.kill, self.reason, self.stop, self.why):
            self.assertFalse(path.exists(), path.name)

    def test_below_the_floor_the_session_is_asked_to_end_and_nothing_is_killed(self):
        proc = self.start(3 * GIB_KB, grace=60)
        self.assertTrue(self.wait_for(self.stop), "no stop request")
        self.assertEqual(self.why.read_text(), "memory-floor\n")
        time.sleep(2.5)
        self.assertFalse(self.kill.exists(), "killed inside the grace time")
        self.assertFalse(self.reason.exists(), ".reason would make every run_incus of the graceful stop fail")
        # the session ends the way play-stop does and svc_stop removes the group: the supervisor ends by itself
        shutil.rmtree(self.dir / "workload")
        proc.wait(timeout=5)
        self.assertFalse(self.reason.exists())

    def test_the_request_is_written_reason_first(self):
        text = CTL.read_text()
        loop = text[text.index("canary_watchdog_loop() {"):text.index("canary_watchdog_start() {")]
        self.assertLess(loop.index('> "$RUN_DIR/play-end-reason"'), loop.index(': > "$RUN_DIR/polyseatd-stop"'))

    def test_the_grace_time_is_a_bound(self):
        self.start(3 * GIB_KB, grace=2)
        self.assertTrue(self.wait_for(self.kill, 8), "no hard stop after the grace time")
        self.assertEqual(self.reason.read_text(), "memory-floor-timeout\n")
        self.assertTrue(self.stop.exists())

    def test_the_bound_holds_when_the_memory_recovers_meanwhile(self):
        self.start(3 * GIB_KB, grace=3)
        self.assertTrue(self.wait_for(self.stop))
        self.meminfo(8 * GIB_KB)
        self.assertFalse(self.wait_for(self.kill, 1.5))
        self.assertTrue(self.wait_for(self.kill, 8), "a session that did not finish is cut off at the deadline")
        self.assertEqual(self.reason.read_text(), "memory-floor-timeout\n")

    def test_below_the_hard_floor_there_is_no_grace(self):
        start = time.monotonic()
        self.start(2 * GIB_KB - 1, grace=90)
        self.assertTrue(self.wait_for(self.kill, 4))
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(self.reason.read_text(), "memory-floor-hard\n")
        self.assertFalse(self.stop.exists() or self.why.exists())

    def test_a_falling_memory_escalates_from_the_request_to_the_kill(self):
        self.start(3 * GIB_KB, grace=60)
        self.assertTrue(self.wait_for(self.stop))
        self.assertFalse(self.kill.exists())
        self.meminfo(GIB_KB)
        self.assertTrue(self.wait_for(self.kill, 4))
        self.assertEqual(self.reason.read_text(), "memory-floor-hard\n")

    def test_unreadable_meminfo_is_the_hard_floor_as_it_always_was(self):
        self.start(None)
        self.assertTrue(self.wait_for(self.kill, 4))
        self.assertEqual(self.reason.read_text(), "memory-floor-hard\n")

    def test_a_stop_that_is_under_way_keeps_its_name_but_not_its_time(self):
        self.stop.write_text("")
        self.start(3 * GIB_KB, grace=2)
        time.sleep(1.5)
        self.assertFalse(self.why.exists(), "an operator's stop is not renamed")
        self.assertTrue(self.wait_for(self.kill, 8))
        self.assertEqual(self.reason.read_text(), "memory-floor-timeout\n")

    def test_the_other_operations_keep_the_single_hard_floor_and_ask_nobody(self):
        self.start(3 * GIB_KB, operation=4, extra="canary_host_pressure_check() { return 1; }")
        self.assertTrue(self.wait_for(self.kill, 4))
        self.assertEqual(self.reason.read_text(), "resource-policy-or-budget\n")
        self.assertFalse(self.stop.exists() or self.why.exists())

    def test_the_supervisor_gets_everything_it_needs_in_its_own_clean_shell(self):
        text = CTL.read_text()
        start = text[text.index("canary_watchdog_start() {"):text.index("canary_watchdog_check() {")]
        for needle in ("CANARY_HOST_HARD_AVAILABLE_KB=", "CANARY_FLOOR_GRACE_SECONDS=", 'RUN_DIR="$RUN_DIR"',
                       "canary_mem_available_kb canary_memory_floor_verdict"):
            self.assertIn(needle, start)


if __name__ == "__main__":
    unittest.main()
