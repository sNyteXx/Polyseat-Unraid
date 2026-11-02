"""play-recover and the machine-readable play state (owner decision 2026-11-01, after two abnormal ends on Tower).

A /tmp sandbox root with a fake /proc, a fake mount table, fake sysfs, fake Incus data directory with backup.yaml files.
Nothing real is touched; the controller functions are sourced and driven directly (like the other owner-mode tests).
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

from unraid.tests.ctl_support import CTL

SHA = "a" * 64
SCHEMA = CTL.parent.parent / "schema.json"
EPP = {0: "balance_performance", 1: "balance_performance", 2: "power", 3: "default"}

BACKUP = """container:
  architecture: x86_64
  config:
    boot.autostart: "false"
    image.architecture: amd64
    volatile.base_image: 0123abcd
    volatile.idmap.next: '[{{"Isuid":true}}]'
  created_at: 2026-11-01T10:00:00Z
  devices:
    root:
      path: /
      pool: default
      type: disk
  ephemeral: false
  profiles:
  - default
  stateful: false
  description: ""
  name: {name}
  status: Stopped
  expanded_config:
    boot.autostart: "false"
    image.architecture: amd64
  expanded_devices:
    root:
      path: /
      pool: default
      type: disk
snapshots: []
pool:
  name: default
  driver: dir
"""

PRELUDE = r"""
source <(sed '$d' "$1")
POLYSEAT_ROOT=$2 POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=${MODE:-owner}
init_paths; SCHEMA=$3
mkdir -p "$RUN_DIR" "$LOG_DIR"; LOG_ENABLED=1
me=$(id -u); mg=$(id -g)
stat() { local out w r=""; out=$(command stat "$@") || return; [[ $out == *" "* ]] || { printf '%s\n' "$out"; return; }
  for w in $out; do [[ $w == "$me" || $w == "$mg" ]] && w=0; r+="$w "; done; printf '%s\n' "${r% }"; }
CANARY_CGROUP=$ROOT/cg/polyseat-unraid-canary CANARY_WATCHDOG_CGROUP=$ROOT/cg/polyseat-unraid-canary-watchdog PERSIST_MIN_FREE_KB=1
polyseatd_verify() { return "${VERIFY_RC:-0}"; }
# the real one walks every process of this machine (a second or two per call); one test below runs it
[[ -n ${REAL_LEAK_CHECK:-} ]] || canary_leak_check() { return 0; }
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="polyseat-ctl-rec"))
        self.root.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.root)
        r = self.root
        self.run_dir = r / "var/run/polyseat-unraid"
        self.log = r / "var/log/polyseat-unraid/polyseat-unraid.log"
        self.run_dir.mkdir(parents=True, mode=0o700)
        self.log.parent.mkdir(parents=True)
        self.log.write_text("")
        flash = r / "boot/config/plugins/polyseat-unraid"
        (flash / "state").mkdir(parents=True)
        (flash / "state/pointer.json").write_text(json.dumps({"current": SHA, "previous": None}))
        (flash / "state/pointer.json").chmod(0o600)
        self.cfg = flash / "polyseat-unraid.cfg"
        self.cfg.write_text('PLAY_CPU_PERFORMANCE="yes"\n')
        mount = r / "mnt/cache/polyseat"
        self.data = mount / "incus"
        self.pool = self.data / "incus/storage-pools/default/containers"
        self.pool.mkdir(parents=True)
        mount.chmod(0o755)
        self.data.chmod(0o711)
        (self.data / "persist.json").write_text(json.dumps({"schema": 1, "kind": "persistent", "incus_version": "7.5.1"}))
        # fake /proc: no process, a mount table without our directory
        (r / "proc/self").mkdir(parents=True)
        (r / "proc/self/mountinfo").write_text("25 1 0:21 / /mnt/cache/polyseat rw - zfs cache/polyseat rw\n")
        (r / "proc/sys/kernel").mkdir(parents=True)
        (r / "proc/sys/kernel/sysrq").write_text("1\n")
        # fake host
        (r / "etc/udev/rules.d").mkdir(parents=True)
        (r / "usr/local/lib").mkdir(parents=True)
        cpu = r / "sys/devices/system/cpu"
        for n, v in EPP.items():
            (cpu / f"cpu{n}/cpufreq").mkdir(parents=True)
            (cpu / f"cpu{n}/cpufreq/energy_performance_preference").write_text(v + "\n")
        daemon = r / "var/lib/polyseat-unraid-daemon"
        (daemon / "bin").mkdir(parents=True)
        (daemon / "rules").mkdir()
        (daemon / "lib/polyseat").mkdir(parents=True)
        console = daemon / "bin/polyseat-console"
        console.write_text('#!/bin/sh\n[ "$1" = off ] && echo "{}" > "$3"; [ "$1" = restore ] && rm -f "$2"; exit 0\n')
        console.chmod(0o755)
        (daemon / "rules/72-polyseat-hide.rules").write_text("# rule\n")
        (daemon / "lib/polyseat/device_owner.udev").write_text("#!@BUNDLE@/x\n")
        self.names = ["polyseat-persist-cpu", "seat1"]
        for name in self.names:
            self.instance(name)
        (self.data / "clean-stop.used.json").write_text(json.dumps(
            {"schema": 1, "archive_sha256": SHA, "stopped_at": 1, "instances": [
                {"name": n, "status": "Stopped", "autostart": "false"} for n in self.names]}))

    # -- the world -----------------------------------------------------------------------------------------------
    def instance(self, name, yaml=None):
        d = self.pool / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "backup.yaml").write_text(BACKUP.format(name=name) if yaml is None else yaml)
        return d

    def proc(self, pid, comm, cgroup="0::/system.slice/x.service"):
        d = self.root / "proc" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "comm").write_text(comm + "\n")
        (d / "cgroup").write_text(cgroup + "\n")

    def abnormal_end(self, reason="memory-floor-hard", console=True, session_files=True):
        """What a killed session leaves: the real play_host_on has run (sysrq 0, EPP performance, rule, helper, console)."""
        res = self.ctl("play_host_on || exit 7")
        self.assertEqual(res.returncode, 0, res.stderr)
        if not console:
            (self.run_dir / "play-console.json").unlink()
        if reason:
            (self.run_dir / "canary-session.json.reason").write_text(reason + "\n")
        if session_files:
            for name in ("polyseatd-status.json", "canary-session.json", "canary-guardian.pid", "canary-watchdog.ready",
                         "canary-emergency-stop", "owner-approval.json", "polyseatd-stop", "polyseatd.pid", "play-end-reason"):
                (self.run_dir / name).write_text("x\n")
        (self.run_dir / "play-evidence").mkdir(exist_ok=True)
        (self.run_dir / "play-evidence/status-start.json").write_text("{}")

    def clean_stop(self, instances=None, mode=0o600):
        path = self.data / "clean-stop.json"
        path.write_text(json.dumps({"schema": 1, "archive_sha256": SHA, "stopped_at": 1,
                                    "instances": instances if instances is not None else []}))
        path.chmod(mode)

    # -- running -------------------------------------------------------------------------------------------------
    def ctl(self, body, mode="owner", env=None):
        environment = {"PATH": os.environ["PATH"], "MODE": mode, **(env or {})}
        return subprocess.run(["bash", "-c", PRELUDE + body, "rec", str(CTL), str(self.root), str(SCHEMA)],
                              env=environment, capture_output=True, text=True, timeout=60)

    def recover(self, pre="", mode="owner", env=None):
        return self.ctl(pre + "\ncmd_play_recover; echo \"rc=$?\"", mode=mode, env=env)

    def snapshot(self):
        out = {}
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if rel.parts[:2] == ("var", "log"):
                continue
            out[str(rel)] = (path.readlink().as_posix() if path.is_symlink() else
                             path.read_bytes() if path.is_file() else "<dir>")
        return out

    def sysrq(self):
        return (self.root / "proc/sys/kernel/sysrq").read_text().strip()

    def epp(self):
        cpu = self.root / "sys/devices/system/cpu"
        return {n: (cpu / f"cpu{n}/cpufreq/energy_performance_preference").read_text().strip() for n in EPP}

    def log_text(self):
        return self.log.read_text()

    def refused(self, res, needle, *more, record=False):
        # the wrapper script ends with `echo rc=$?`, so the verdict is that line, not the shell's own exit code
        rc = re.search(r"^rc=(\d+)$", res.stdout, re.M)
        self.assertIsNotNone(rc, res.stdout + res.stderr)
        self.assertNotEqual(rc.group(1), "0", res.stdout + res.stderr)
        text = res.stderr
        self.assertIn(needle, text)
        for extra in more:
            self.assertIn(extra, text)
        if not record:
            self.assertFalse((self.data / "clean-stop.json").exists(), "a refusal never writes the record")

    def refused_untouched(self, needle, *more, pre="", record=False, **kw):
        before = self.snapshot()
        res = self.recover(pre, **kw)
        self.refused(res, needle, *more, record=record)
        self.assertEqual(self.snapshot(), before, "a refused recovery changes nothing")
        return res


class RecoveryHappyPathTests(Fixture):
    def test_the_verified_steps_in_order_and_nothing_else(self):
        self.abnormal_end("memory-floor-hard")
        self.assertEqual((self.sysrq(), set(self.epp().values())), ("0", {"performance"}))
        self.assertTrue((self.root / "etc/udev/rules.d/72-polyseat-hide.rules").exists())
        self.assertTrue((self.root / "usr/local/lib/polyseat").exists())
        self.assertTrue((self.run_dir / "play-console.json").exists())
        # a pool directory that is not in the last clean stop's list is checked as well
        self.instance("seat2")
        res = self.recover()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("rc=0", res.stdout)
        self.assertIn("recovered after memory-floor-hard: host restored, clean-stop record written", res.stdout)
        # the host is exactly as the record says
        self.assertEqual((self.sysrq(), self.epp()), ("1", EPP))
        self.assertFalse((self.root / "etc/udev/rules.d/72-polyseat-hide.rules").exists())
        self.assertFalse((self.root / "usr/local/lib/polyseat").exists())
        for name in ("play-host.json", "play-console.json"):
            self.assertFalse((self.run_dir / name).exists(), name)
        # the record: root 0600 (the stat shim says root), the schema persist_state_prepare reads, and why
        record = json.loads((self.data / "clean-stop.json").read_text())
        self.assertEqual(oct((self.data / "clean-stop.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual((record["schema"], record["archive_sha256"]), (1, SHA))
        self.assertEqual(record["instances"], [{"name": n, "status": "Stopped", "autostart": "false"}
                                               for n in ("polyseat-persist-cpu", "seat1", "seat2")])
        self.assertEqual(record["recovery"]["reason"], "memory-floor-hard")
        self.assertEqual(record["recovery"]["by"], "polyseatctl play-recover")
        self.assertIn("boot.autostart", record["recovery"]["why"])
        self.assertLess(abs(record["stopped_at"] - time.time()), 30)
        ok = self.ctl('persist_clean_stop_valid "$ROOT/mnt/cache/polyseat/incus/clean-stop.json" && echo VALID')
        self.assertIn("VALID", ok.stdout)
        # the watchdog's reason and every session record are gone, the evidence stays for the next start to rotate
        left = sorted(p.name for p in self.run_dir.iterdir())
        # lxc-shared.lock: the lock play_lxc_state_remove takes around the shared LXC mount point (shared with workloads).
        self.assertEqual([f for f in left if f not in ("lock", "lxc-shared.lock")], ["play-evidence", "play-last-end.json"], left)
        end = json.loads((self.run_dir / "play-last-end.json").read_text())
        self.assertEqual((end["reason"], end["clean"], end["recovered"]), ("memory-floor-hard", False, True))
        # every step is logged, in the order of the checks
        log = self.log_text()
        steps = ["needs recovery (memory-floor-hard)", "no incusd, no polyseatd, no process", "boot.autostart is \"false\" in every backup.yaml",
                 "all checks passed", "restoring the host from play-host.json", "putting the console keyboard back", "sysrq back to 1",
                 "clean-stop record written for 3 instance(s)", "removed canary-session.json.reason", "done; a new session can be started"]
        positions = [log.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions), steps)

    def test_the_next_state_is_idle_and_the_note_about_the_last_end_survives(self):
        self.abnormal_end("memory-floor")
        self.assertEqual(self.recover().returncode, 0)
        state = json.loads(self.ctl("play_state").stdout)
        self.assertEqual(state["state"], "idle")
        self.assertEqual({k: state["last_end"][k] for k in ("reason", "clean", "recovered")}, {"reason": "memory-floor", "clean": False, "recovered": True})
        again = self.recover()
        self.refused(again, "nothing to recover", record=True)

    def test_a_host_that_was_restored_before_needs_no_host_record(self):
        # killed after play_host_off but before the clean stop: no play-host.json, no clean stop, a stale status file
        (self.run_dir / "polyseatd-status.json").write_text("{}")
        res = self.recover()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no play-host.json", self.log_text())
        self.assertTrue((self.data / "clean-stop.json").exists())

    def test_a_valid_clean_stop_is_kept_when_only_the_host_was_not_restored(self):
        self.clean_stop([{"name": "seat1", "status": "Stopped", "autostart": "false"}])
        before = (self.data / "clean-stop.json").read_text()
        self.abnormal_end(reason=None, session_files=False)
        res = self.recover()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.data / "clean-stop.json").read_text(), before)
        self.assertIn("kept", res.stdout)
        self.assertEqual((self.sysrq(), self.epp()), ("1", EPP))

    def test_an_invalid_clean_stop_record_is_replaced(self):
        self.clean_stop([{"name": "seat1", "status": "Running", "autostart": "false"}])
        (self.run_dir / "polyseatd-status.json").write_text("{}")
        self.assertEqual(self.recover().returncode, 0)
        self.assertEqual(json.loads((self.data / "clean-stop.json").read_text())["instances"][0]["status"], "Stopped")

    def test_no_instances_at_all_is_a_valid_empty_record(self):
        shutil.rmtree(self.pool)
        (self.data / "clean-stop.used.json").unlink()
        (self.run_dir / "polyseatd-status.json").write_text("{}")
        self.assertEqual(self.recover().returncode, 0)
        self.assertEqual(json.loads((self.data / "clean-stop.json").read_text())["instances"], [])

    def test_a_failed_recovery_can_be_repeated(self):
        self.abnormal_end()
        self.proc(4242, "incusd")
        self.refused(self.recover(), "incusd is still running (pid 4242)")
        shutil.rmtree(self.root / "proc/4242")
        self.assertEqual(self.recover().returncode, 0)


class RecoveryRefusalTests(Fixture):
    def setUp(self):
        super().setUp()
        self.abnormal_end()

    def test_a_running_incusd_or_polyseatd(self):
        self.proc(4242, "incusd")
        self.refused_untouched(needle="incusd is still running (pid 4242)")
        shutil.rmtree(self.root / "proc/4242")
        self.proc(4243, "polyseatd")
        self.refused_untouched(needle="polyseatd is still running (pid 4243)")

    def test_any_process_in_a_seats_cgroup(self):
        for cg in ("0::/polyseat-unraid-canary/daemon/lxc.payload.seat1/init.scope", "0::/lxc.payload.seat2",
                   "0::/polyseat-unraid-canary/daemon/lxc.monitor.seat1"):
            with self.subTest(cg=cg):
                self.proc(5000, "sway", cg)
                self.refused_untouched(needle="process 5000 is in a seat's cgroup")
                shutil.rmtree(self.root / "proc/5000")

    def test_any_process_left_in_the_plugins_own_cgroups(self):
        for cg in ("0::/polyseat-unraid-canary/polyseatd", "0::/polyseat-unraid-canary-watchdog"):
            with self.subTest(cg=cg):
                self.proc(5001, "bash", cg)
                self.refused_untouched(needle="process 5001 is in the plugin's cgroup")
                shutil.rmtree(self.root / "proc/5001")

    def test_other_processes_and_other_cgroups_do_not_matter(self):
        self.proc(1, "init", "0::/")
        self.proc(900, "php-fpm", "0::/system.slice/emhttp.service")
        self.proc(901, "incusd-helper", "0::/system.slice/x")  # a name that only begins with it
        self.proc(902, "qemu-system-x86", "0::/machine.slice/qemu")
        self.assertEqual(self.recover().returncode, 0)

    def test_a_vanished_process_is_no_process(self):
        (self.root / "proc/6000").mkdir()  # exited between the listing and the read: neither comm nor cgroup
        self.assertEqual(self.recover().returncode, 0)

    def test_the_real_leak_check_is_part_of_the_checks(self):
        res = self.recover(env={"REAL_LEAK_CHECK": "1"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("rc=0", res.stdout)

    def test_a_process_that_left_its_cgroup_but_runs_the_runtime_is_caught_by_the_leak_check(self):
        self.refused_untouched(pre='canary_leak_check() { log error "canary: processes survived cgroup removal: 77"; return 1; }',
                               needle="processes survived cgroup removal")

    def test_a_mount_below_the_persistent_data_directory(self):
        base = "25 1 0:21 / /mnt/cache/polyseat rw - zfs cache/polyseat rw\n"
        for mount in ("/mnt/cache/polyseat/incus/incus/storage-pools/default/containers/seat1",
                      "/mnt/cache/polyseat/incus/incus/devices/seat1", "/mnt/cache/polyseat/incus"):
            with self.subTest(mount=mount):
                (self.root / "proc/self/mountinfo").write_text(base + f"99 25 0:55 / {self.root}{mount} rw - tmpfs x rw\n")
                self.refused_untouched(needle="still mounted below the persistent data directory")
        # the dataset's own mount, its other children and a lookalike sibling are not "below the data directory"
        (self.root / "proc/self/mountinfo").write_text(
            base + f"99 25 0:55 / {self.root}/mnt/cache/polyseat/steam-seat rw - tmpfs x rw\n"
                   f"98 25 0:56 / {self.root}/mnt/cache/polyseat/incus-other rw - tmpfs x rw\n")
        self.assertEqual(self.recover().returncode, 0)

    def test_an_unreadable_mount_table_and_missing_proc(self):
        (self.root / "proc/self/mountinfo").unlink()
        self.refused_untouched(needle="cannot read the mount table")
        (self.root / "proc/self/mountinfo").write_text("")
        shutil.rmtree(self.root / "proc")
        self.refused_untouched("cannot look at the processes")

    def test_a_polyseat_cgroup(self):
        for name in ("polyseat-unraid-canary", "polyseat-unraid-canary-watchdog"):
            with self.subTest(name=name):
                (self.root / "cg").mkdir(exist_ok=True)
                (self.root / "cg" / name).mkdir()
                self.refused_untouched(needle=f"the cgroup {self.root}/cg/{name} still exists")
                (self.root / "cg" / name).rmdir()

    def test_the_refusal_the_card_shows_names_the_first_failure_and_counts_the_rest(self):
        self.proc(4242, "incusd")
        (self.root / "cg").mkdir()
        (self.root / "cg/polyseat-unraid-canary").mkdir()
        res = self.recover()
        last = [line for line in res.stderr.splitlines() if "refused" in line][-1]
        self.assertIn("refused, nothing was changed: incusd is still running (pid 4242) (and 1 more, see the log)", last)
        self.assertLess(len(last.split("] ", 1)[1]), 300, "the card shows at most 300 characters")
        (self.root / "cg/polyseat-unraid-canary").rmdir()
        shutil.rmtree(self.root / "proc/4242")
        self.instance("seat2", BACKUP.format(name="seat2").replace('boot.autostart: "false"', 'boot.autostart: "true"', 1))
        last = [line for line in self.recover().stderr.splitlines() if "refused" in line][-1]
        self.assertTrue(last.endswith("(boot.autostart-is-true)"), last)  # one failure: no count

    def test_every_failed_check_is_reported_not_only_the_first(self):
        self.proc(4242, "incusd")
        (self.root / "cg").mkdir()
        (self.root / "cg/polyseat-unraid-canary").mkdir()
        self.instance("seat2", BACKUP.format(name="seat2").replace('boot.autostart: "false"', 'boot.autostart: "true"', 1))
        self.refused_untouched("incusd is still running", "the cgroup", "instance seat2")

    def test_nothing_is_recorded_if_something_starts_while_the_host_is_put_back(self):
        pre = ('eval "$(declare -f play_host_off | sed "1s/play_host_off/real_play_host_off/")"\n'
               'play_host_off() { real_play_host_off; mkdir -p "$ROOT/proc/7777"; echo incusd > "$ROOT/proc/7777/comm"; }')
        res = self.recover(pre)
        self.refused(res, "something is running now")
        self.assertEqual((self.sysrq(), self.epp()), ("1", EPP), "the host was already put back")
        self.assertTrue((self.run_dir / "canary-session.json.reason").exists(), "the session records stay for the next try")

    def test_a_host_restore_that_fails_keeps_its_record_and_writes_nothing(self):
        shutil.rmtree(self.root / "sys/devices/system/cpu/cpu2/cpufreq")
        res = self.recover()
        self.refused(res, "the host was not restored exactly")
        self.assertTrue((self.run_dir / "play-host.json").exists())
        self.assertTrue((self.run_dir / "canary-session.json.reason").exists())


class InstanceTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.run_dir / "polyseatd-status.json").write_text("{}")  # an abnormal end whose host was restored

    def test_boot_autostart_must_be_the_quoted_string_false_exactly_once(self):
        good = BACKUP.format(name="seat9")
        cases = {
            "absent": (good.replace('    boot.autostart: "false"\n', "", 1), "no-boot.autostart"),
            "true": (good.replace('boot.autostart: "false"', 'boot.autostart: "true"', 1), "boot.autostart-is-true"),
            "empty": (good.replace('boot.autostart: "false"', 'boot.autostart: ""', 1), "boot.autostart-is-"),
            "unquoted": (good.replace('boot.autostart: "false"', "boot.autostart: false", 1), "boot.autostart-is-false"),
            "twice": (good.replace('  config:\n', '  config:\n    boot.autostart: "false"\n', 1), "boot.autostart-twice"),
            "another instance's file": (good.replace("name: seat9", "name: seat1"), "name-is-not-the-directory"),
            "no container block": ("pool:\n  name: default\n", "no-container-block"),
            "no config block": ("container:\n  name: seat9\n", "no-config-block"),
            "empty": ("", "no-container-block"),
        }
        cases["expanded true"] = (good.replace("  expanded_config:\n    boot.autostart: \"false\"", "  expanded_config:\n    boot.autostart: \"true\""),
                                  "expanded-boot.autostart-is-true")
        for label, (yaml, verdict) in cases.items():
            with self.subTest(label):
                self.assertNotEqual(yaml, good, "the case must differ from the good file")
                self.instance("seat9", yaml)
                self.refused_untouched('instance seat9: backup.yaml does not prove boot.autostart "false"', *([f"({verdict})"] if verdict else []))
        self.instance("seat9", good)
        self.assertEqual(self.recover().returncode, 0)

    def test_the_single_quoted_false_and_a_numeric_name_are_fine(self):
        self.instance("seat9", BACKUP.format(name="seat9").replace('boot.autostart: "false"', "boot.autostart: 'false'", 1))
        self.instance("1234", BACKUP.format(name='"1234"'))
        res = self.recover()
        self.assertEqual(res.returncode, 0, res.stderr)
        names = [i["name"] for i in json.loads((self.data / "clean-stop.json").read_text())["instances"]]
        self.assertEqual(names, ["1234", "polyseat-persist-cpu", "seat1", "seat9"])

    def test_a_missing_or_odd_backup_yaml(self):
        d = self.instance("seat9")
        (d / "backup.yaml").unlink()
        self.refused_untouched(needle="instance seat9: no plain backup.yaml")
        (d / "backup.yaml").symlink_to("/etc/hostname")
        self.refused_untouched(needle="instance seat9: no plain backup.yaml")
        (d / "backup.yaml").unlink()
        (d / "backup.yaml").write_text("x" * (1048576 + 1))
        self.refused_untouched(needle="instance seat9: no plain backup.yaml")

    def test_a_name_in_the_last_clean_stop_without_a_directory_is_not_proven(self):
        shutil.rmtree(self.pool / "seat1")
        self.refused_untouched(needle="instance seat1: no plain backup.yaml")

    def test_names_outside_the_seat_names(self):
        for name in ("Seat_1", "-seat", "seat 1", "x" * 64, "UPPER"):
            with self.subTest(name=name):
                self.instance(name)
                self.refused_untouched(needle="is outside the seat names")
                shutil.rmtree(self.pool / name)
        (self.data / "clean-stop.used.json").write_text(json.dumps({"schema": 1, "instances": [{"name": "../etc", "status": "Stopped"}]}))
        self.refused_untouched(needle="is outside the seat names")

    def test_entries_of_the_pool_that_are_not_directories(self):
        (self.pool / "stray").write_text("x")
        self.refused_untouched(needle="unexpected entry stray")
        (self.pool / "stray").unlink()
        (self.pool / "link").symlink_to("seat1")
        self.refused_untouched(needle="unexpected entry link")
        (self.pool / "link").unlink()
        (self.pool / ".hidden").mkdir()
        self.refused_untouched(needle="is outside the seat names")

    def test_a_used_record_that_cannot_be_read(self):
        for text in ("not json", json.dumps({"instances": "x"}), json.dumps({"instances": [{"name": 5}]})):
            with self.subTest(text=text):
                (self.data / "clean-stop.used.json").write_text(text)
                self.refused_untouched(needle="clean-stop.used.json is not a record this controller wrote")

    def test_without_a_used_record_the_pool_alone_counts(self):
        (self.data / "clean-stop.used.json").unlink()
        self.assertEqual(self.recover().returncode, 0)


class HostRecordTests(Fixture):
    def test_a_console_that_cannot_be_put_back_is_refused_before_anything_changes(self):
        self.abnormal_end()
        (self.root / "var/lib/polyseat-unraid-daemon/bin/polyseat-console").unlink()
        self.refused_untouched("the staged polyseat-console is gone or not the verified one", "restore it by hand", "nothing was changed")
        self.assertEqual(self.sysrq(), "0", "the host is left as it was, not half restored")

    def test_the_staged_daemon_must_verify_before_its_tool_runs_as_root(self):
        self.abnormal_end()
        self.refused_untouched(needle="the staged polyseat-console is gone or not the verified one", env={"VERIFY_RC": "1"})
        self.assertEqual(self.recover(env={"VERIFY_RC": "0"}).returncode, 0)

    def test_without_a_console_record_the_staged_tool_is_not_needed(self):
        self.abnormal_end(console=False)
        (self.root / "var/lib/polyseat-unraid-daemon/bin/polyseat-console").unlink()
        self.assertEqual(self.recover(env={"VERIFY_RC": "1"}).returncode, 0)

    def test_a_console_record_alone_is_not_put_back_blind(self):
        (self.run_dir / "play-console.json").write_text("{}")
        self.refused_untouched(needle="play-console.json without play-host.json")

    def test_only_a_record_play_host_on_wrote_is_restored_from(self):
        self.abnormal_end()
        path = self.run_dir / "play-host.json"
        good = json.loads(path.read_text())
        bad = {
            "extra key": {**good, "evil": 1},
            "sysrq not a number": {**good, "sysrq": "1; reboot"},
            "sysrq as number": {**good, "sysrq": 1},
            "uhid": {**good, "uhid": "maybe"},
            "rule": {**good, "rule": "removed"},
            "helper": {**good, "helper": ""},
            "schema": {**good, "schema": 2},
            "epp path": {**good, "epp": {"../../x": "power"}},
            "epp value": {**good, "epp": {"0": "a b"}},
            "epp list": {**good, "epp": ["power"]},
        }
        for label, record in bad.items():
            with self.subTest(label):
                path.write_text(json.dumps(record))
                self.refused_untouched(needle="is not a record that play_host_on wrote")
        path.write_text("not json")
        self.refused_untouched(needle="is not a record that play_host_on wrote")
        path.write_text(json.dumps(good))
        self.assertEqual(self.recover().returncode, 0)
        # an older record without the energy profile is fine
        self.abnormal_end(reason=None, session_files=False)
        old = json.loads(path.read_text())
        old.pop("epp")
        path.write_text(json.dumps(old))
        self.clean_stop()
        self.assertEqual(self.recover().returncode, 0)


class DataAndGateTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.run_dir / "polyseatd-status.json").write_text("{}")

    def test_the_persistent_workspace_must_be_the_validated_one(self):
        (self.data / "persist.json").unlink()
        self.refused_untouched(needle="not an initialised persistent workspace")
        (self.data / "persist.json").write_text(json.dumps({"schema": 1, "kind": "disposable"}))
        self.refused_untouched(needle="not an initialised persistent workspace")
        (self.data / "persist.json").write_text(json.dumps({"schema": 1, "kind": "persistent"}))
        (self.root / "mnt/cache/polyseat").chmod(0o775)
        self.refused_untouched(needle="must be root:root and writable by root only")
        (self.root / "mnt/cache/polyseat").chmod(0o755)
        self.data.chmod(0o755)
        self.refused_untouched(needle="must be a root:root 0711 directory")

    def test_nothing_to_recover_when_the_last_session_stopped_cleanly(self):
        (self.run_dir / "polyseatd-status.json").unlink()
        self.clean_stop()
        self.refused_untouched("nothing to recover", record=True)

    def test_a_pending_update_transaction_and_a_missing_pointer(self):
        journal = self.root / "boot/config/plugins/polyseat-unraid/state/journal.json"
        journal.write_text(json.dumps({"phase": "activating", "target": SHA, "previous": None, "was_running": False}))
        journal.chmod(0o600)
        self.refused_untouched(needle="unfinished transaction")
        journal.unlink()
        (self.root / "boot/config/plugins/polyseat-unraid/state/pointer.json").write_text(json.dumps({"current": None, "previous": None}))
        (self.root / "boot/config/plugins/polyseat-unraid/state/pointer.json").chmod(0o600)
        self.refused_untouched(needle="no active runtime bundle is recorded")

    def test_only_an_owner_build_recovers_and_nothing_takes_arguments(self):
        res = self.recover(mode="signed")
        self.refused(res, "needs an owner-mode build")
        res = self.ctl('cmd_play_recover extra; echo "rc=$?"')
        self.refused(res, "takes no arguments")
        gate = ('POLYSEAT_APPROVAL_MODE=$1 POLYSEAT_RELEASE_GATE=canary ROOT=""; id() { printf "0\\n"; }; '
                'canary_approval_for() { echo APPROVAL-CHECKED; }; shift; require_mutation_allowed play-recover; echo ALLOWED')
        for mode, ok in (("owner", True), ("signed", False)):
            res = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\")\nshift\n" + gate, "x", str(CTL), mode],
                                 capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
            with self.subTest(mode=mode):
                self.assertEqual(("ALLOWED" in res.stdout, "APPROVAL-CHECKED" in res.stdout), (ok, False))
                if not ok:
                    self.assertIn("play-recover exists only in an owner-mode build", res.stderr)

    def test_dispatch_is_lock_aware_and_goes_through_the_lock(self):
        main = CTL.read_text().split("\nmain() {", 1)[1]
        early = main[:main.index("  acquire_lock\n")]
        self.assertIn("play-recover) play_lock_free ||", early)
        self.assertIn('play-recover) cmd_play_recover "${args[@]}" ;;', main[main.index("  acquire_lock\n"):])
        self.assertIn("play-recover | \\\n", main[:main.index("  require_mutation_allowed")])  # in the verb list


class StateTests(Fixture):
    def state(self, pre=""):
        return json.loads(self.ctl(pre + "\nplay_state").stdout)

    def test_idle(self):
        self.clean_stop()
        self.assertEqual(self.state(), {"state": "idle", "last_end": None})
        shutil.rmtree(self.data)
        self.assertEqual(self.state()["state"], "idle", "no persistent state at all: nothing ever ran")

    def test_every_fact_of_an_abnormal_end_is_needs_recovery_with_its_reason(self):
        self.clean_stop()
        facts = {
            "canary-session.json.reason": ("memory-floor-hard\n", "memory-floor-hard"),
            "play-host.json": ("{}", "host-not-restored"),
            "play-console.json": ("{}", "host-not-restored"),
            "polyseatd-status.json": ("{}", "session-process-gone"),
        }
        for name, (content, reason) in facts.items():
            with self.subTest(name):
                (self.run_dir / name).write_text(content)
                self.assertEqual(self.state(), {"state": "needs-recovery", "reason": reason, "last_end": None})
                (self.run_dir / name).unlink()
        (self.data / "clean-stop.json").unlink()
        self.assertEqual(self.state()["reason"], "no-clean-stop")
        (self.data / "clean-stop.json").write_text("{}")
        self.assertEqual(self.state()["reason"], "no-clean-stop", "a record the next start would refuse")
        (self.data / "clean-stop.json").unlink()
        shutil.rmtree(self.data)
        self.assertEqual(self.state()["state"], "idle")

    def test_the_cause_names_the_reason_but_never_makes_one(self):
        self.clean_stop()
        (self.run_dir / "play-end-reason").write_text("memory-floor\n")
        self.assertEqual(self.state()["state"], "idle", "a stale label alone is no abnormal end")
        (self.run_dir / "play-host.json").write_text("{}")
        self.assertEqual(self.state()["reason"], "memory-floor", "the graceful request is why the host is not restored")
        (self.run_dir / "canary-session.json.reason").write_text("memory-floor-timeout\n")
        self.assertEqual(self.state()["reason"], "memory-floor-timeout", "the watchdog's own word is the more exact one")

    def test_the_reason_is_a_word_whatever_the_file_says(self):
        self.clean_stop()
        for text in ("<script>alert(1)</script>\n", "A B\n", "", "x" * 300 + "\n", "\n"):
            with self.subTest(text=text[:15]):
                (self.run_dir / "canary-session.json.reason").write_text(text)
                self.assertEqual(self.state(), {"state": "needs-recovery", "reason": "unknown", "last_end": None})
        (self.run_dir / "canary-session.json.reason").unlink()
        (self.run_dir / "canary-session.json.reason").symlink_to("/etc/hostname")
        self.assertEqual(self.state()["reason"], "unknown", "a link is a reason too, and never followed")

    def test_the_last_end_is_passed_on_validated(self):
        self.clean_stop()
        (self.run_dir / "play-last-end.json").write_text(json.dumps(
            {"schema": 1, "reason": "memory-floor", "clean": True, "recovered": False, "ended_at": 1700000000, "evil": "x"}))
        self.assertEqual(self.state()["last_end"], {"reason": "memory-floor", "clean": True, "recovered": False, "ended_at": 1700000000})
        for bad in ({"reason": "A B", "clean": True, "ended_at": 1}, {"reason": "x", "clean": "yes", "ended_at": 1},
                    {"reason": "x", "clean": True, "ended_at": "now"}, {"reason": "x", "clean": True, "ended_at": 1.5}, []):
            with self.subTest(bad=bad):
                (self.run_dir / "play-last-end.json").write_text(json.dumps(bad))
                self.assertIsNone(self.state()["last_end"])

    def test_a_held_lock_is_running_with_a_status_and_busy_without(self):
        self.clean_stop()
        holder = subprocess.Popen(["flock", str(self.run_dir / "lock"), "sleep", "30"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        time.sleep(0.5)
        self.assertEqual(self.state(), {"state": "busy"})
        (self.run_dir / "polyseatd-status.json").write_text(json.dumps({"ok": True, "alive": True, "url": "https://10.0.0.2:47800"}))
        self.assertEqual(self.state(), {"ok": True, "alive": True, "url": "https://10.0.0.2:47800", "state": "running"})
        (self.run_dir / "polyseatd-status.json").write_text("garbage")
        self.assertEqual(self.state(), {"state": "running"})
        # while a session holds the lock, abnormal-looking leftovers are its own business
        (self.run_dir / "play-host.json").write_text("{}")
        self.assertEqual(self.state(), {"state": "running"})

    def test_the_status_verb_prints_the_state_and_keeps_its_exit_codes(self):
        def status():
            res = self.ctl('cmd_polyseatd_status; echo "rc=$?"')
            return json.loads(res.stdout.split("rc=")[0]), res.stderr
        self.clean_stop()
        state, err = status()
        self.assertEqual(state["state"], "idle")
        self.assertIn("no polyseatd session is running", err)
        self.assertNotIn("abnormally", err)
        (self.run_dir / "play-host.json").write_text("{}")
        state, err = status()
        self.assertEqual((state["state"], state["reason"]), ("needs-recovery", "host-not-restored"))
        self.assertIn("ended abnormally (host-not-restored): play-recover", err)
        self.assertIn("rc=2", self.ctl('cmd_polyseatd_status; echo "rc=$?"').stdout + "rc=2")  # die exits the shell with 2
        (self.run_dir / "play-host.json").unlink()
        holder = subprocess.Popen(["flock", str(self.run_dir / "lock"), "sleep", "30"])
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        time.sleep(0.5)
        (self.run_dir / "polyseatd-status.json").write_text(json.dumps({"alive": True}))
        res = self.ctl('cmd_polyseatd_status; echo "rc=$?"')
        self.assertIn("rc=0", res.stdout)
        self.assertEqual(json.loads(res.stdout.split("rc=")[0]), {"alive": True, "state": "running"})

    def test_it_is_read_only_and_needs_neither_the_lock_nor_incus(self):
        text = CTL.read_text()
        body = text[text.index("play_state() {"):text.index("# play-host.json is exactly what play_host_on wrote")]
        for forbidden in ("acquire_lock", "run_incus", "rm -f", "svc_pid", "> \"$RUN_DIR"):
            self.assertNotIn(forbidden, body)


class PlayStartPointsToTheButtonTests(Fixture):
    """play-start keeps refusing while recovery is needed, and says where the button is."""

    def start(self, pre=""):
        return self.ctl(pre + '\ncmd_play_start; echo "rc=$?"')

    def test_every_abnormal_end_is_refused_with_the_pointer(self):
        self.clean_stop()
        cases = {"play-host.json": "host changes not restored", "play-console.json": "earlier session left",
                 "polyseatd-status.json": "earlier session left", "canary-session.json.reason": "watchdog ended the previous session (memory-floor-hard)"}
        for name, needle in cases.items():
            with self.subTest(name):
                (self.run_dir / name).write_text("memory-floor-hard\n")
                res = self.start()
                self.assertNotIn("rc=0", res.stdout)
                self.assertIn(needle, res.stderr)
                self.assertIn('press "Wiederherstellen" on the Polyseat card', res.stderr)
                (self.run_dir / name).unlink()
        (self.root / "cg").mkdir()
        (self.root / "cg/polyseat-unraid-canary").mkdir()
        res = self.start()
        self.assertIn("cgroup still exists", res.stderr)
        self.assertIn("Wiederherstellen", res.stderr)

    def test_a_missing_clean_stop_record_is_refused_where_the_admin_reads_it(self):
        res = self.start()  # persist.json, no clean-stop.json
        self.assertNotIn("rc=0", res.stdout)
        self.assertIn("recorded no clean stop (no-clean-stop)", res.stderr)
        self.assertIn("Wiederherstellen", res.stderr)
        self.assertIn("polyseatctl play-recover", res.stderr)
        # nothing was removed or started by that refusal
        self.assertFalse((self.run_dir / "owner-approval.json").exists())

    def test_after_a_clean_stop_the_start_gets_past_that_check(self):
        self.clean_stop()
        res = self.start()
        self.assertNotIn("Wiederherstellen", res.stderr)
        self.assertIn("no complete runtime bundle", res.stderr)  # the first thing after it: the flash drive is empty here

    def test_a_stale_cause_label_and_last_end_are_cleared_by_the_start(self):
        self.clean_stop()
        (self.run_dir / "play-end-reason").write_text("memory-floor\n")
        (self.run_dir / "play-last-end.json").write_text("{}")
        res = self.start()
        self.assertIn("removed play-end-reason of an earlier session", self.log_text())
        self.assertIn("removed play-last-end.json of an earlier session", self.log_text())
        self.assertFalse((self.run_dir / "play-end-reason").exists())
        self.assertNotIn("rc=0", res.stdout)


class BackupAwkTests(unittest.TestCase):
    """The parser alone, on the shapes Incus writes."""

    def verdict(self, text, name="seat1"):
        with tempfile.TemporaryDirectory(prefix="polyseat-ctl-yaml") as d:
            (Path(d) / "backup.yaml").write_text(text)
            script = 'source <(sed \'$d\' "$1"); awk -v name="$3" "$PLAY_BACKUP_AWK" "$2/backup.yaml"'
            return subprocess.run(["bash", "-c", script, "x", str(CTL), d, name], capture_output=True, text=True,
                                  env={"PATH": "/usr/bin:/bin"}).stdout.strip()

    def test_incus_shapes(self):
        self.assertEqual(self.verdict(BACKUP.format(name="seat1")), "ok")
        self.assertEqual(self.verdict(BACKUP.format(name="seat1").replace("  config:\n", "  config:\n    user.note: \"boot.autostart: true\"\n", 1)), "ok")
        # a key of the same name elsewhere (the pool's config, a device) is not the instance's
        other = BACKUP.format(name="seat1") + "profiles:\n- config:\n    boot.autostart: \"true\"\n"
        self.assertEqual(self.verdict(other), "ok")
        self.assertEqual(self.verdict("container:\n  config:\n    boot.autostart: \"false\"\n  name: seat1\n"), "ok")
        self.assertEqual(self.verdict("container:\n  config: {}\n  name: seat1\n"), "no-config-block")
        self.assertEqual(self.verdict("container:\n  config:\n    a: b\n  name: seat1\n"), "no-boot.autostart")


if __name__ == "__main__":
    unittest.main()
