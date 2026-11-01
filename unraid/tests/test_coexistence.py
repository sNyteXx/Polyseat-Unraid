"""The play session and the permanent workloads daemon run side by side on Tower: every play-side check that used to assume
"no incusd, no polyseat cgroup, no lxc payload process, no mount below the data directory" is scoped to the play instance,
and the other way round. Sandbox directories under /tmp, a fake /proc and fixture cgroup directories; nothing real is touched.

The fixtures of test_play_recover and test_owner_mode are reused by composition (their classes carry the tests themselves).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests import test_owner_mode as om
from unraid.tests import test_play_recover as rec
from unraid.tests.ctl_support import CTL, Sandbox

WL_GROUP = "polyseat-unraid-workloads"
PLAY_GROUP = "polyseat-unraid-canary"


class RecoveryWithWorkloads(rec.Fixture):
    """play-recover, with the workloads daemon fully up: processes, cgroup, pid file, lock, status, mounts."""

    def workloads_up(self):
        r = self.root
        # the daemon, a monitor and a container (init, docker inside it, a forked helper that carries the daemon's name)
        self.proc(7001, "incusd", f"0::/{WL_GROUP}/daemon")
        self.proc(7002, "incusd", f"0::/{WL_GROUP}/daemon")
        self.proc(7003, "lxc-monitord", f"0::/{WL_GROUP}/daemon/lxc.monitor.ubuntudev")
        self.proc(7004, "systemd", f"0::/{WL_GROUP}/daemon/lxc.payload.ubuntudev/init.scope")
        self.proc(7005, "dockerd", f"0::/{WL_GROUP}/daemon/lxc.payload.ubuntudev/system.slice/docker.service")
        self.proc(7006, "sway", f"0::/{WL_GROUP}/daemon/lxc.payload.seat1")  # even a container that has a seat's name
        self.proc(7007, "bash", f"0::/{WL_GROUP}")
        cg = r / "cg" / WL_GROUP
        (cg / "daemon").mkdir(parents=True)
        (self.run_dir / "workloads-incusd.pid").write_text("7001\n")
        (self.run_dir / "workloads-status.json").write_text('{"state": "running"}')
        (self.run_dir / "workloads.claim").write_text("")
        (self.run_dir / "workloads.lock").write_text("")
        (r / "mnt/cache/polyseat/workloads/incus").mkdir(parents=True)
        with open(r / "proc/self/mountinfo", "a") as table:
            table.write(f"40 25 0:30 / {r}/mnt/cache/polyseat/workloads/incus/storage-pools/workloads/containers/ubuntudev rw - zfs x rw\n")
            table.write(f"41 25 0:31 / {r}/mnt/cache/polyseat/workloads/incus/devices/ubuntudev rw - tmpfs x rw\n")

    def workloads_files(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else "<dir>")
                for p in sorted(self.root.rglob("*"))
                if "workloads" in p.name or "cg" in p.relative_to(self.root).parts[:1] or "workloads" in str(p) and p.is_file()}

    def test_a_running_workloads_daemon_is_no_reason_to_refuse_and_stays_untouched(self):
        self.abnormal_end("memory-floor-hard")
        self.workloads_up()
        before_proc = sorted(p.name for p in (self.root / "proc").iterdir())
        before = self.workloads_files()
        res = self.recover()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("rc=0", res.stdout)
        self.assertEqual(self.workloads_files(), before, "play-recover touched something of the workloads daemon")
        self.assertEqual(sorted(p.name for p in (self.root / "proc").iterdir()), before_proc)
        self.assertTrue((self.data / "clean-stop.json").exists())
        self.assertEqual(json.loads(self.ctl("play_state").stdout)["state"], "idle")

    def test_the_real_leak_check_agrees(self):
        # the real one walks this machine's /proc; the workloads daemon's own processes are skipped there by their group
        self.abnormal_end()
        self.workloads_up()
        res = self.recover(env={"REAL_LEAK_CHECK": "1"})
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_the_state_is_the_play_sessions_not_the_workloads_daemons(self):
        self.clean_stop()
        self.workloads_up()
        self.assertEqual(json.loads(self.ctl("play_state").stdout)["state"], "idle")
        self.abnormal_end("expired")
        state = json.loads(self.ctl("play_state").stdout)
        self.assertEqual((state["state"], state["reason"]), ("needs-recovery", "expired"))

    def test_what_belongs_to_play_is_still_found_next_to_the_workloads_daemon(self):
        self.abnormal_end()
        self.workloads_up()
        cases = (("an incusd outside both groups", "incusd", "0::/system.slice/x.service", "incusd is still running"),
                 ("a seat of the play group", "sway", f"0::/{PLAY_GROUP}/daemon/lxc.payload.seat1/init.scope", "in a seat's cgroup"),
                 ("a seat that left every group", "sway", "0::/lxc.payload.seat2", "in a seat's cgroup"),
                 ("a monitor of a seat", "lxc-monitord", f"0::/{PLAY_GROUP}/daemon/lxc.monitor.seat1", "in a seat's cgroup"),
                 ("the play group", "bash", f"0::/{PLAY_GROUP}/polyseatd", "in the plugin's cgroup"),
                 ("polyseatd anywhere", "polyseatd", "0::/system.slice/x.service", "polyseatd is still running"))
        for label, comm, cgroup, needle in cases:
            with self.subTest(label):
                self.proc(8000, comm, cgroup)
                self.refused_untouched(needle)
                shutil.rmtree(self.root / "proc/8000")
        self.assertEqual(self.recover().returncode, 0)

    def test_only_the_workloads_group_itself_is_exempt_not_a_name_that_starts_like_it(self):
        self.abnormal_end()
        self.workloads_up()
        for cgroup in (f"0::/{WL_GROUP}-evil/daemon", f"0::/{WL_GROUP}x", "0::/system.slice/polyseat-unraid-workloads",
                       f"0::/user.slice/{WL_GROUP}/daemon"):
            with self.subTest(cgroup=cgroup):
                self.proc(8001, "incusd", cgroup)
                self.refused_untouched("incusd is still running (pid 8001)")
                shutil.rmtree(self.root / "proc/8001")

    def test_a_cgroup_or_mount_of_the_play_instance_is_still_a_reason(self):
        self.abnormal_end()
        self.workloads_up()
        (self.root / "cg" / PLAY_GROUP).mkdir(parents=True)
        self.refused_untouched("the cgroup")
        shutil.rmtree(self.root / "cg" / PLAY_GROUP)
        with open(self.root / "proc/self/mountinfo", "a") as table:
            table.write(f"50 25 0:40 / {self.root}/mnt/cache/polyseat/incus/incus/storage-pools/default/containers/seat1 rw - zfs x rw\n")
        self.refused_untouched("still mounted below the persistent data directory")

    def test_the_playside_never_names_the_workloads_files(self):
        text = CTL.read_text()
        for name in ("play_recover_check_processes", "play_recover_check_cgroups", "play_recover_check_mounts", "play_recovery_reason",
                     "play_state", "play_seat_priorities", "play_seat_pin", "play_payload_seat"):
            body = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.M | re.S).group(0)
            self.assertNotRegex(body, r"\bWL_(?!x)\w+", f"{name} must not read the workloads daemon's files")


class SeatLoopsWithWorkloads(unittest.TestCase):
    """The priority and the pinning loops: a seat payload of the play group only. A container of the workloads daemon has the
    same cgroup shape and must never be reniced or pinned."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-seats"))
        self.addCleanup(shutil.rmtree, self.dir)

    def proc(self, pid, comm, cgroup, threads, allowed="0-27"):
        base = self.dir / "proc" / str(pid)
        base.mkdir(parents=True)
        (base / "comm").write_text(comm + "\n")
        (base / "cgroup").write_text(cgroup + "\n")
        for tid, nice in threads:
            (base / "task" / str(tid)).mkdir(parents=True)
            fields = ["S"] + ["0"] * 15 + [str(nice)] + ["0"] * 20
            (base / "task" / str(tid) / "stat").write_text(f"{tid} ({comm}) " + " ".join(fields) + "\n")
            (base / "task" / str(tid) / "status").write_text(f"Name:\t{comm}\nCpus_allowed_list:\t{allowed}\n")

    def layout(self):
        self.proc(100, "sway", f"0::/{PLAY_GROUP}/daemon/lxc.payload.seat1/init.scope", [(100, 19), (101, 0)])
        self.proc(200, "gamescope-wl", f"0::/{PLAY_GROUP}/lxc.payload.seat2", [(200, 19)])
        # the owner's containers, with the very same comm names and a seat-like container name
        self.proc(300, "sway", f"0::/{WL_GROUP}/daemon/lxc.payload.seat1/init.scope", [(300, 19)])
        self.proc(301, "Xwayland", f"0::/{WL_GROUP}/daemon/lxc.payload.ubuntudev/system.slice/x.service", [(301, 19), (302, 19)])
        self.proc(302 + 100, "wineserver", f"0::/{WL_GROUP}/lxc.payload.ubuntudev", [(402, 5)])
        self.proc(500, "sway", "0::/lxc.payload.seat1", [(500, 19)])          # left every group: not a play seat either
        self.proc(501, "sway", f"0::/{PLAY_GROUP}-watchdog/lxc.payload.seat1", [(501, 19)])  # a name that starts like the group

    def run_loop(self, body):
        script = r"""
          renice() { printf 'renice %s\n' "$*" >> "$1/calls"; }
          taskset() { printf 'taskset %s\n' "$*" >> "$1/calls"; }
          PLAY_PROC="$1/proc"; SCHEMA=@SCHEMA@
        """.replace("@SCHEMA@", str(CTL.parent.parent / "schema.json")).replace("$1/calls", f"{self.dir}/calls") + body
        res = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(self.dir)],
                             env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = (self.dir / "calls").read_text().split("\n")[:-1] if (self.dir / "calls").exists() else []
        return sorted(calls)

    def test_priorities_touch_play_seats_only(self):
        self.layout()
        calls = self.run_loop("play_seat_priorities")
        self.assertEqual(calls, ["renice -n -12 -p 100", "renice -n -12 -p 101", "renice -n -12 -p 200"])

    def test_pinning_touches_play_seats_only(self):
        self.layout()
        calls = self.run_loop('play_seat_cpus_parse "0-15" || exit 9\nplay_seat_pin')
        self.assertEqual(calls, ["taskset -pc 0-15 100", "taskset -pc 0-15 101", "taskset -pc 0-15 200"])

    def test_a_per_seat_list_does_not_leak_to_a_workloads_container_of_the_same_name(self):
        self.layout()
        calls = self.run_loop('play_seat_cpus_parse "seat1=0-7" || exit 9\nplay_seat_pin')
        self.assertEqual(calls, ["taskset -pc 0-7 100", "taskset -pc 0-7 101"])   # seat2 is not named; the container "seat1" is not a seat

    def test_the_seat_helper_names_the_seat_and_nothing_else(self):
        cases = {f"0::/{PLAY_GROUP}/lxc.payload.seat1": "seat1", f"0::/{PLAY_GROUP}/daemon/lxc.payload.seat-2/init.scope": "seat-2",
                 f"0::/{PLAY_GROUP}/daemon/lxc.monitor.seat1": None, f"0::/{WL_GROUP}/daemon/lxc.payload.seat1": None,
                 f"0::/{PLAY_GROUP}": None, "0::/lxc.payload.seat1": None, f"0::/{PLAY_GROUP}x/lxc.payload.seat1": None,
                 f"0::/system.slice/{PLAY_GROUP}/lxc.payload.seat1": None, "": None}
        for cg, want in cases.items():
            with self.subTest(cg=cg):
                res = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\nplay_payload_seat \"$1\"", "x", str(CTL), cg],
                                     env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
                self.assertEqual(res.stdout.strip() if res.returncode == 0 else None, want, res.stderr)

    def test_the_session_loop_calls_only_these(self):
        text = CTL.read_text()
        body = re.search(r"^cmd_polyseatd_play\(\) \([^\n]*\n(.*?)^\)", text, re.M | re.S).group(1)
        self.assertIn("play_seat_priorities; play_seat_pin; fi", body)
        for loop in ("play_seat_priorities", "play_seat_pin"):
            func = re.search(rf"^{loop}\(\) \{{.*?^\}}", text, re.M | re.S).group(0)
            self.assertIn("play_payload_seat", func)
            self.assertNotIn("lxc.payload", func.replace("# ", ""))


class MemoryFloorWithWorkloads(unittest.TestCase):
    """The floor ends the PLAY session. The workloads daemon (and its containers) lose memory to a game, never their lives."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-floor"))
        self.addCleanup(shutil.rmtree, self.dir)
        for d in ("proc", "run", "workload", "supervisor", WL_GROUP, f"{WL_GROUP}/daemon"):
            (self.dir / d).mkdir(parents=True)
        for f in ("workloads-stop", "workloads-incusd.pid", "workloads-status.json"):
            (self.dir / "run" / f).write_text("x\n")
        self.procs = []
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in self.procs])

    def watchdog(self, kb):
        (self.dir / "proc/meminfo").write_text(f"MemTotal: 83886080 kB\nMemAvailable: {kb} kB\n")
        script = r'''
          source <(sed '$d' "$1")
          ROOT="$2" LOG_ENABLED=0 RUN_DIR="$2/run" CANARY_OPERATION=8 CANARY_FLOOR_GRACE_SECONDS=2
          CANARY_CGROUP="$2/workload" CANARY_WATCHDOG_CGROUP="$2/supervisor" CANARY_SESSION="$2/session.json"
          canary_cgroup_check() { return 0; }
          canary_budget_check() { return 0; }
          canary_process_starttime() { printf '12345\n'; }
          sync_path() { :; }
          canary_watchdog_loop $(( $(date -u +%s) + 600 )) $(( $(canary_monotonic_seconds) + 600 ))
        '''
        proc = subprocess.Popen(["bash", "-c", script, "wd", str(CTL), str(self.dir)], env={"PATH": "/usr/bin:/bin"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)

    def wait_for(self, path, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if path.exists():
                return True
            time.sleep(0.05)
        return False

    def test_the_graceful_request_names_the_play_session_only(self):
        self.watchdog(3 * 1024 * 1024)
        self.assertTrue(self.wait_for(self.dir / "run/polyseatd-stop"))
        self.assertEqual((self.dir / "run/play-end-reason").read_text(), "memory-floor\n")
        time.sleep(0.5)
        self.assertEqual((self.dir / "run/workloads-stop").read_text(), "x\n", "the workloads daemon was asked to stop")
        self.assertFalse((self.dir / WL_GROUP / "cgroup.kill").exists())
        self.assertFalse((self.dir / WL_GROUP / "daemon/cgroup.kill").exists())

    def test_the_hard_stop_and_the_timeout_kill_the_play_group_only(self):
        for kb, reason in ((1024 * 1024, "memory-floor-hard"), (3 * 1024 * 1024, "memory-floor-timeout")):
            with self.subTest(reason=reason):
                (self.dir / "workload/cgroup.kill").unlink(missing_ok=True)
                (self.dir / "session.json.reason").unlink(missing_ok=True)
                self.watchdog(kb)
                self.assertTrue(self.wait_for(self.dir / "workload/cgroup.kill"))
                self.assertTrue(self.wait_for(self.dir / "session.json.reason"))
                time.sleep(0.3)
                self.assertEqual((self.dir / "session.json.reason").read_text(), reason + "\n")
                self.assertFalse((self.dir / WL_GROUP / "cgroup.kill").exists())
                self.assertFalse((self.dir / WL_GROUP / "daemon/cgroup.kill").exists())
                self.assertEqual((self.dir / "run/workloads-stop").read_text(), "x\n")
                self.procs[-1].kill()
                self.procs[-1].wait()
                for f in ("polyseatd-stop", "play-end-reason"):
                    (self.dir / "run" / f).unlink(missing_ok=True)

    def test_the_supervisor_and_the_watchdog_cannot_name_the_workloads_group(self):
        text = CTL.read_text()
        loop = text[text.index("canary_watchdog_loop() {"):text.index("canary_watchdog_start() {")]
        start = text[text.index("canary_watchdog_start() {"):text.index("canary_watchdog_check() {")]
        for part in (loop, start):
            self.assertNotIn("WL_", part)
            self.assertNotIn("workloads", part.replace("# ", "").lower().replace("workload tree", ""))
        # the one place the hard kill writes: play's group, by the variable the play code sets
        kills = re.findall(r'cgroup\.kill', text)
        self.assertTrue(kills)
        for line in text.splitlines():
            if "cgroup.kill" in line and not line.lstrip().startswith("#"):
                self.assertTrue("$CANARY_CGROUP" in line or "$group/" in line or "$group\"" in line, line)


class PlayStartWithWorkloads(unittest.TestCase):
    """play-start next to a running workloads daemon: its refusals are about the play instance."""

    def setUp(self):
        self.t = om.PlayStartTests("test_first_start_verifies_activates_stages_approves_and_launches")
        self.t.setUp()
        self.addCleanup(self.t.sb.cleanup)
        self.sb = self.t.sb
        self.assertEqual(self.t.start().returncode, 0)    # first start: activates the bundle, launches (stubbed)
        (self.sb.run_dir / "owner-approval.json").unlink()  # owner_session_end
        self.t.launches.unlink()

    WL = r"""
      WL_CGROUP=$POLYSEAT_ROOT/cg/polyseat-unraid-workloads; mkdir -p "$WL_CGROUP/daemon"
      : > "$RUN_DIR/workloads.claim"; echo '{"state":"running"}' > "$RUN_DIR/workloads-status.json"
      echo 4242 > "$RUN_DIR/workloads-incusd.pid"; touch "$RUN_DIR/workloads-stop-not-requested"
    """

    def test_a_workloads_daemon_is_no_reason_to_refuse(self):
        before = sorted(p.name for p in self.sb.run_dir.iterdir())
        proc = self.t.start(pre=self.WL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.t.launched(), ["launched approval=yes"])
        for name in ("workloads.claim", "workloads-status.json", "workloads-incusd.pid"):
            self.assertTrue((self.sb.run_dir / name).exists(), f"{name} was removed by play-start")
        self.assertEqual(json.loads((self.sb.run_dir / "workloads-status.json").read_text()), {"state": "running"})
        self.assertTrue(set(before) <= set(p.name for p in self.sb.run_dir.iterdir()))

    def test_play_refusals_are_play_refusals_still(self):
        for label, pre, needle in (
                ("the play cgroup", 'mkdir -p "$POLYSEAT_ROOT/cg"; CANARY_CGROUP=$POLYSEAT_ROOT/cg/polyseat-unraid-canary; mkdir -p "$CANARY_CGROUP"',
                 "an earlier session's cgroup still exists"),
                ("a play daemon", f"svc_pid() {{ echo 4242; }}", "Incus is already running"),
                ("a watchdog reason", 'echo expired > "$CANARY_SESSION.reason"', "the watchdog ended the previous session")):
            with self.subTest(label):
                proc = self.t.start(pre=self.WL + pre)
                self.t.assert_refused(proc, needle)

    def test_a_different_bundle_is_refused_while_the_workloads_daemon_runs_from_the_active_one(self):
        newer = self.t.put_bundle("2.0.0")
        pointer = self.sb.pointer()
        proc = self.t.start(pre=self.WL)
        self.t.assert_refused(proc, "the workloads daemon runs from the active one; stop the workloads daemon first")
        self.assertEqual(self.sb.pointer(), pointer, "nothing may be activated under the workloads daemon")
        # and the same start goes through once the workloads daemon is down
        proc = self.t.start()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.sb.pointer()["current"], newer["sha"])

    def test_the_workloads_lock_alone_counts_as_running(self):
        self.t.put_bundle("2.0.0")
        holder = subprocess.Popen(["flock", str(self.sb.run_dir / "workloads.lock"), "sleep", "20"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        time.sleep(0.5)
        self.t.assert_refused(self.t.start(), "stop the workloads daemon first")


class StopVerb(unittest.TestCase):
    """`stop` in an owner build is the graceful play stop first; the emergency path stays for signed builds and for a stop that
    times out. The workloads daemon is never part of it."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.sb.run_dir.mkdir(parents=True)
        self.sb.log_file.parent.mkdir(parents=True)

    def stop(self, *args, mode="owner", play_rc=0, workloads=False):
        script = r"""
          POLYSEAT_ROOT=$1; shift; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=$MODE
          require_mutation_allowed() { :; }
          cmd_play_stop() { echo "play-stop $*" >> "$ROOT/order"; return "$PLAY_RC"; }
          svc_stop() { echo "svc_stop $* marker=$([[ -e $RUN_DIR/canary-emergency-stop ]] && echo yes || echo no)" >> "$ROOT/order"; }
          cmd_workloads_stop() { echo WORKLOADS-TOUCHED >> "$ROOT/order"; }
          main "$@"; echo "rc=$?"
        """
        if workloads:
            (self.sb.run_dir / "workloads-incusd.pid").write_text("1\n")
        return subprocess.run(["bash", "-c", "source <(sed -e '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(self.sb.root), "stop", *args],
                              capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ["PATH"], "MODE": mode, "PLAY_RC": str(play_rc)})

    def order(self):
        f = self.sb.root / "order"
        return f.read_text().splitlines() if f.exists() else []

    def test_an_owner_build_ends_a_running_session_gracefully_before_the_legacy_stop(self):
        res = self.stop()
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertEqual(self.order(), ["play-stop --wait 300", "svc_stop full marker=yes"])

    def test_a_session_that_does_not_end_in_time_gets_the_emergency_stop(self):
        res = self.stop(play_rc=1)
        self.assertIn("rc=0", res.stdout)
        self.assertEqual(self.order(), ["play-stop --wait 300", "svc_stop full marker=yes"])
        self.assertIn("did not end within 300s; the emergency stop follows", res.stderr)

    def test_a_signed_build_keeps_the_emergency_stop_alone(self):
        self.assertIn("rc=0", self.stop(mode="signed").stdout)
        self.assertEqual(self.order(), ["svc_stop full marker=yes"])

    def test_the_explicit_emergency_form_skips_the_wait(self):
        self.assertIn("rc=0", self.stop("--emergency").stdout)
        self.assertEqual(self.order(), ["svc_stop full marker=yes"])

    def test_other_arguments_are_a_usage_error(self):
        for args in (("--wait", "5"), ("now",), ("--emergency", "x"), ("--EMERGENCY",)):
            with self.subTest(args=args):
                res = self.stop(*args)
                self.assertNotIn("rc=0", res.stdout)
                self.assertEqual(self.order(), [])
                self.assertIn("usage:", res.stderr)

    def test_the_workloads_daemon_is_not_part_of_it(self):
        self.stop(workloads=True)
        self.assertNotIn("WORKLOADS-TOUCHED", self.order())
        self.assertTrue((self.sb.run_dir / "workloads-incusd.pid").exists())

    def test_the_array_stop_event_uses_the_emergency_form_after_its_own_bounded_wait(self):
        stopping = (CTL.parents[1] / "event/stopping_svcs").read_text()
        self.assertIn('"$CTL" stop --emergency', stopping)
        self.assertNotRegex(stopping, r'"\$CTL" stop > ')
        self.assertLess(stopping.index('"$CTL" play-stop --wait 300'), stopping.index('"$CTL" stop --emergency'))

    def test_the_real_service_stop_really_is_the_cgroup_stop_of_the_play_instance(self):
        text = CTL.read_text()
        body = re.search(r"^canary_cgroup_stop\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn('canary_cgroup_remove "$CANARY_CGROUP"', body)
        self.assertNotIn("WL_", body)


class SupervisorCopy(unittest.TestCase):
    """The workloads supervisor runs from a RAM copy, so a package upgrade under it replaces nothing it reads."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.sb.run_dir.mkdir(parents=True)
        self.sb.log_file.parent.mkdir(parents=True)
        self.pkg = Path(tempfile.mkdtemp(prefix="polyseat-ctl-pkg"))
        self.addCleanup(shutil.rmtree, self.pkg)
        (self.pkg / "scripts").mkdir()
        shutil.copy(CTL, self.pkg / "scripts/polyseatctl")
        shutil.copy(CTL.parent.parent / "schema.json", self.pkg / "schema.json")

    def run_ctl(self, body):
        script = r"""
          POLYSEAT_ROOT=$1; CTL_SELF=$2; init_paths; CTL_SELF=$2
          POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner; LOG_ENABLED=1
          setsid() { printf '%s\n' "$*" >> "$POLYSEAT_ROOT/setsid"; }
        """ + body
        return subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(self.sb.root),
                               str(self.pkg / "scripts/polyseatctl")], capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "POLYSEAT_ROOT": str(self.sb.root)})

    def test_the_supervisor_is_started_from_a_byte_identical_ram_copy_read_by_bash(self):
        res = self.run_ctl('wl_launch_detached run; echo "rc=$?"')
        self.assertIn("rc=0", res.stdout, res.stderr)
        copy = self.sb.run_dir / "workloads-ctl/scripts/polyseatctl"
        self.assertEqual(copy.read_bytes(), (self.pkg / "scripts/polyseatctl").read_bytes())
        self.assertEqual((self.sb.run_dir / "workloads-ctl/schema.json").read_bytes(), (self.pkg / "schema.json").read_bytes())
        self.assertEqual((self.sb.root / "setsid").read_text().strip(), f"-f /bin/bash {copy} workloads-run run")
        self.assertEqual(oct(copy.stat().st_mode & 0o777), "0o700")

    def test_an_upgrade_under_it_changes_nothing_it_reads_and_the_next_start_copies_the_new_release(self):
        self.run_ctl('wl_launch_detached run')
        copy = self.sb.run_dir / "workloads-ctl/scripts/polyseatctl"
        old = copy.read_bytes()
        # upgradepkg: the installed files are replaced (here in place, the worst case for a running bash)
        (self.pkg / "scripts/polyseatctl").write_text("#!/bin/bash\necho new release\n")
        (self.pkg / "schema.json").write_text("{}")
        self.assertEqual(copy.read_bytes(), old)
        self.assertEqual(json.loads((self.sb.run_dir / "workloads-ctl/schema.json").read_text()).get("WORKLOADS_ENABLED", {}).get("default"), "no")
        self.run_ctl('wl_launch_detached run')
        self.assertEqual(copy.read_text(), "#!/bin/bash\necho new release\n")

    def test_the_copy_is_removed_exactly_and_never_through_a_symlink(self):
        self.run_ctl('wl_launch_detached run')
        self.assertTrue((self.sb.run_dir / "workloads-ctl").exists())
        self.run_ctl("wl_ctl_remove")
        self.assertFalse((self.sb.run_dir / "workloads-ctl").exists())
        victim = self.sb.root / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        (self.sb.run_dir / "workloads-ctl").symlink_to(victim)
        (self.sb.root / "setsid").unlink()
        res = self.run_ctl('wl_launch_detached run; echo "rc=$?"')
        self.assertNotIn("rc=0", res.stdout)
        self.assertEqual((victim / "keep").read_text(), "x")
        self.assertFalse((self.sb.root / "setsid").exists())

    def test_a_copy_that_differs_is_not_launched(self):
        res = self.run_ctl('cmp() { return 1; }; wl_launch_detached run; echo "rc=$?"')
        self.assertNotIn("rc=0", res.stdout)
        self.assertFalse((self.sb.root / "setsid").exists())
        self.assertFalse((self.sb.run_dir / "workloads-ctl/scripts/polyseatctl").exists())

    def test_the_supervisor_removes_its_copy_when_it_ends_and_uninstall_does_too(self):
        text = CTL.read_text()
        run = re.search(r"^cmd_workloads_run\(\) \(.*?^\)", text, re.M | re.S).group(0)
        self.assertIn("wl_run_exit \"$rc\" \"$ended\" \"$started\"; wl_ctl_remove; exit", run)
        uninstall = re.search(r"^cmd_uninstall\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn("wl_ctl_remove", uninstall)


class WorkloadsSideWithPlay(unittest.TestCase):
    """The reverse: the workloads leak check and its recovery ignore the play daemon."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-wlplay"))
        self.addCleanup(shutil.rmtree, self.dir)
        self.dir.chmod(0o700)

    def test_the_workloads_leak_check_skips_the_play_group_and_still_finds_strays(self):
        slot = self.dir / "usr/local/polyseat-unraid/slots/abc/payload/bin"
        slot.mkdir(parents=True)
        (slot / "incusd").write_text("")
        procs = self.dir / "procs"
        for pid, cgroup in ((10, f"0::/{PLAY_GROUP}/daemon"), (11, f"0::/{PLAY_GROUP}/daemon/lxc.payload.seat1"),
                            (12, f"0::/{PLAY_GROUP}-watchdog")):
            (procs / str(pid)).mkdir(parents=True)
            (procs / str(pid) / "cgroup").write_text(cgroup + "\n")
            (procs / str(pid) / "exe").symlink_to(slot / "incusd")
        script = r"""
          POLYSEAT_ROOT=$1; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner; init_paths; LOG_ENABLED=0
          wl_enter; CANARY_PROC=$2
          canary_leak_check; echo "rc=$?"
          mkdir -p "$2/13"; echo '0::/system.slice/x' > "$2/13/cgroup"; ln -s "$3" "$2/13/exe"
          canary_leak_check; echo "rc=$?"
        """
        res = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(self.dir), str(procs), str(slot / "incusd")],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(re.findall(r"rc=(\d)", res.stdout), ["0", "1"], res.stdout + res.stderr)
        self.assertIn("survived cgroup removal: 13", res.stderr)

    def test_workloads_recover_ignores_the_play_daemon(self):
        fake = self.dir / "proc"
        (fake / "20").mkdir(parents=True)
        (fake / "20/cgroup").write_text(f"0::/{PLAY_GROUP}/daemon/lxc.payload.seat1\n")
        (fake / "20/environ").write_bytes(b"INCUS_DIR=/mnt/cache/polyseat/incus/incus\0")
        script = r"""
          POLYSEAT_ROOT=$1; POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner; init_paths; LOG_ENABLED=0
          wl_enter; WL_PROC=$2
          wl_daemon_pid() { return 1; }
          wl_mounts_below() { :; }
          wl_nothing_runs; echo "rc=$?"
        """
        res = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + script, "x", str(CTL), str(self.dir), str(fake)],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertIn("rc=0", res.stdout, res.stderr)


if __name__ == "__main__":
    unittest.main()
