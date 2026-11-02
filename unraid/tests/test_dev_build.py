"""Guard tests for the local-dev build mode (no docker, no compiler, nothing is built or installed)."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "unraid" / "scripts"
GUARDS = SCRIPTS / "lib" / "dev-guards.sh"
PINS = ROOT / "unraid" / "build-pins.env"
WORKFLOW = ROOT / ".github" / "workflows" / "incus-feasibility.yml"


def guard(call: str, *, env: dict | None = None, cwd: Path | None = None):
    """Run one guard function from dev-guards.sh in a clean shell."""
    environment = {"PATH": os.environ["PATH"]}
    environment.update(env or {})
    return subprocess.run(["sh", "-c", f". {GUARDS}; {call}"], capture_output=True, text=True,
                          env=environment, cwd=cwd)


class HostAndCiGuards(unittest.TestCase):
    def test_unraid_root_and_foreign_platforms_are_refused(self):
        with tempfile.NamedTemporaryFile() as marker:
            result = guard(f"dev_guard_host {marker.name} 1000 Linux x86_64")
            self.assertEqual(result.returncode, 2)
            self.assertIn("never run a build on Unraid", result.stderr)
        for args, text in (("/nonexistent 0 Linux x86_64", "root"), ("/nonexistent 1000 Darwin x86_64", "Linux"),
                           ("/nonexistent 1000 Linux aarch64", "x86_64")):
            result = guard(f"dev_guard_host {args}")
            self.assertEqual(result.returncode, 2, args)
            self.assertIn(text, result.stderr)
        self.assertEqual(guard("dev_guard_host /nonexistent 1000 Linux x86_64").returncode, 0)

    def test_any_ci_signal_is_refused_and_the_guard_never_sets_one(self):
        self.assertEqual(guard("dev_guard_not_ci '' '' '' ''").returncode, 0)
        for position in range(4):
            values = ["", "", "", ""]
            values[position] = "true"
            result = guard("dev_guard_not_ci " + " ".join(f"'{v}'" for v in values))
            self.assertEqual(result.returncode, 2, values)
            self.assertIn("CI environment detected", result.stderr)

    def test_wrapper_refuses_inside_ci_without_faking_anything(self):
        env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "GITHUB_ACTIONS": "true"}
        result = subprocess.run(["sh", str(SCRIPTS / "dev-build.sh"), "--dry-run"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("CI environment detected", result.stderr)


class HomeGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-dev-guard-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.uid = os.getuid()

    def check(self, path, uid=None):
        return guard(f"dev_guard_home '{path}' '{self.home}' {self.uid if uid is None else uid}")

    def test_accepts_a_private_directory_below_home_and_a_new_one(self):
        good = self.home / "dev"
        self.assertEqual(self.check(good).returncode, 0)          # not created yet
        good.mkdir(mode=0o700)
        self.assertEqual(self.check(good).returncode, 0)

    def test_rejects_unsafe_locations(self):
        (self.home / "open").mkdir(mode=0o755)
        os.chmod(self.home / "open", 0o755)
        link = self.home / "link"
        link.symlink_to(self.home)
        (self.home / "file").write_text("x")
        cases = [("relative/dir", "absolute"), (f"{self.home}/../outside", "normalized"), (f"{self.home}/a/./b", "normalized"),
                 ("/tmp/elsewhere-polyseat", "below your home"), (str(self.home), "home directory itself"),
                 (str(link), "symlink"), (str(self.home / "file"), "not a directory"),
                 (str(self.home / "open"), "0700")]
        for path, text in cases:
            result = self.check(path)
            self.assertEqual(result.returncode, 2, path)
            self.assertIn(text, result.stderr, path)
        (self.home / "mine").mkdir(mode=0o700)
        result = self.check(self.home / "mine", uid=self.uid + 1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not owned", result.stderr)


class Pins(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory(prefix="polyseat-pins-") as tmp:
            path = Path(tmp) / "pins.env"
            path.write_text(text)
            result = guard(f"dev_load_pins '{path}' && printf '%s\\n' \"$INCUS_COMMIT\"")
            return result, Path(tmp)

    def test_committed_pins_load_and_are_exported(self):
        result = guard(f"dev_load_pins '{PINS}' && env | grep -E '^(INCUS_COMMIT|BUILDER_BASE_IMAGE)='")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INCUS_COMMIT=b20ec6c3", result.stdout)

    def test_pins_match_the_ci_workflow_exactly(self):
        pins = dict(line.split("=", 1) for line in PINS.read_text().splitlines() if line and not line.startswith("#"))
        workflow = WORKFLOW.read_text()
        for key in ("INCUS_COMMIT", "INCUS_RELEASE", "RAFT_COMMIT", "COWSQL_COMMIT", "LXC_COMMIT", "LXC_RELEASE",
                    "LXC_NVIDIA_HOOK_SHA256", "INCUS_PATCH_HOST_VETH_MAC_SHA256"):
            match = re.search(rf"^\s+{key}:\s*(\S+)\s*$", workflow, re.M)
            self.assertIsNotNone(match, key)
            self.assertEqual(match.group(1), pins[key], f"{key} drifted between the workflow and unraid/build-pins.env")
        self.assertEqual(re.search(r"go-version:\s*'([^']+)'", workflow).group(1), pins["GO_VERSION"])

    def test_malformed_and_unsafe_pin_files_are_rejected_without_executing_anything(self):
        good = PINS.read_text()
        sha = "b20ec6c3cb17eaff28fc1145238b8769e44ce39d"
        mutations = [
            good.replace(sha, sha[:39]),                      # short
            good.replace(sha, sha.upper()),                   # upper case
            good + "UNKNOWN_KEY=1\n",
            good + "INCUS_RELEASE=$(touch /tmp/polyseat-pin-pwned)\n",
            good + "INCUS_RELEASE='x y'\n",
            good.replace("@sha256:", ":"),                    # base image not pinned by digest
            re.sub(r"(GO_LINUX_AMD64_SHA256=)[0-9a-f]{2}", r"\g<1>zz", good),
            "\n".join(line for line in good.splitlines() if not line.startswith("LXC_COMMIT")) + "\n",
        ]
        for text in mutations:
            result, _ = self.load(text)
            self.assertEqual(result.returncode, 2, text[-120:])
        self.assertFalse(Path("/tmp/polyseat-pin-pwned").exists())

    def test_pin_file_may_not_be_a_symlink(self):
        with tempfile.TemporaryDirectory(prefix="polyseat-pins-") as tmp:
            link = Path(tmp) / "pins.env"
            link.symlink_to(PINS)
            self.assertEqual(guard(f"dev_load_pins '{link}'").returncode, 2)


class ContainerAndCache(unittest.TestCase):
    def args(self, **kw):
        values = {"uid": "1000", "gid": "1000", "cpus": "4", "mem": "12", "name": "n", "repo": "/r", "cache": "/c", "out": "/o"}
        values.update(kw)
        result = guard("dev_docker_args {uid} {gid} {cpus} {mem} {name} {repo} {cache} {out}".format(**values))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_container_is_unprivileged_limited_and_mounts_only_three_paths(self):
        argv = self.args()
        joined = " ".join(argv)
        self.assertNotIn("--pid", argv)                      # exact element: --pids-limit is fine
        for forbidden in ("--privileged", "--cap-add", "docker.sock", "--device", "--pid=", "--network host", "--net=host",
                          "--userns", "--security-opt apparmor=unconfined", "--security-opt seccomp=unconfined", "SYS_ADMIN"):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(argv[argv.index("--user") + 1], "1000:1000")
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertIn("no-new-privileges", argv)
        self.assertIn("--read-only", argv)
        self.assertEqual(argv[argv.index("--cpus") + 1], "4")
        self.assertEqual(argv[argv.index("--memory") + 1], "12g")
        self.assertEqual(argv[argv.index("--memory-swap") + 1], "12g")        # no swap use beyond the limit
        self.assertIn("--pids-limit", argv)
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        self.assertEqual(mounts, ["/r:/repo:ro", "/c:/cache", "/o:/out"])

    def test_cache_key_isolation(self):
        def key(core="aa", image="img", **pins):
            env = {"RAFT_COMMIT": "r" * 40, "COWSQL_COMMIT": "c" * 40, "LXC_COMMIT": "l" * 40, "GO_VERSION": "1.26.7",
                   "BUILDER_BASE_IMAGE": "ubuntu@sha256:" + "0" * 64}
            env.update(pins)
            exports = " ".join(f"{k}={v}" for k, v in env.items())
            result = guard(f"export {exports}; dev_cache_key {core} {image}")
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        base = key()
        self.assertRegex(base, r"^[0-9a-f]{24}$")
        self.assertEqual(base, key())                                  # stable
        for changed in (key(core="bb"), key(image="other"), key(RAFT_COMMIT="x" * 40), key(COWSQL_COMMIT="x" * 40),
                        key(LXC_COMMIT="x" * 40), key(GO_VERSION="1.26.8"),
                        key(BUILDER_BASE_IMAGE="ubuntu@sha256:" + "1" * 64)):
            self.assertNotEqual(base, changed)

    def test_cache_and_output_paths_are_disjoint_and_never_deleted_by_the_script(self):
        text = (SCRIPTS / "dev-build.sh").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\brm\s+-")
        self.assertNotIn("sudo", code)
        self.assertNotIn("--privileged", code)
        self.assertNotRegex(code, r"(GITHUB_ACTIONS|RUNNER_OS|RUNNER_ENVIRONMENT)=")   # never fakes CI
        self.assertIn('out_dir="$dev_home/out/$run_id"', text)
        self.assertIn('cache_dir="$dev_home/cache"', text)
        self.assertIn("mv \"$deps\" \"$deps.incomplete.", text)          # interrupted builds are moved aside, not removed

    def test_resource_guards_abort_instead_of_starting(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo") as meminfo:
            meminfo.write("MemAvailable:     1048576 kB\n")
            meminfo.flush()
            result = guard(f"dev_guard_resources /tmp 1 12 {meminfo.name}")
            self.assertEqual(result.returncode, 2)
            self.assertIn("memory available", result.stderr)
            meminfo.seek(0)
            meminfo.truncate()
            meminfo.write("MemAvailable:     99999999 kB\n")
            meminfo.flush()
            self.assertEqual(guard(f"dev_guard_resources /tmp 99999999 4 {meminfo.name}").returncode, 2)
            self.assertEqual(guard(f"dev_guard_resources /tmp 1 4 {meminfo.name}").returncode, 0)


class WrapperBehaviour(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-dev-wrap-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}

    def run_wrapper(self, *args, env=None):
        merged = dict(self.env)
        merged.update(env or {})
        return subprocess.run(["sh", str(SCRIPTS / "dev-build.sh"), *args], capture_output=True, text=True, env=merged)

    def test_dry_run_prints_the_plan_and_creates_nothing(self):
        result = self.run_wrapper("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run (nothing created)", result.stdout)
        self.assertIn("--cap-drop", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_bad_arguments_are_refused(self):
        for args in (["--jobs", "0"], ["--jobs", "99"], ["--jobs", "x"], ["--memory", "2"], ["--memory", "64"], ["--bogus"]):
            self.assertEqual(self.run_wrapper(*args, "--dry-run").returncode, 2, args)

    def test_dev_home_outside_home_is_refused(self):
        result = self.run_wrapper("--dry-run", env={"POLYSEAT_DEV_HOME": "/tmp/polyseat-dev-elsewhere"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("below your home", result.stderr)

    def test_inner_mode_cannot_be_started_by_hand_and_never_overwrites(self):
        self.assertIn("internal", self.run_wrapper("--inner", "--dry-run").stderr)
        cache, out = self.home / "cache", self.home / "out"
        cache.mkdir()
        out.mkdir()
        (out / "existing").write_text("keep")
        env = {"POLYSEAT_DEV_INNER": "1", "POLYSEAT_DEV_CACHE": str(cache), "POLYSEAT_DEV_OUT": str(out), "POLYSEAT_DEV_RUNID": "t"}
        result = self.run_wrapper("--inner", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not empty", result.stderr)
        self.assertEqual((out / "existing").read_text(), "keep")
        link = self.home / "cachelink"
        link.symlink_to(cache)
        env["POLYSEAT_DEV_CACHE"] = str(link)
        self.assertIn("symlink", self.run_wrapper("--inner", env=env).stderr)

    def test_native_mode_refuses_when_tools_are_missing_and_installs_nothing(self):
        empty = self.home / "emptybin"
        empty.mkdir()
        for tool in ("sh", "dirname", "id", "uname", "git", "grep", "sed", "cut", "date", "wc", "tr", "sha256sum", "cat", "mkdir", "chmod", "stat", "df", "awk"):
            found = shutil.which(tool)
            if found:
                (empty / tool).symlink_to(found)
        result = self.run_wrapper("--native", env={"PATH": str(empty)})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("native mode needs", result.stderr)
        self.assertIn("nothing is installed", result.stderr)
        self.assertEqual(list(self.home.iterdir()), [emptydir for emptydir in [empty]])   # no dev home was created


class SbomWriterModes(unittest.TestCase):
    def run_sbom(self, mode, out_dir, allowed, extra_env=None):
        modules = Path(out_dir) / "go-modules.txt"
        modules.write_text("example.com/m v1.0.0\n")
        env = {"PATH": os.environ["PATH"], "INCUS_RELEASE": "v7.5.1", "INCUS_COMMIT": "a" * 40,
               "RAFT_COMMIT": "b" * 40, "COWSQL_COMMIT": "c" * 40}
        env.update(extra_env or {})
        args = ["python3", str(SCRIPTS / "make_proof_sbom.py"), "--mode", mode, "--modules", str(modules),
                "--output", str(Path(out_dir) / "sbom.json")]
        if allowed:
            args += ["--allowed-dir", str(allowed)]
        return subprocess.run(args, capture_output=True, text=True, env=env)

    def test_local_dev_mode_has_its_own_guards(self):
        with tempfile.TemporaryDirectory(prefix="polyseat-sbom-") as tmp:
            ok = self.run_sbom("local-dev", tmp, tmp)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            data = json.loads((Path(tmp) / "sbom.json").read_text())
            self.assertIn({"name": "polyseat:build-mode", "value": "local-dev"}, data["metadata"]["properties"])
        with tempfile.TemporaryDirectory(prefix="polyseat-sbom-") as tmp:
            self.assertNotEqual(self.run_sbom("local-dev", tmp, None).returncode, 0)                 # needs --allowed-dir
            self.assertNotEqual(self.run_sbom("local-dev", tmp, "/tmp").returncode, 0)               # wrong directory
            self.assertNotEqual(self.run_sbom("local-dev", tmp, tmp, {"GITHUB_ACTIONS": "true"}).returncode, 0)

    def test_ci_mode_still_requires_a_real_ci_environment(self):
        with tempfile.TemporaryDirectory(prefix="polyseat-sbom-") as tmp:
            result = self.run_sbom("ci", tmp, None)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("only an isolated Linux CI build", result.stderr)


class CiGateIsUntouched(unittest.TestCase):
    def test_the_ci_script_keeps_its_hard_gate_and_uses_the_shared_core(self):
        text = (SCRIPTS / "build-incus-artifact.sh").read_text()
        for line in ('if [ "${CI:-}" != true ] || [ "${GITHUB_ACTIONS:-}" != true ] ||',
                     '   [ "${RUNNER_OS:-}" != Linux ] ||',
                     '   [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ]; then',
                     "  die 'disposable GitHub-hosted Linux Actions runner required'",
                     "  die 'never run this build script on Unraid'"):
            self.assertIn(line, text)
        self.assertLess(text.index("github-hosted ]; then"), text.index("lib/build-core.sh"))
        self.assertNotIn("dev-guards", text)

    def test_the_core_has_no_gate_of_its_own_and_is_only_sourced(self):
        mode = (SCRIPTS / "lib" / "build-core.sh").stat().st_mode
        self.assertFalse(mode & stat.S_IXUSR, "the core must not be directly executable")
        users = [p.name for p in SCRIPTS.glob("*.sh") if "lib/build-core.sh" in p.read_text()]
        self.assertEqual(sorted(users), ["build-incus-artifact.sh", "dev-build.sh"])


if __name__ == "__main__":
    unittest.main()
