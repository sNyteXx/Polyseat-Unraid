"""One-shot canary lifecycle/policy tests. No Incus, namespaces or real cgroups."""

from __future__ import annotations

import json
import os
import shlex
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL, Sandbox

ROOT = Path(__file__).resolve().parents[2]


class CanaryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="polyseat-canary-runtime-")
        self.addCleanup(self.workspace.cleanup)
        self.dir = Path(self.workspace.name)

    def shell(self, script, *args):
        return subprocess.run(
            ["bash", "-c", 'source <(sed \'$d\' "$1"); shift; ' + script,
             "canary-unit", str(CTL), str(self.dir), *args],
            env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
            timeout=15, check=False,
        )

    def test_fresh_state_is_created_once_and_never_reused(self):
        result = self.shell(r'''
          POLYSEAT_RELEASE_GATE=canary
          DATA_REAL="$1/data" INCUS_DIR="$1/data/incus" CANARY_SESSION="$1/session.json"
          canary_fresh_state_prepare aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa || exit 10
          [[ -d "$INCUS_DIR" && -f "$DATA_REAL/one-shot.json" ]] || exit 11
          canary_fresh_state_prepare aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa && exit 12
          [[ -d "$INCUS_DIR" && -f "$DATA_REAL/one-shot.json" ]]
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.dir / "data/one-shot.json").read_text())
        self.assertEqual(record["kind"], "ephemeral-cpu-only")

    def test_persistent_autostart_db_blocks_before_any_mutation(self):
        incus = self.dir / "data/incus/database"
        incus.mkdir(parents=True)
        db = incus / "local.db"
        db.write_text("previous-incus-autostart-state")
        result = self.shell(r'''
          POLYSEAT_RELEASE_GATE=canary
          DATA_REAL="$1/data" INCUS_DIR="$1/data/incus" CANARY_SESSION="$1/session.json"
          canary_fresh_state_prepare aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        ''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(db.read_text(), "previous-incus-autostart-state")
        self.assertFalse((self.dir / "data/one-shot.json").exists())

    def test_nonempty_workspace_and_session_both_block(self):
        for obstruction in ("data/foreign", "session.json"):
            path = self.dir / obstruction
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            result = self.shell(r'''
              POLYSEAT_RELEASE_GATE=canary
              DATA_REAL="$1/data" INCUS_DIR="$1/data/incus" CANARY_SESSION="$1/session.json"
              canary_fresh_state_prepare aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
            ''')
            self.assertNotEqual(result.returncode, 0, obstruction)
            self.assertFalse((self.dir / "data/incus").exists())
            path.unlink()

    @unittest.skipUnless(os.geteuid() == 0, "root ownership positive fixture requires root")
    def test_private_ancestors_pass_and_shared_writable_parents_fail(self):
        safe = self.dir / "safe"
        safe.mkdir(mode=0o755)
        result = self.shell('canary_secure_path "$1/safe"')
        self.assertEqual(result.returncode, 0, result.stderr)
        safe.chmod(0o777)
        self.assertNotEqual(self.shell('canary_secure_path "$1/safe"').returncode, 0)
        safe.chmod(0o755)
        (self.dir / "alias").symlink_to(safe, target_is_directory=True)
        self.assertNotEqual(self.shell('canary_secure_path "$1/alias"').returncode, 0)

    def test_canary_path_does_not_widen_normal_data_dir_schema(self):
        schema = CTL.parent.parent / "schema.json"
        result = self.shell(r'''
          SCHEMA="$2"
          POLYSEAT_RELEASE_GATE=canary
          schema_validate DATA_DIR /var/lib/polyseat-unraid-canary || exit 10
          schema_validate DATA_DIR /var/lib/foreign && exit 11
          POLYSEAT_RELEASE_GATE=closed
          schema_validate DATA_DIR /var/lib/polyseat-unraid-canary && exit 12
          schema_validate DATA_DIR /mnt/cache/appdata/polyseat
        ''', str(schema))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixed_operation_allowlist_refuses_arbitrary_exec_network_gpu_and_raw_config(self):
        result = self.shell(r'''
          CURRENT="$1/current" INCUS_DIR="$1/data/incus"
          CANARY_OPERATION=0
          canary_command_allowed query /1.0 || exit 10
          canary_command_allowed start polyseat-canary-cpu && exit 11
          CANARY_OPERATION=1
          canary_command_allowed start polyseat-canary-cpu || exit 12
          canary_command_allowed exec polyseat-canary-cpu -- /bin/busybox echo POLYSEAT_CANARY_OK || exit 13
          canary_command_allowed exec polyseat-canary-cpu -- /bin/sh && exit 14
          canary_command_allowed config set polyseat-canary-cpu raw.lxc bad && exit 15
          canary_command_allowed config device add polyseat-canary-cpu gpu gpu && exit 16
          canary_command_allowed network create incusbr0 && exit 17
          canary_command_allowed start foreign && exit 18
          canary_command_allowed image import /tmp/untrusted --alias polyseat-canary-busybox && exit 19
          exit 0
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_kernel_module_tools_always_refuse_and_existing_tool_dir_is_not_overwritten(self):
        result = self.shell(r'''
          RUN_DIR="$1"
          canary_deny_tools_prepare || exit 10
          for tool in modprobe insmod rmmod btrfs lvm zfs zpool qemu-system-x86_64 nft; do
            "$RUN_DIR/deny-tools/$tool" --help
            [[ $? == 126 ]] || exit 11
          done
          [[ $(ls "$RUN_DIR/deny-tools" | wc -l) == 9 ]] || exit 13
          canary_deny_tools_prepare && exit 12
          exit 0
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (self.dir / "deny-tools").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("$@", path.read_text())

    def test_denied_tools_never_shadow_a_program_incus_requires(self):
        text = CTL.read_text()
        denied = set(re.search(r'^CANARY_DENIED_TOOLS="([^"]*)"', text, re.M).group(1).split())
        required = set(re.search(r'^INCUSD_REQUIRED_PROGRAMS="([^"]*)"', text, re.M).group(1).split())
        self.assertFalse(denied & required, "a deny stub would satisfy Incus' startup check with a fake program")
        self.assertTrue({"modprobe", "insmod", "rmmod", "btrfs", "lvm", "zfs", "zpool", "qemu-system-x86_64"} <= denied)

    def test_emergency_stop_uses_no_api_and_stops_workload_before_supervisor(self):
        result = self.shell(r'''
          POLYSEAT_RELEASE_GATE=canary
          RUN_DIR="$1" PIDFILE="$1/pid" SLOTS="$1/ram/slots"
          canary_cgroup_remove() { printf '%s\n' "$1"; }
          svc_pid() { exit 88; }
          run_incus() { exit 99; }
          svc_stop full
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "/sys/fs/cgroup/polyseat-unraid-canary",
            "/sys/fs/cgroup/polyseat-unraid-canary-watchdog",
        ])

    def test_recovery_never_auto_restarts_canary(self):
        result = self.shell('POLYSEAT_RELEASE_GATE=canary; want_running_after 1')
        self.assertNotEqual(result.returncode, 0)

    def test_prestart_failure_does_not_clean_up_preexisting_cgroups(self):
        result = self.shell(r'''
          POLYSEAT_RELEASE_GATE=canary
          require_approval() { :; }
          require_no_journal() { :; }
          read_pointer() { PTR_CURRENT=aaaa; }
          current_sha_from_link() { printf 'aaaa\n'; }
          canary_image_verify() { :; }
          svc_start() { return 1; }
          svc_stop() { printf 'UNEXPECTED_STOP\n'; }
          cmd_canary_smoke
        ''')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn("UNEXPECTED_STOP", result.stdout)

    def test_failed_smoke_cleans_up_its_own_new_run(self):
        result = self.shell(r'''
          POLYSEAT_RELEASE_GATE=canary
          require_approval() { :; }
          require_no_journal() { :; }
          read_pointer() { PTR_CURRENT=aaaa; }
          current_sha_from_link() { printf 'aaaa\n'; }
          canary_image_verify() { :; }
          svc_start() { CANARY_RUN_OWNED=1; return 1; }
          svc_stop() { printf 'OWN_RUN_STOPPED\n'; }
          cmd_canary_smoke
        ''')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("OWN_RUN_STOPPED", result.stdout)

    def test_emergency_stop_does_not_require_an_approval_token(self):
        result = self.shell(r'''
          POLYSEAT_RELEASE_GATE=canary ROOT=""
          id() { printf '0\n'; }
          canary_approval_for() { exit 99; }
          require_mutation_allowed stop
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guardian_is_in_workload_and_checks_supervisor_identity(self):
        body = CTL.read_text().split("canary_guardian_start() {", 1)[1].split("canary_guardian_check() {", 1)[0]
        self.assertIn('"$group/daemon/cgroup.procs"', body)
        self.assertIn('"/proc/$supervisor/stat"', body)
        self.assertIn('me=$BASHPID', body)
        self.assertIn('"/proc/$me/stat"', body)
        self.assertNotIn('"/proc/$BASHPID/stat"', body)
        self.assertIn('[[ $actual == "$expected" ]]', body)
        self.assertIn('"$group/cgroup.kill"', body)
        self.assertIn('>/dev/null 2>&1 </dev/null 9>&-', body)

    def test_only_image_inside_verified_signed_slot_can_be_consumed(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        bundle = sb.make_bundle("7.5.1", extra={"share/canary/busybox.tar.gz": b"fixed-image-fixture"})
        installed = sb.install(bundle)
        self.assertEqual(installed.code, 0, installed.text)
        script = r'''
          POLYSEAT_ROOT="$2"
          init_paths
          POLYSEAT_RELEASE_GATE=canary
          canary_image_verify "$3"
        '''
        self.assertEqual(self.shell(script, str(sb.root), bundle["sha"]).returncode, 0)
        image = sb.ram / "slots" / bundle["sha"] / "payload/share/canary/busybox.tar.gz"
        image.write_bytes(b"changed-image")
        self.assertNotEqual(self.shell(script, str(sb.root), bundle["sha"]).returncode, 0)

    def test_unsigned_test_bypass_is_not_available_to_canary_gate(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        result = self.shell(r'''
          ROOT="$2"
          POLYSEAT_RELEASE_GATE=canary POLYSEAT_TEST_UNSIGNED=1
          SIGNERS="$1/missing-signers" REVOKED="$1/missing-revocations"
          verify_signature "$1/missing-manifest" "$1/missing-signature"
        ''', str(sb.root))
        self.assertNotEqual(result.returncode, 0)

    def test_budget_stops_for_excess_data_or_symlinked_log(self):
        data = self.dir / "data"
        logs = self.dir / "logs"
        data.mkdir()
        logs.mkdir()
        (data / "file").write_bytes(b"x" * 8192)
        result = self.shell(r'''
          DATA_REAL="$1/data" LOG_DIR="$1/logs"
          CANARY_DATA_MAX_KB=1
          canary_budget_check
        ''')
        self.assertNotEqual(result.returncode, 0)
        (logs / "incusd.log").symlink_to(data / "file")
        result = self.shell(r'''
          DATA_REAL="$1/data" LOG_DIR="$1/logs"
          CANARY_DATA_MAX_KB=262144
          canary_budget_check
        ''')
        self.assertNotEqual(result.returncode, 0)

    def test_budget_tolerates_vanishing_entries_but_requires_a_numeric_total(self):
        data = self.dir / "data"
        logs = self.dir / "logs"
        data.mkdir()
        logs.mkdir()
        # GNU du exits 1 when an entry disappears mid-walk but still prints the total.
        vanished = self.shell(r"""
          DATA_REAL="$1/data" LOG_DIR="$1/logs" CANARY_DATA_MAX_KB=262144
          du() { printf '8\t%s\n' "$DATA_REAL"; printf 'du: cannot access x\n' >&2; return 1; }
          canary_budget_check
        """)
        self.assertEqual(vanished.returncode, 0, vanished.stderr)
        for fake in ("du() { return 1; }", "du() { printf 'x\\t%s\\n' \"$DATA_REAL\"; }",
                     "du() { printf '262145\\t%s\\n' \"$DATA_REAL\"; return 1; }"):
            with self.subTest(fake=fake):
                result = self.shell('DATA_REAL="$1/data" LOG_DIR="$1/logs" CANARY_DATA_MAX_KB=262144\n'
                                    + fake + "\ncanary_budget_check")
                self.assertNotEqual(result.returncode, 0)

    def test_real_du_race_is_not_a_budget_failure(self):
        data = self.dir / "data"
        (data / "churn").mkdir(parents=True)
        (self.dir / "logs").mkdir()
        result = self.shell(r"""
          DATA_REAL="$1/data" LOG_DIR="$1/logs" CANARY_DATA_MAX_KB=262144
          ( for ((round = 0; round < 400; round++)); do
              for i in 1 2 3 4 5 6 7 8; do : > "$DATA_REAL/churn/f$i"; done
              for i in 1 2 3 4 5 6 7 8; do rm -f -- "$DATA_REAL/churn/f$i"; done
            done ) &
          churn=$!
          failures=0
          for ((n = 0; n < 150; n++)); do canary_budget_check || failures=$((failures + 1)); done
          wait "$churn"
          exit "$failures"
        """)
        self.assertEqual(result.returncode, 0, "budget check failed while files vanished")

    def test_canary_tool_and_topology_checks_precede_one_shot_state(self):
        text = CTL.read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"command -v [A-Za-z][^ \"]* [A-Za-z]",
                            "`command -v a b` succeeds if any one name exists")
        start = text.split("svc_start() {", 1)[1].split("svc_stop() {", 1)[0]
        for earlier in ('for tool in unshare setpriv mount umount setsid renice; do command -v "$tool"',
                        "canary_cgroup_precheck || return 1"):
            self.assertLess(start.index(earlier), start.index("canary_fresh_state_prepare"))
            self.assertLess(start.index(earlier), start.index("canary_deny_tools_prepare"))
        precheck = text.split("canary_cgroup_precheck() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotRegex(precheck, r"\bmkdir\b|\brmdir\b|>\s*\"?\$|\bprintf\b[^\n]*>")
        prepare = text.split("canary_cgroup_prepare() {", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(prepare.index("canary_cgroup_precheck || return 1"), prepare.index('mkdir -- "$CANARY_CGROUP"'))

    # ---- run-2 fixes: daemon programs, cgroup2-unraid teardown, leak check, health timeout ----
    def fake_programs(self, names, where):
        where.mkdir(parents=True, exist_ok=True)
        for name in names:
            tool = where / name
            tool.write_text("#!/bin/sh\nexit 0\n")
            tool.chmod(0o755)

    def programs_check(self, gate):
        return self.shell(r"""
          POLYSEAT_RELEASE_GATE=""" + gate + r"""
          SAFE_PATH="$1/host"
          daemon_programs_check "$1/slot"
        """)

    def test_daemon_programs_check_requires_the_bundled_unsquashfs_for_canary(self):
        self.fake_programs(["ip", "rsync", "setfattr", "tar", "xz"], self.dir / "host")
        (self.dir / "slot/payload/tools").mkdir(parents=True)
        missing = self.programs_check("canary")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("bundled unsquashfs missing", missing.stderr)
        # A host copy must not stand in for the reviewed bundle on the canary.
        self.fake_programs(["unsquashfs"], self.dir / "host")
        self.assertNotEqual(self.programs_check("canary").returncode, 0)
        self.fake_programs(["unsquashfs"], self.dir / "slot/payload/tools")
        self.assertEqual(self.programs_check("canary").returncode, 0)

    def test_daemon_programs_check_reports_each_missing_host_program(self):
        self.fake_programs(["ip", "setfattr", "tar", "xz"], self.dir / "host")
        self.fake_programs(["unsquashfs"], self.dir / "slot/payload/tools")
        result = self.programs_check("canary")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required daemon programs missing on PATH: rsync", result.stderr)
        # The exact Tower failure of run 1: nothing provides unsquashfs at all.
        (self.dir / "slot/payload/tools/unsquashfs").unlink()
        self.fake_programs(["rsync"], self.dir / "host")
        self.assertIn("missing on PATH: unsquashfs", self.programs_check("test-host").stderr)
        self.fake_programs(["unsquashfs"], self.dir / "host")
        self.assertEqual(self.programs_check("test-host").returncode, 0)

    def test_program_check_precedes_one_shot_state_and_daemon_path_prefers_bundle(self):
        text = CTL.read_text()
        start = text.split("svc_start() {", 1)[1].split("svc_stop() {", 1)[0]
        self.assertLess(start.index('daemon_programs_check "$slot_real"'), start.index("canary_fresh_state_prepare"))
        self.assertIn('PATH="$slot_real/payload/tools:$SAFE_PATH"', start)
        launch = text.split("canary_launch() {", 1)[1].split("foreign_incusd()", 1)[0]
        self.assertIn('PATH="$RUN_DIR/deny-tools:$slot_real/payload/tools:$SAFE_PATH"', launch)
        self.assertNotIn('PATH="$RUN_DIR/deny-tools:$SAFE_PATH"', launch)

    def test_required_program_list_is_extracted_exactly_like_the_build_gate(self):
        line = '\tneededPrograms := []string{"ip", "rsync", "setfattr", "tar", "unsquashfs", "xz"}\n'
        source = self.dir / "main_daemon.go"
        source.write_text("package main\n" + line)
        extracted = subprocess.run(
            ["sh", "-c", "sed -n 's/^[[:space:]]*neededPrograms := \\[\\]string{\\(.*\\)}$/\\1/p' \"$1\" | tr -d '\",'",
             "x", str(source)], capture_output=True, text=True, check=True).stdout.strip()
        controller = re.search(r'^INCUSD_REQUIRED_PROGRAMS="([^"]*)"', CTL.read_text(), re.M).group(1)
        self.assertEqual(extracted, controller)
        build = (ROOT / "unraid/scripts/lib/build-core.sh").read_text()
        self.assertIn("neededPrograms := \\[\\]string{", build)
        self.assertIn("differs from Incus neededPrograms", build)

    def remove_group(self, background="", sleep_stub=""):
        group = self.dir / "cg"
        group.mkdir()
        (group / "cgroup.kill").write_text("")  # a regular file keeps rmdir busy, like live tasks would
        return self.shell(r"""
          POLYSEAT_RELEASE_GATE=canary CANARY_CGROUP="$1/cg" CANARY_WATCHDOG_CGROUP="$1/wd"
          fs_type() { echo cgroup2fs; }
          """ + sleep_stub + r"""
          """ + background + r"""
          canary_cgroup_remove "$CANARY_CGROUP"
        """)

    def test_group_removed_by_cgroup2_unraid_is_a_completed_removal(self):
        result = self.remove_group(background=r'( sleep 0.3; rm -f -- "$1/cg/cgroup.kill"; rmdir -- "$1/cg" ) &')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("No such file", result.stderr)

    def test_group_that_never_empties_still_fails(self):
        result = self.remove_group(sleep_stub="sleep() { :; }")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.dir / "cg").exists())

    def test_leak_check_finds_surviving_supervisor_and_bundled_binaries(self):
        slots = self.dir / "ram/slots/abc"
        slots.mkdir(parents=True)
        # Named "sleep" so that multi-call coreutils builds still behave as sleep.
        sleeper = slots / "sleep"
        sleeper.write_bytes(Path("/bin/sleep").read_bytes())
        sleeper.chmod(0o755)
        clean = self.shell('SLOTS="$1/ram/slots"; canary_leak_check')
        self.assertEqual(clean.returncode, 0, clean.stderr)
        # The trailing ':' stops bash from exec-replacing itself, so argv0 stays visible.
        for argv in ([str(sleeper), "30"], ["bash", "-c", "sleep 30; :", "canary-guardian"]):
            with self.subTest(argv=argv[0]):
                leak = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.addCleanup(leak.kill)
                time.sleep(0.2)
                self.assertIsNone(leak.poll(), "the simulated leak must really be running")
                result = self.shell('SLOTS="$1/ram/slots"; canary_leak_check')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(leak.pid), result.stderr)
                leak.kill()
                leak.wait()
        self.assertNotEqual(self.shell('SLOTS=""; canary_leak_check').returncode, 0)

    def test_canary_health_timeout_is_fixed_and_not_user_tunable(self):
        text = CTL.read_text()
        self.assertRegex(text, r"(?m)^CANARY_HEALTH_TIMEOUT=60\b")
        start = text.split("svc_start() {", 1)[1].split("svc_stop() {", 1)[0]
        canary_branch = start.split('timeout=$CANARY_HEALTH_TIMEOUT', 1)
        self.assertEqual(len(canary_branch), 2)
        self.assertIn("if [[ $POLYSEAT_RELEASE_GATE == canary ]]; then\n    timeout=$CANARY_HEALTH_TIMEOUT", start)
        ci = (ROOT / "unraid/scripts/ci-e2e-canary.sh").read_text()
        self.assertNotIn("HEALTH_TIMEOUT", ci, "the rehearsal must run with the same health timeout as Tower")

    def test_activation_probes_the_bundled_unsquashfs(self):
        sha = "a" * 64
        slot = self.dir / "ram/slots" / sha
        for path, body in (("payload/bin/incus", "echo 7.5.1"), ("payload/bin/incusd", "echo 7.5.1"),
                           # Like upstream: the version first, then exit 1 because no image was named.
                           ("payload/tools/unsquashfs", "echo 'unsquashfs version 4.7.5 (2026/03/01)'; exit 1")):
            target = slot / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\n" + body + "\n")
            target.chmod(0o755)
        (self.dir / "bundles").mkdir()
        manifest = {"incus_version": "7.5.1", "files": [{"path": "payload/tools/unsquashfs", "mode": 493}]}
        (self.dir / f"bundles/{sha}.manifest.json").write_text(json.dumps(manifest))
        script = r"""
          POLYSEAT_RELEASE_GATE=$2 SLOTS="$1/ram/slots" BUNDLE_DIR="$1/bundles" RUN_DIR="$1" SAFE_PATH=/usr/bin:/bin
          smoke_slot """ + sha
        self.assertEqual(self.shell(script, "canary").returncode, 0)
        for broken in ("exit 3", "echo 'unsquashfs version 4.7.5'; exit 127", "echo 'error while loading'; exit 1",
                       "echo 'unsquashfs version 4.7.5'; kill -SEGV $$"):
            with self.subTest(broken=broken):
                (slot / "payload/tools/unsquashfs").write_text("#!/bin/sh\n" + broken + "\n")
                self.assertNotEqual(self.shell(script, "canary").returncode, 0)
        (self.dir / f"bundles/{sha}.manifest.json").write_text(json.dumps({"incus_version": "7.5.1", "files": []}))
        self.assertNotEqual(self.shell(script, "canary").returncode, 0, "canary bundles must ship unsquashfs")
        self.assertEqual(self.shell(script, "test-host").returncode, 0)

    def test_launcher_and_supervisor_ordering_contract(self):
        text = CTL.read_text()
        start = text.split("svc_start() {", 1)[1].split("svc_stop() {", 1)[0]
        self.assertLess(start.index("canary_fresh_state_prepare"), start.index("canary_launch"))
        self.assertLess(start.index("canary_watchdog_start"), start.index("canary_launch"))
        self.assertLess(start.index("canary_guardian_start"), start.index("canary_launch"))
        launch = text.split("canary_launch() {", 1)[1].split("foreign_incusd()", 1)[0]
        for phrase in (
            '"$CANARY_CGROUP/daemon/cgroup.procs"',
            'PATH="$RUN_DIR/deny-tools:$slot_real/payload/tools:$SAFE_PATH"',
            "setpriv --bounding-set=-sys_module,-sys_rawio",
            "unshare --mount --cgroup --pid --fork --kill-child=KILL --propagation private",
            "read -r host_pid unused < /proc/self/stat",
            "mount -n -t proc -o nosuid,nodev,noexec proc /proc",
            "umount -n /sys/fs/cgroup",
            "mount -n -t cgroup2 -o nosuid,nodev,noexec none /sys/fs/cgroup",
            '-z $(< /sys/fs/cgroup/cgroup.controllers)',
        ):
            self.assertIn(phrase, launch)
        self.assertNotIn('> "$CANARY_CGROUP/cgroup.subtree_control"', text)
        self.assertIn('CANARY_WATCHDOG_MEMORY_MAX=33554432', text)
        self.assertIn('CANARY_HOST_MIN_AVAILABLE_KB=4194304', text)
        self.assertIn('CANARY_HOST_MAX_LOAD=14', text)

    def private_mount_rebase(self, *, propagation="", namespace="mnt:[11]",
                             readlink_fail=False, unmount_fail=False,
                             mount_fail=False, fresh_root="/"):
        # Execute the actual literal child section with inert mount stubs. This
        # regression never unshares, mounts or unmounts anything on the runner.
        launch = CTL.read_text().split("canary_launch() {", 1)[1].split("foreign_incusd()", 1)[0]
        child = shlex.split(launch.split("bash -u -o pipefail -c ", 1)[1])[0]
        section = child.split("# BEGIN_PRIVATE_CGROUP_REBASE", 1)[1].split("# END_PRIVATE_CGROUP_REBASE", 1)[0]
        section = section.split("\n", 1)[1]
        mountinfo = self.dir / "mountinfo"
        mountinfo.write_text(
            f"1 0 0:1 / /sys rw {propagation} - sysfs sysfs rw\n"
            "2 1 0:2 /.. /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
        )
        fresh = self.dir / "fresh"
        fresh.write_text(f"2 1 0:2 {fresh_root} /sys/fs/cgroup rw - cgroup2 none rw\n")
        trace = self.dir / "trace"
        trace.write_text("")
        section = section.replace("/proc/self/mountinfo", shlex.quote(str(mountinfo)))
        stubs = r'''
          readlink() {
            [[ $TEST_READLINK_FAIL != 1 ]] || return 1
            printf '%s\n' "$TEST_NAMESPACE"
          }
          umount() {
            printf 'umount %s\n' "$*" >> "$TEST_TRACE"
            [[ $TEST_UNMOUNT_FAIL != 1 ]]
          }
          mount() {
            printf 'mount %s\n' "$*" >> "$TEST_TRACE"
            if [[ $3 == cgroup2 ]]; then
              [[ $TEST_MOUNT_FAIL != 1 ]] || return 1
              cp -- "$TEST_FRESH" "$TEST_MOUNTINFO"
            fi
          }
        '''
        result = subprocess.run(
            ["bash", "-u", "-o", "pipefail", "-c", stubs + section,
             "mount-regression", "unused-pidfile", "mnt:[10]"],
            env={"PATH": "/usr/bin:/bin", "TEST_NAMESPACE": namespace,
                 "TEST_READLINK_FAIL": str(int(readlink_fail)),
                 "TEST_UNMOUNT_FAIL": str(int(unmount_fail)),
                 "TEST_MOUNT_FAIL": str(int(mount_fail)), "TEST_TRACE": str(trace),
                 "TEST_FRESH": str(fresh), "TEST_MOUNTINFO": str(mountinfo)},
            capture_output=True, text=True, timeout=5, check=False,
        )
        return result, trace.read_text().splitlines()

    def test_private_cgroup_rebase_unmounts_before_fresh_mount(self):
        result, commands = self.private_mount_rebase()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(commands, [
            "mount -n -t proc -o nosuid,nodev,noexec proc /proc",
            "umount -n /sys/fs/cgroup",
            "mount -n -t cgroup2 -o nosuid,nodev,noexec none /sys/fs/cgroup",
        ])

    def test_private_cgroup_rebase_rejects_shared_parent_or_same_namespace(self):
        for options in ({"propagation": "shared:8"}, {"propagation": "master:7"},
                        {"propagation": "propagate_from:9"}, {"namespace": "mnt:[10]"},
                        {"namespace": "invalid"}, {"readlink_fail": True}):
            with self.subTest(options=options):
                result, commands = self.private_mount_rebase(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(commands, [])
                self.assertIn("canary private mount failure", result.stderr)

    def test_private_cgroup_rebase_has_no_forced_fallback(self):
        result, commands = self.private_mount_rebase(unmount_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands[-1], "umount -n /sys/fs/cgroup")
        self.assertEqual(len(commands), 2)
        self.assertIn("unmount-cgroup", result.stderr)
        for options, reason in (({"mount_fail": True}, "failure: cgroup"),
                                ({"fresh_root": "/.."}, "failure: cgroup-root")):
            result, commands = self.private_mount_rebase(**options)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(commands), 3)
            self.assertIn(reason, result.stderr)


class CanaryWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="polyseat-canary-timer-")
        self.addCleanup(self.workspace.cleanup)
        self.dir = Path(self.workspace.name)
        (self.dir / "workload").mkdir()
        (self.dir / "supervisor").mkdir()

    def observe_abort(self, condition):
        # All cgroup paths here are ordinary temporary files. The timer only
        # writes "1" into our fake kill file; it cannot signal any host task.
        script = r'''
          source <(sed '$d' "$1")
          ROOT="" LOG_ENABLED=0
          CANARY_CGROUP="$2/workload" CANARY_WATCHDOG_CGROUP="$2/supervisor"
          CANARY_SESSION="$2/session.json"
          canary_cgroup_check() { return 0; }
          canary_budget_check() { return 0; }
          canary_host_pressure_check() { return 0; }
          # Some test executors expose an outer /proc alongside inner PIDs.
          # Identity binding is covered by the real cgroup/namespace CI run.
          canary_process_starttime() { printf '12345\n'; }
          sync_path() { :; }
          case "$3" in
            expiry) expiry=$(( $(date -u +%s) - 1 )); deadline=$(( $(canary_monotonic_seconds) + 60 )) ;;
            monotonic) expiry=$(( $(date -u +%s) + 60 )); deadline=$(( $(canary_monotonic_seconds) - 1 )) ;;
            budget) expiry=$(( $(date -u +%s) + 60 )); deadline=$(( $(canary_monotonic_seconds) + 60 )); canary_budget_check() { return 1; } ;;
            pressure) expiry=$(( $(date -u +%s) + 60 )); deadline=$(( $(canary_monotonic_seconds) + 60 )); canary_host_pressure_check() { return 1; } ;;
          esac
          canary_watchdog_loop "$expiry" "$deadline"
        '''
        proc = subprocess.Popen(
            ["bash", "-c", script, "canary-timer-unit", str(CTL), str(self.dir), condition],
            env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            kill_file = self.dir / "workload/cgroup.kill"
            while time.monotonic() < deadline and not kill_file.exists():
                time.sleep(0.02)
            self.assertTrue(kill_file.exists(), "timer did not write the isolated fixture kill file")
            self.assertEqual(kill_file.read_text().strip(), "1")
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                self.fail(f"supervisor exited instead of reaping: {stdout}{stderr}")
            reason = (self.dir / "session.json.reason").read_text().strip()
            return reason
        finally:
            proc.terminate()
            proc.communicate(timeout=4)

    def test_wallclock_expiry_aborts(self):
        self.assertEqual(self.observe_abort("expiry"), "expired")

    def test_monotonic_deadline_prevents_clock_rollback_extension(self):
        self.assertEqual(self.observe_abort("monotonic"), "expired")

    def test_growth_budget_aborts_even_with_valid_ttl(self):
        self.assertEqual(self.observe_abort("budget"), "resource-policy-or-budget")

    def test_host_pressure_aborts_even_with_valid_ttl(self):
        self.assertEqual(self.observe_abort("pressure"), "resource-policy-or-budget")


class ProcReadNoiseTests(unittest.TestCase):
    """Tower runs 6-8 logged "/proc/<pid>/cmdline: No such file or directory" during the leak check: the input
    redirection was evaluated before `2>/dev/null`, so a vanished process still printed to stderr."""

    def test_proc_reads_silence_stderr_before_the_input_redirection(self):
        import re
        text = (ROOT / "unraid/plugin/src/emhttp/scripts/polyseatctl").read_text()
        self.assertEqual(re.findall(r'<\s*"\$(?:proc|\{proc\})/[^"]*"\s+2>\s*/dev/null', text), [])
        self.assertIn("""mapfile -d '' -t argv 2>/dev/null < "$proc/cmdline" || continue""", text)


if __name__ == "__main__":
    unittest.main()
