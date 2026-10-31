"""Static/guard contracts only; these are NOT a substitute for real canary CI."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "ci-e2e-canary.sh"
SCRIPT = SCRIPT_PATH.read_text()


class CanaryE2EContractTests(unittest.TestCase):
    def test_shell_parses(self):
        subprocess.run(["bash", "-n", str(SCRIPT_PATH)], check=True)

    def test_non_hosted_execution_is_refused_before_any_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = {"PATH": os.environ["PATH"], "CI": "true", "GITHUB_ACTIONS": "true",
                           "RUNNER_OS": "Linux", "RUNNER_ENVIRONMENT": "self-hosted",
                           "GITHUB_RUN_ID": "123", "GITHUB_WORKSPACE": tmp,
                           "RUNNER_TEMP": tmp, "INCUS_PROOF_STAGE": tmp}
            result = subprocess.run(["bash", str(SCRIPT_PATH)], env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn("disposable GitHub-hosted Linux runner required", result.stderr)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_forged_ci_flags_without_hosted_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = {"PATH": os.environ["PATH"], "CI": "true", "GITHUB_ACTIONS": "true",
                           "RUNNER_OS": "Linux", "RUNNER_ENVIRONMENT": "github-hosted",
                           "GITHUB_RUN_ID": "123", "GITHUB_WORKSPACE": tmp,
                           "RUNNER_TEMP": tmp, "INCUS_PROOF_STAGE": tmp}
            result = subprocess.run(["bash", str(SCRIPT_PATH)], env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_real_unraid_is_explicitly_forbidden_before_setup(self):
        self.assertIn("! -e /etc/unraid-version", SCRIPT)
        self.assertIn("$(uname -r) != *[Uu]nraid*", SCRIPT)
        self.assertLess(SCRIPT.index("! -e /etc/unraid-version"), SCRIPT.index("mktemp"))

    def test_only_real_signed_canary_gate_is_rehearsed(self):
        self.assertIn("build-canary-package.py", SCRIPT)
        self.assertIn('POLYSEAT_RELEASE_GATE="canary"', SCRIPT)
        self.assertIn('grep -q \'^POLYSEAT_RELEASE_GATE="closed"\'', SCRIPT)
        self.assertNotIn('POLYSEAT_RELEASE_GATE="test-host"', SCRIPT)
        self.assertNotIn("cfg_get()", SCRIPT)
        self.assertNotIn("canary_host_sha256()", SCRIPT)
        self.assertIn("expect_blocked missing-approval stage", SCRIPT)
        self.assertIn("expect_blocked wrong-signing-key stage", SCRIPT)
        self.assertIn("work/release-key", SCRIPT)
        self.assertIn("work/approval-key", SCRIPT)

    def test_constant_rendering_does_not_modify_quota_or_security_code(self):
        start = SCRIPT.index("for name, value in zip(")
        end = SCRIPT.index("sys.argv[2:]):", start)
        rendering = SCRIPT[start:end]
        for name in ("CANARY_HOST_CPUSET", "CANARY_CPUSET", "CANARY_CGROUP", "CANARY_WATCHDOG_CGROUP", "SAFE_PATH"):
            self.assertIn(name, rendering)
        for name in ("CANARY_MEMORY_MAX", "CANARY_CPU_MAX", "CANARY_PIDS_MAX", "canary_host_sha256"):
            self.assertNotIn(name, rendering)

    def test_fixture_renderer_changes_exactly_the_five_allowed_constants(self):
        code = SCRIPT.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        original = ('POLYSEAT_RELEASE_GATE="canary"  # real gate\n'
                    'SAFE_PATH="/usr/sbin:/usr/bin:/sbin:/bin"\n'
                    'CANARY_HOST_CPUSET="0-27"\nCANARY_CPUSET="24-27"\n'
                    'CANARY_CGROUP="/sys/fs/cgroup/polyseat-unraid-canary"\n'
                    'CANARY_WATCHDOG_CGROUP="/sys/fs/cgroup/polyseat-unraid-canary-watchdog"\n'
                    'CANARY_MEMORY_MAX=536870912\nCANARY_CPU_MAX="100000 1000000"\n'
                    'canary_approval_for() { real_signature_check; }\n')
        tower = "/tmp/polyseat-canary-e2e-Ab3dE6gH/tower-path"
        with tempfile.TemporaryDirectory() as tmp:
            controller = Path(tmp) / "polyseatctl"
            controller.write_text(original)
            result = subprocess.run([sys.executable, "-", str(controller), "0-3", "0",
                                     "/sys/fs/cgroup/ci-a", "/sys/fs/cgroup/ci-b", tower],
                                    input=code, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = controller.read_text()
            self.assertEqual(updated, original.replace('CANARY_HOST_CPUSET="0-27"', 'CANARY_HOST_CPUSET="0-3"')
                             .replace('CANARY_CPUSET="24-27"', 'CANARY_CPUSET="0"')
                             .replace('CANARY_CGROUP="/sys/fs/cgroup/polyseat-unraid-canary"', 'CANARY_CGROUP="/sys/fs/cgroup/ci-a"')
                             .replace('CANARY_WATCHDOG_CGROUP="/sys/fs/cgroup/polyseat-unraid-canary-watchdog"', 'CANARY_WATCHDOG_CGROUP="/sys/fs/cgroup/ci-b"')
                             .replace('SAFE_PATH="/usr/sbin:/usr/bin:/sbin:/bin"', 'SAFE_PATH="' + tower + '"'))
            # Anything but the private Tower-like directory is refused, and nothing is rewritten.
            controller.write_text(original)
            for bad in ("/usr/local/bin:/usr/bin", "/tmp/polyseat-canary-e2e-Ab3dE6gH/tower-path:/usr/bin", ""):
                result = subprocess.run([sys.executable, "-", str(controller), "0-3", "0", "/a", "/b", bad],
                                        input=code, text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, bad)
                self.assertEqual(controller.read_text(), original)

    def test_fixture_renderer_fails_closed_on_missing_constant(self):
        code = SCRIPT.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        original = 'POLYSEAT_RELEASE_GATE="canary"\nCANARY_HOST_CPUSET="0-27"\n'
        with tempfile.TemporaryDirectory() as tmp:
            controller = Path(tmp) / "polyseatctl"
            controller.write_text(original)
            result = subprocess.run([sys.executable, "-", str(controller), "0-3", "0", "/a", "/b",
                                     "/tmp/polyseat-canary-e2e-Ab3dE6gH/tower-path"],
                                    input=code, text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(controller.read_text(), original)

    def test_real_ram_data_and_lifecycle_are_not_mocked(self):
        self.assertIn("mount -t tmpfs -o size=128m,nosuid,nodev,mode=0755", SCRIPT)
        self.assertIn('[[ ! -e $data/incus ]]', SCRIPT)
        self.assertIn("ctl canary-smoke --approve", SCRIPT)
        self.assertIn("observe_container", SCRIPT)
        self.assertIn("^NSpid:", SCRIPT)
        self.assertIn('daemon_depth + 1', SCRIPT)
        self.assertIn('unprivileged root UID mapping', SCRIPT)
        self.assertIn("for ns in mnt cgroup pid; do", SCRIPT)
        self.assertIn("'^POLYSEAT_CANARY_OK$'", SCRIPT)
        for fake in ("POLYSEAT_FAKE", "fake-incusd", "INCUS_MOCK", "FAKE_DB"):
            self.assertNotIn(fake, SCRIPT)

    def test_independent_expiry_uses_real_time_and_fresh_fixture(self):
        self.assertIn("fixture_prepare expiry", SCRIPT)
        self.assertIn("approval 45", SCRIPT)
        self.assertIn('while [[ -e /proc/$daemon_pid ]]', SCRIPT)
        self.assertIn("expect_blocked expired-token start", SCRIPT)
        self.assertIn("populated 0", SCRIPT)
        self.assertIn("workload death was not caused by the real expiry supervisor", SCRIPT)

    def test_real_start_and_reused_state_test_require_explicit_approval(self):
        self.assertEqual(SCRIPT.count("\nctl start --approve\n"), 3)
        self.assertIn('ctl start --approve > "$evidence/reused-state-refusal.txt"', SCRIPT)
        self.assertIn("prior state/session exists|workspace is not empty", SCRIPT)
        self.assertIn("both stop/restart cycles must be evidenced", SCRIPT)

    def test_real_supervisor_loss_uses_only_its_exact_cgroup_and_live_guardian(self):
        case = SCRIPT.split("fixture_prepare supervisor-loss\n", 1)[1].split("\nfixture_finish", 1)[0]
        self.assertIn("approval 600", case)
        self.assertIn("ctl start --approve", case)
        self.assertIn('assert_namespace "$daemon_pid"', case)
        self.assertIn('[[ $watchdog == "/sys/fs/cgroup/polyseat-unraid-canary-watchdog-ci-$GITHUB_RUN_ID-supervisor-loss"', case)
        self.assertIn('printf \'1\\n\' > "$watchdog/cgroup.kill"', case)
        self.assertNotIn('> "$workload/cgroup.kill"', case)
        self.assertNotIn("ctl stop", case)
        self.assertIn('populated 1', case)
        self.assertIn('populated 0', case)
        self.assertIn('attempt<50', case)
        self.assertIn('$(date -u +%s) -lt $supervisor_expiry', case)
        self.assertIn("step supervisor-loss", case)
        self.assertIn('$supervisor_loss[0]', SCRIPT)

    def test_host_snapshot_includes_udp_policy_and_shared_state(self):
        self.assertIn("ss -H -lnt", SCRIPT)
        self.assertIn("ss -H -lnu", SCRIPT)
        self.assertIn("nft --stateless list ruleset", SCRIPT)
        self.assertIn('cp /proc/self/mountinfo "$snapshot/mountinfo"', SCRIPT)
        self.assertIn("cgroup.subtree_control", SCRIPT)
        self.assertIn("sha256sum /etc/subuid /etc/subgid", SCRIPT)
        self.assertNotIn("grep -v '^[#:]'", SCRIPT)
        self.assertNotIn("iptables-save 2>/dev/null || true", SCRIPT)
        self.assertIn("for item in mountinfo cgroups subids lxc-mount-paths; do", SCRIPT)
        self.assertIn("unscoped subordinate ID change", SCRIPT)

    def test_cleanup_never_uses_broad_kills_or_recursive_delete(self):
        self.assertIn('printf \'1\\n\' > "$group/cgroup.kill"', SCRIPT)
        self.assertIn('umount -- "$data"', SCRIPT)
        for unsafe in ("rm -rf", "pkill ", "killall ", "umount -R", "rmmod ", "modprobe "):
            self.assertNotIn(unsafe, SCRIPT)

    def test_cleanup_error_invalidates_machine_readable_success(self):
        cleanup = SCRIPT.split("cleanup() {", 1)[1].split("trap cleanup EXIT", 1)[0]
        self.assertIn('rm -f -- "$proof/canary-rehearsal.json"', cleanup)
        self.assertIn('$passed != 1', cleanup)

    def test_rehearsal_manifest_records_exact_artifacts_and_completed_steps(self):
        for field in ("archive_sha256:$archive", "controller_sha256:$controller",
                      "controller_file_sha256:$controller_file", 'platform:"disposable-linux"'):
            self.assertIn(field, SCRIPT)
        for name in ("signature-stage", "activate", "prestart-policy", "canary-smoke",
                     "expiry-refusal", "cleanup", "host-state-unchanged", "missing-unsquashfs-reproduced",
                     "missing-program-refusal", "tower-sequence", "exec-inventory"):
            self.assertIn("step " + name, SCRIPT)

    def test_tower_parity_fixture_uses_tower_order_actions_path_and_reproduces_run_1(self):
        tower = SCRIPT.split("fixture_prepare tower-sequence", 1)[1].split("fixture_finish", 1)[0]
        self.assertIn("approval 600 '[\"stage\",\"activate\",\"canary-smoke\"]'", tower)
        self.assertNotIn("ctl start", tower)
        self.assertIn('exec: "unsquashfs": executable file not found in $PATH', tower)
        self.assertIn("required daemon programs missing on PATH: rsync", tower)
        self.assertIn("--seccomp-bpf", SCRIPT)
        self.assertIn('env -i PATH="$tower_path"', SCRIPT.split("ctl() {", 1)[1].split("}", 1)[0])
        self.assertIn("unraid/abi/unraid-7.3.3-x86_64.tools", SCRIPT)
        for absent in ("unsquashfs", "mksquashfs", "nft", "python3"):
            self.assertIn(absent, SCRIPT.split("for absent in", 1)[1].split(";", 1)[0])
        self.assertIn("not an Unraid test or production approval", SCRIPT)


if __name__ == "__main__":
    unittest.main()
