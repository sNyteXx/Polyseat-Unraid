"""Level 1 (local, seconds): the LXC nvidia mount hook path without any GPU, daemon or namespace privilege.

Covers the Polyseat wrapper (arguments, exit status, output capture before the instance is deleted, size caps,
environment filter, fallback) and every path of the verbatim upstream hook that does not need a user namespace.
The user-namespace path and LXC's own invocation are rehearsed in Level 2 (unraid/scripts/local-hook-rehearsal.sh,
real incusd + LXC). Neither level reproduces the real nvidia-container-cli talking to an NVIDIA driver.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "unraid/plugin/src/emhttp/scripts/lxc-nvidia-hook"
SHIM = REPO / "unraid/plugin/src/emhttp/scripts/lxc-nvidia-cli"
UPSTREAM = REPO / "unraid/tests/fixtures/lxc-6.0.6-hooks-nvidia"
PINS = dict(line.split("=", 1) for line in (REPO / "unraid/build-pins.env").read_text().splitlines()
            if line and not line.startswith("#"))

STUB_CLI = """#!/bin/bash
# Stub nvidia-container-cli: records argv and the NVIDIA environment, prints, exits with $STUB_RC.
printf '%s\\n' "$@" > "$STUB_DIR/argv"
env | grep -E '^NVIDIA_' | sort > "$STUB_DIR/env"
echo "stub-stdout"
echo "${STUB_MSG:-stub-stderr}" >&2
exit "${STUB_RC:-0}"
"""


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-hook-l1-")
        self.addCleanup(self.tmp.cleanup)
        d = self.d = Path(self.tmp.name)
        self.hooks = d / "hooks"
        self.hooks.mkdir()
        shutil.copy(WRAPPER, self.hooks / "nvidia")
        shutil.copy(UPSTREAM, self.hooks / "nvidia.upstream")
        (self.hooks / "cli").mkdir()
        shutil.copy(SHIM, self.hooks / "cli/nvidia-container-cli")
        for f in ("nvidia", "nvidia.upstream", "cli/nvidia-container-cli"):
            (self.hooks / f).chmod(0o755)
        self.log = d / "hook-log"
        self.log.mkdir(mode=0o700)
        self.bin = d / "bin"
        self.bin.mkdir()
        (self.bin / "nvidia-container-cli").write_text(STUB_CLI)
        (self.bin / "nvidia-container-cli").chmod(0o755)
        self.stub = d / "stub"
        self.stub.mkdir()
        self.rootfs = d / "rootfs"
        self.rootfs.mkdir()

    def env(self, **extra):
        env = {"PATH": f"{self.bin}:/usr/sbin:/usr/bin:/sbin:/bin", "STUB_DIR": str(self.stub),
               "LXC_HOOK_TYPE": "mount", "LXC_HOOK_SECTION": "lxc", "LXC_ROOTFS_MOUNT": str(self.rootfs),
               "LXC_ROOTFS_PATH": f"dir:{self.d}/ct/rootfs", "LXC_NAME": "polyseat-desktop-gpu",
               "NVIDIA_VISIBLE_DEVICES": "GPU-3cf9157a-c7d3-cbe4-f153-82f96a08a660",
               "NVIDIA_DRIVER_CAPABILITIES": "compute,utility,video,graphics",
               "SECRET_TOKEN": "must-not-be-logged", "HOOK_LOG": str(self.log)}
        # Step 4: the supervisor's fresh "allow"; GpuAdmissionTests covers everything else.
        admission = self.d / "gpu-admission"
        if not admission.exists() or admission.read_text().startswith("allow"):
            admission.write_text(f"allow {int(time.time())} 0000:01:00.0\n")
        env["POLYSEAT_GPU_ADMISSION"] = str(admission)
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run_hook(self, hook="nvidia", args=("polyseat-desktop-gpu", "lxc", "mount"), **extra):
        env = self.env(**extra)
        env["POLYSEAT_HOOK_LOG_DIR"] = str(self.log)
        # Like lxc_popen: stdout AND stderr into one pipe.
        return subprocess.run(["/bin/sh", "-c", f'"{self.hooks / hook}" "$@"', "sh", *args], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)

    def run_with_fake_upstream(self, body: str, **extra):
        (self.hooks / "nvidia.upstream").write_text("#!/bin/bash\n" + body)
        return self.run_hook(**extra)


class UpstreamPinTests(unittest.TestCase):
    def test_fixture_is_the_pinned_upstream_hook(self):
        self.assertEqual(hashlib.sha256(UPSTREAM.read_bytes()).hexdigest(), PINS["LXC_NVIDIA_HOOK_SHA256"])


class WrapperTests(Harness):
    def test_arguments_pass_through_with_a_writable_cli_debug_log(self):
        res = self.run_with_fake_upstream('printf "%s\\n" "$@" > "$STUB_DIR/upstream-argv"\n')
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual((self.stub / "upstream-argv").read_text().split("\n")[:5],
                         ["--debug", str(self.log / "nvidia-container-cli.log"), "polyseat-desktop-gpu", "lxc", "mount"])

    def test_exit_status_is_preserved_and_recorded(self):
        for rc in (0, 1, 2, 126, 137):
            with self.subTest(rc=rc):
                res = self.run_with_fake_upstream(f"echo out; echo err >&2; exit {rc}\n")
                self.assertEqual(res.returncode, rc)
                self.assertEqual((self.log / "status").read_text().strip(), str(rc))
                self.assertEqual(sorted((self.log / "hook.out").read_text().split()), ["err", "out"])

    def test_large_output_is_capped_without_changing_the_hook(self):
        res = self.run_with_fake_upstream("head -c 2000000 /dev/zero | tr '\\0' x; exit 3\n")
        self.assertEqual(res.returncode, 3)  # no SIGPIPE: the sink drains past the cap
        self.assertEqual((self.log / "hook.out").stat().st_size, 262144)

    def test_environment_record_is_filtered(self):
        self.run_with_fake_upstream("exit 0\n")
        text = (self.log / "env.txt").read_text()
        self.assertIn("NVIDIA_VISIBLE_DEVICES=GPU-3cf9157a", text)
        self.assertIn("LXC_HOOK_TYPE=mount", text)
        self.assertIn("uid_map=", text)
        self.assertIn(f"cli={self.hooks}/cli/nvidia-container-cli", text)  # the shim, first on PATH
        self.assertIn(f"POLYSEAT_NVC_REAL={self.bin}/nvidia-container-cli", text)
        self.assertNotIn("must-not-be-logged", text)
        self.assertNotIn("STUB_DIR", text)

    def test_without_a_usable_log_directory_it_is_the_plain_hook(self):
        shutil.rmtree(self.log)
        res = self.run_with_fake_upstream('printf "%s\\n" "$@" > "$STUB_DIR/upstream-argv"; exit 5\n')
        self.assertEqual(res.returncode, 5)
        self.assertEqual((self.stub / "upstream-argv").read_text().split(), ["polyseat-desktop-gpu", "lxc", "mount"])
        self.assertFalse(self.log.exists())

    @unittest.skipIf(os.geteuid() == 0, "root can write a 0500 directory")
    def test_unwritable_log_directory_falls_back(self):
        self.log.chmod(0o500)
        self.addCleanup(self.log.chmod, 0o700)
        res = self.run_with_fake_upstream("exit 4\n")
        self.assertEqual(res.returncode, 4)
        self.assertFalse((self.log / "status").exists())


# The upstream hook's own CLI lookup and call (lines 33-34 and 258 of the pinned hook), with a typical argv.
HOOK_LIKE_CALL = """export PATH=$PATH:/usr/sbin:/usr/bin:/sbin:/bin
command -v nvidia-container-cli >/dev/null || { echo "ERROR: Missing tool nvidia-container-cli" >&2; exit 1; }
exec nvidia-container-cli --debug=/x/cli.log --user configure --no-cgroups --ldconfig=@/sbin/ldconfig /x/rootfs
"""
HOOK_ARGV = ["--debug=/x/cli.log", "--user", "configure", "--no-cgroups", "--ldconfig=@/sbin/ldconfig", "/x/rootfs"]


class CliShimTests(Harness):
    """Tower run 7: pivot_root fails on Unraid's initramfs root, so the CLI must run with --no-pivot."""

    def argv(self):
        return (self.stub / "argv").read_text().splitlines()

    def test_hook_reaches_the_real_cli_with_no_pivot_first_and_its_status(self):
        for rc in (0, 1):
            with self.subTest(rc=rc):
                res = self.run_with_fake_upstream(HOOK_LIKE_CALL, STUB_RC=str(rc))
                self.assertEqual(res.returncode, rc, res.stdout)
                self.assertEqual(self.argv(), ["--no-pivot", *HOOK_ARGV])
                self.assertEqual((self.log / "status").read_text().strip(), str(rc))

    def test_shim_also_applies_without_a_log_directory(self):
        shutil.rmtree(self.log)
        res = self.run_with_fake_upstream(HOOK_LIKE_CALL)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(self.argv(), ["--no-pivot", *HOOK_ARGV])

    def test_without_the_shim_the_cli_call_is_unchanged(self):
        shutil.rmtree(self.hooks / "cli")
        self.assertEqual(self.run_with_fake_upstream(HOOK_LIKE_CALL).returncode, 0)
        self.assertEqual(self.argv(), HOOK_ARGV)

    def test_missing_cli_is_still_reported_by_the_hook(self):
        res = self.run_with_fake_upstream(HOOK_LIKE_CALL, PATH="/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Missing tool nvidia-container-cli", (self.log / "hook.out").read_text())
        self.assertNotIn("POLYSEAT_NVC_REAL", (self.log / "env.txt").read_text())

    def test_shim_refuses_without_an_absolute_real_cli_or_pointing_at_itself(self):
        shim = self.hooks / "cli/nvidia-container-cli"
        for value in (None, "", "nvidia-container-cli", str(shim)):
            with self.subTest(value=value):
                env = {"PATH": "/usr/bin:/bin", "STUB_DIR": str(self.stub)}
                if value is not None:
                    env["POLYSEAT_NVC_REAL"] = value
                res = subprocess.run([str(shim), "info"], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(res.returncode, 1)
                self.assertIn("polyseat: POLYSEAT_NVC_REAL", res.stderr)
                self.assertFalse((self.stub / "argv").exists())


class UpstreamHookPaths(Harness):
    """Paths of the real upstream hook (through the wrapper) that need no user namespace."""

    def test_not_in_a_user_namespace_is_exit_1_with_a_clear_message(self):
        # On a CI runner or UbuntuDev the test process is in the initial user namespace: the hook refuses.
        if Path("/proc/self/uid_map").read_text().split()[:3] != ["0", "0", "4294967295"]:
            self.skipTest("already inside a user namespace")
        res = self.run_hook()
        self.assertEqual(res.returncode, 1)
        self.assertIn("only works in unprivileged mode", (self.log / "hook.out").read_text())
        self.assertFalse((self.stub / "argv").exists())  # the CLI was never reached

    def test_gpu_support_disabled_or_absent_exits_0_before_anything(self):
        for value in ("void", ""):
            with self.subTest(value=value):
                res = self.run_hook(NVIDIA_VISIBLE_DEVICES=value)
                self.assertEqual(res.returncode, 0, res.stdout)
        res = self.run_hook(NVIDIA_VISIBLE_DEVICES=None)
        self.assertEqual(res.returncode, 0)

    def test_wrong_hook_type_or_missing_cli_exit_1(self):
        res = self.run_hook(args=("polyseat-desktop-gpu", "lxc", "start"), LXC_HOOK_TYPE="start")
        self.assertEqual(res.returncode, 1)
        (self.bin / "nvidia-container-cli").unlink()
        res = self.run_hook(PATH="/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Missing tool nvidia-container-cli", (self.log / "hook.out").read_text())

    def test_timing_budget(self):
        started = time.monotonic()
        self.run_hook()
        self.assertLess(time.monotonic() - started, 5)


@unittest.skipUnless(os.environ.get("POLYSEAT_L2_BUNDLE"), "set POLYSEAT_L2_BUNDLE=<sha>.tar.gz to run Level 2 "
                     "(privileged disposable Docker container; also runs in CI job desktop-canary)")
class Level2Rehearsal(unittest.TestCase):
    def test_real_incusd_and_lxc_call_the_hook_as_on_a_host(self):
        out = Path(tempfile.mkdtemp(prefix="polyseat-l2-"))
        cli = subprocess.run(["sh", str(REPO / "unraid/scripts/fetch-nvidia-cli-for-tests.sh"), str(out / "cli")],
                             capture_output=True, text=True, check=True).stdout.strip()
        res = subprocess.run(["bash", str(REPO / "unraid/scripts/local-hook-rehearsal.sh"),
                              os.environ["POLYSEAT_L2_BUNDLE"], str(out / "run"), cli],
                             capture_output=True, text=True, timeout=600)
        summary = (out / "run/summary.txt").read_text()
        self.assertEqual(res.returncode, 0, res.stdout[-3000:] + res.stderr[-3000:])
        self.assertIn("DONE", summary)
        self.assertNotIn("FAIL", summary)
        for case in ("A: 0700 hook path", "B: hook ran as container root", "C: the CLI error text",
                     "D: real CLI reached", "D: CLI debug log opened"):
            self.assertIn(case, summary)


if __name__ == "__main__":
    unittest.main()


class GpuAdmissionTests(unittest.TestCase):
    """Step 4: every start of a GPU container passes the hook, polyseatd's included; only a fresh "allow" from the
    supervisor's VM-priority check lets it touch the GPU."""

    def setUp(self):
        self.h = WrapperTests("test_exit_status_is_preserved_and_recorded")
        self.h.setUp()
        self.addCleanup(self.h.tmp.cleanup)
        (self.h.hooks / "nvidia.upstream").write_text("#!/bin/bash\necho UPSTREAM-RAN\n")
        (self.h.hooks / "nvidia.upstream").chmod(0o755)

    def run_with(self, content):
        admission = self.h.d / "gpu-admission-under-test"  # not the harness' own always-fresh file
        if content is None:
            admission.unlink(missing_ok=True)
            env = {"POLYSEAT_GPU_ADMISSION": str(admission)}
        else:
            admission.write_text(content)
            env = {"POLYSEAT_GPU_ADMISSION": str(admission)}
        start = time.monotonic()
        res = self.h.run_hook(**env)
        return res, time.monotonic() - start

    def test_only_a_fresh_allow_runs_the_hook(self):
        now = int(time.time())
        res, _ = self.run_with(f"allow {now} 0000:01:00.0\n")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("UPSTREAM-RAN", (self.h.log / "hook.out").read_text())
        for content in (f"deny {now} 0000:01:00.0\n", f"allow {now - 60} 0000:01:00.0\n",
                        f"allow {now + 60} 0000:01:00.0\n", "allow notanumber x\n", "", None):
            with self.subTest(content=content):
                (self.h.log / "hook.out").unlink(missing_ok=True)
                res, took = self.run_with(content)
                self.assertEqual(res.returncode, 1)
                self.assertIn("the GPU is not admitted", res.stdout)
                self.assertNotIn("UPSTREAM-RAN", res.stdout + (self.h.log / "hook.out").read_text())
                self.assertLess(took, 8)

    def test_the_supervisor_writes_allow_only_while_the_gpu_check_passes(self):
        ctl = Path(__file__).resolve().parents[1] / "plugin/src/emhttp/scripts/polyseatctl"
        body = ctl.read_text()
        loop = body[body.index("canary_watchdog_loop() {"):body.index("canary_watchdog_start() {")]
        self.assertIn("if [[ $aborted -eq 0 && -n ${CANARY_GPU_PCI:-} ]]; then canary_gpu_admission_write allow", loop)
        self.assertIn("trap 'canary_gpu_admission_write deny;", loop)
        self.assertLess(loop.index("gpu_guard_quick"), loop.index("canary_gpu_admission_write allow"))
        out = self.h.d / "adm"
        res = subprocess.run(["bash", "-c", "source <(sed '$d' \"$1\"); GPU_ADMISSION=$2 CANARY_GPU_PCI=0000:01:00.0 "
                              "canary_gpu_admission_write allow", "t", str(ctl), str(out)],
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        verdict, stamp, pci = out.read_text().split()
        self.assertEqual((verdict, pci), ("allow", "0000:01:00.0"))
        self.assertLess(abs(int(stamp) - time.time()), 5)
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o644")
