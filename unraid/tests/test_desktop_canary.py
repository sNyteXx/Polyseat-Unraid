"""M1 desktop canary: token binding, fixed command list, policy guard and RAM staging.

Disposable keys and /tmp sandboxes only; no Incus daemon, no cgroup and no host path is touched.
"""

from __future__ import annotations

import hashlib
import re
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL, Sandbox

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ROOT_DISK = {"type": "disk", "path": "/", "pool": "default"}
COMMIT = "1" * 40


def make_manifest(image: Path, out: Path, *, unpacked=1_500_000_000, commit=COMMIT, created="1790000000"):
    env = dict(os.environ, SOURCE_DATE_EPOCH=created)
    return subprocess.run([sys.executable, "-I", str(SCRIPTS / "make-seat-manifest.py"), "--image", str(image),
                           "--unpacked-bytes", str(unpacked), "--source-commit", commit, "--out", str(out)],
                          capture_output=True, text=True, env=env, check=False)


def source_ctl(body: str, *args: str, env: dict | None = None):
    script = "source <(sed '$d' \"$1\")\nshift\n" + body
    return subprocess.run(["bash", "-c", script, "desktop-test", str(CTL), *args],
                          env=env or {"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False)


class SeatManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-seat-manifest-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.image = self.dir / "image.tar.xz"
        self.image.write_bytes(b"seat-image-fixture" * 100)

    def test_manifest_has_exactly_the_controller_keys(self):
        res = make_manifest(self.image, self.dir / "m.json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads((self.dir / "m.json").read_text())
        self.assertEqual(sorted(data), ["alias", "created_at", "image_sha256", "image_size", "name", "profiles",
                                        "schema", "selftest", "source_commit", "unpacked_bytes"])
        self.assertEqual(data["image_sha256"], hashlib.sha256(self.image.read_bytes()).hexdigest())

    def test_refuses_oversized_unpack_bad_commit_and_overwrite(self):
        self.assertNotEqual(make_manifest(self.image, self.dir / "a.json", unpacked=4 * 1024**3).returncode, 0)
        self.assertNotEqual(make_manifest(self.image, self.dir / "b.json", commit="abc").returncode, 0)
        (self.dir / "c.json").write_text("{}")
        self.assertNotEqual(make_manifest(self.image, self.dir / "c.json").returncode, 0)


class DesktopAllowListTests(unittest.TestCase):
    def allowed(self, operation: int, command: str) -> bool:
        body = r'''
          POLYSEAT_RELEASE_GATE=canary
          init_paths
          INCUS_DIR=/var/lib/polyseat-unraid-canary/incus
          CANARY_OPERATION=$1
          eval "set -- $2"
          canary_command_allowed "$@"
        '''
        return source_ctl(body, str(operation), command).returncode == 0

    SEQUENCE = (
        "storage create default dir source=/var/lib/polyseat-unraid-canary/incus/storage-pools/default",
        "profile device add default root disk path=/ pool=default",
        "image import /var/lib/polyseat-unraid-seat-image/image --alias polyseat-desktop",
        "init polyseat-desktop polyseat-desktop-cpu --storage default --config boot.autostart=false "
        "--config security.privileged=false --config security.nesting=false --config limits.cpu=2 "
        "--config limits.memory=2GiB --config limits.processes=1024",
        "image delete polyseat-desktop",
        "start polyseat-desktop-cpu",
        "query /1.0/instances/polyseat-desktop-cpu/state",
        "exec polyseat-desktop-cpu -- /usr/local/bin/polyseat-selftest desktop",
        "exec polyseat-desktop-cpu -- /usr/bin/head -c 8388608 /var/lib/polyseat-selftest/screenshot.png",
        "stop polyseat-desktop-cpu --timeout 30",
        "stop polyseat-desktop-cpu --force --timeout 5",
        "delete polyseat-desktop-cpu",
    )

    def test_exact_desktop_sequence_is_allowed_only_for_the_desktop_operation(self):
        for command in self.SEQUENCE:
            with self.subTest(command=command):
                self.assertTrue(self.allowed(2, command))
                if not command.startswith(("storage create", "profile device")):
                    self.assertFalse(self.allowed(1, command))
                    self.assertFalse(self.allowed(0, command) and "query" not in command)

    def test_variations_are_refused(self):
        for command in (
            "exec polyseat-desktop-cpu -- /usr/local/bin/polyseat-selftest nvidia",
            "exec polyseat-desktop-cpu -- /bin/bash",
            "init polyseat-desktop polyseat-desktop-cpu --storage default --config boot.autostart=false "
            "--config security.privileged=true --config security.nesting=false --config limits.cpu=2 "
            "--config limits.memory=2GiB --config limits.processes=1024",
            "config device add polyseat-desktop-cpu gpu gpu pci=0000:01:00.0",
            "config device add polyseat-desktop-cpu eth0 nic nictype=bridged parent=br0",
            "image import /tmp/other.tar.xz --alias polyseat-desktop",
            "image import /var/lib/polyseat-unraid-canary/busybox.tar.gz --alias polyseat-canary-busybox",
            "exec polyseat-canary-cpu -- /bin/busybox echo POLYSEAT_CANARY_OK",
            "start polyseat-canary-cpu",
        ):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(2, command))


class DesktopPolicyGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-desktop-policy-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        cfg = {"image.os": "Archlinux", "volatile.uuid": "a", "boot.autostart": "false",
               "security.privileged": "false", "security.nesting": "false",
               "limits.cpu": "2", "limits.memory": "2GiB", "limits.processes": "1024"}
        self.instance = {"type": "container", "name": "polyseat-desktop-cpu", "profiles": ["default"],
                         "config": dict(cfg), "expanded_config": dict(cfg),
                         "devices": {}, "expanded_devices": {"root": ROOT_DISK}}

    def guard(self, instance, profile="desktop"):
        data = {
            "server": {"config": {}, "environment": {"server_clustered": False}},
            "projects": [{"name": "default", "config": {}}],
            "profiles": [{"name": "default", "config": {}, "devices": {"root": ROOT_DISK}}],
            "instances": [instance],
            "pools": [{"name": "default", "driver": "dir",
                       "config": {"source": "/var/lib/polyseat-unraid-canary/incus/storage-pools/default"}}],
            "networks": [{"name": "lo", "managed": False}],
        }
        for name, value in data.items():
            (self.dir / f"{name}.json").write_text(json.dumps(value))
        body = r'''
          POLYSEAT_RELEASE_GATE=canary
          INCUS_DIR=/var/lib/polyseat-unraid-canary/incus
          canary_budget_select "$1"
          run_incus() {
            case "$2" in
              '/1.0') jq . "$TEST_DIR/server.json" ;;
              '/1.0/projects?recursion=1') jq . "$TEST_DIR/projects.json" ;;
              '/1.0/profiles?recursion=1') jq . "$TEST_DIR/profiles.json" ;;
              '/1.0/instances?recursion=1') jq . "$TEST_DIR/instances.json" ;;
              '/1.0/storage-pools?recursion=1') jq . "$TEST_DIR/pools.json" ;;
              '/1.0/networks?recursion=1') jq . "$TEST_DIR/networks.json" ;;
              *) return 99 ;;
            esac
          }
          canary_profile_guard
        '''
        return source_ctl(body, profile, env={"PATH": "/usr/bin:/bin", "TEST_DIR": str(self.dir)})

    def test_fixed_desktop_instance_passes(self):
        res = self.guard(self.instance)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_desktop_limits_are_exact_values_only(self):
        for key, value in (("limits.memory", "64GiB"), ("limits.cpu", "28"), ("limits.processes", "0")):
            with self.subTest(key=key):
                bad = json.loads(json.dumps(self.instance))
                bad["config"][key] = bad["expanded_config"][key] = value
                self.assertNotEqual(self.guard(bad).returncode, 0)

    def test_gpu_nic_and_raw_settings_are_refused(self):
        for device in ({"type": "gpu", "pci": "0000:01:00.0"},
                       {"type": "nic", "nictype": "bridged", "parent": "br0"},
                       {"type": "unix-char", "source": "/dev/uinput"}):
            with self.subTest(device=device["type"]):
                bad = json.loads(json.dumps(self.instance))
                bad["devices"]["extra"] = device
                bad["expanded_devices"]["extra"] = device
                self.assertNotEqual(self.guard(bad).returncode, 0)
        for key, value in (("nvidia.runtime", "true"), ("raw.lxc", "lxc.apparmor.profile=unconfined"),
                           ("security.privileged", "true")):
            with self.subTest(key=key):
                bad = json.loads(json.dumps(self.instance))
                bad["config"][key] = bad["expanded_config"][key] = value
                self.assertNotEqual(self.guard(bad).returncode, 0)

    def test_cpu_canary_budget_cannot_host_the_desktop_instance_or_its_limits(self):
        self.assertNotEqual(self.guard(self.instance, profile="cpu").returncode, 0)
        busybox = json.loads(json.dumps(self.instance))
        busybox["name"] = "polyseat-canary-cpu"
        self.assertNotEqual(self.guard(busybox, profile="cpu").returncode, 0)

    def test_budgets_are_two_fixed_tuples(self):
        show = 'echo "$CANARY_CPU_MAX|$CANARY_MEMORY_MAX|$CANARY_PIDS_MAX|$CANARY_DATA_MAX_KB|$CANARY_CPUSET|$CANARY_INSTANCE|$CANARY_GPU_PCI"'
        res = source_ctl(f'{show}; canary_budget_select desktop; {show}; canary_budget_select gpu; {show}; '
                         f'canary_budget_select steam; {show}; canary_budget_select bogus || echo refused')
        self.assertEqual(res.stdout.splitlines(), [
            "100000 1000000|536870912|256|262144|24-27|polyseat-canary-cpu|",
            "200000 100000|3221225472|2048|3145728|24-27|polyseat-desktop-cpu|",
            "200000 100000|3221225472|2048|3145728|24-27|polyseat-desktop-gpu|0000:01:00.0",
            "400000 100000|8589934592|4096|8388608|24-27|polyseat-steam-gpu|0000:01:00.0",
            "refused"])


class DesktopApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="polyseat-desktop-approval-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.flash = self.dir / "boot/config/plugins/polyseat-unraid"
        self.flash.mkdir(parents=True)
        self.key = self.dir / "approver"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True)
        public = self.key.with_suffix(".pub").read_text().split()
        (self.flash / "canary_allowed_signers").write_text(
            f'polyseat-unraid-canary-approver namespaces="polyseat-unraid-canary-approval-v1" {public[0]} {public[1]}\n')
        self.bundle = "b" * 64
        state = self.flash / "state"
        state.mkdir()
        (state / "pointer.json").write_text(json.dumps({"current": self.bundle, "previous": None}))
        (state / "pointer.json").chmod(0o600)
        self.image = self.dir / "seat.tar.xz"
        self.image.write_bytes(b"desktop-image")
        self.image_sha = hashlib.sha256(self.image.read_bytes()).hexdigest()
        now = int(time.time())
        self.token = {"schema": 1, "host_sha256": "c" * 64, "archive_sha256": self.bundle,
                      "issued_at": now - 5, "expires_at": now + 900,
                      "actions": ["seat-image-stage", "desktop-smoke"], "seat_image_sha256": self.image_sha}

    def sign(self):
        dest = self.flash / "canary-approval.json"
        dest.write_text(json.dumps(self.token))
        Path(str(dest) + ".sig").unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-n", "polyseat-unraid-canary-approval-v1",
                        "-f", str(self.key), str(dest)], check=True, capture_output=True, stdin=subprocess.DEVNULL)
        os.chmod(dest, 0o600)
        os.chmod(Path(str(dest) + ".sig"), 0o600)

    def check(self, *args):
        body = r'''
          POLYSEAT_ROOT="$1"; shift
          POLYSEAT_RELEASE_GATE=canary
          init_paths
          canary_host_sha256() { printf '%s\n' 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'; }
          canary_approval_for "$@"
        '''
        return source_ctl(body, str(self.dir), *map(str, args)).returncode

    def stage_copy(self, data: bytes):
        seat = self.dir / "var/lib/polyseat-unraid-seat-image"
        seat.mkdir(parents=True, exist_ok=True)
        (seat / "image").write_bytes(data)

    def test_stage_and_smoke_are_bound_to_bundle_and_image(self):
        self.sign()
        self.assertEqual(self.check("seat-image-stage", self.image), 0)
        other = self.dir / "other.tar.xz"
        other.write_bytes(b"something-else")
        self.assertEqual(self.check("seat-image-stage", other), 2)
        self.assertEqual(self.check("desktop-smoke"), 2)  # nothing staged yet
        self.stage_copy(self.image.read_bytes())
        self.assertEqual(self.check("desktop-smoke"), 0)
        self.stage_copy(b"tampered")
        self.assertEqual(self.check("desktop-smoke"), 2)

    def test_desktop_actions_require_the_image_binding_and_vice_versa(self):
        del self.token["seat_image_sha256"]
        self.sign()
        self.assertEqual(self.check("seat-image-stage", self.image), 2)
        self.token["seat_image_sha256"] = self.image_sha
        self.token["actions"] = ["stage", "activate", "canary-smoke"]
        self.sign()
        self.assertEqual(self.check("activate", self.bundle), 2)

    def test_desktop_token_does_not_authorize_cpu_smoke_or_other_bundle(self):
        self.sign()
        self.assertEqual(self.check("canary-smoke"), 2)
        (self.flash / "state/pointer.json").write_text(json.dumps({"current": "f" * 64, "previous": None}))
        (self.flash / "state/pointer.json").chmod(0o600)
        self.stage_copy(self.image.read_bytes())
        self.assertEqual(self.check("desktop-smoke"), 2)


class SeatImageStagingTests(unittest.TestCase):
    def test_seat_image_is_never_staged_below_the_small_run_tmpfs(self):
        # Tower run 3: /run (= /var/run) is a separate 128 MiB tmpfs; a 273 MB image failed there with ENOSPC.
        res = source_ctl('init_paths; echo "$SEAT_DIR|$RUN_DIR"')
        seat, run = res.stdout.strip().split("|")
        self.assertEqual(seat, "/var/lib/polyseat-unraid-seat-image")
        self.assertFalse(seat.startswith(run) or seat.startswith("/run") or seat.startswith("/var/run"))

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        public = self.sb.key.with_suffix(".pub").read_text().split()
        (self.sb.flash / "allowed_signers").write_text(
            f'polyseat-unraid-release namespaces="polyseat-unraid-bundle,polyseat-unraid-seat-image" '
            f'{public[0]} {public[1]}\n')
        self.image = self.sb.work / "seat.tar.xz"
        self.image.write_bytes(os.urandom(4096))
        self.manifest = self.sb.work / "seat.manifest.json"
        res = make_manifest(self.image, self.manifest)
        assert res.returncode == 0, res.stderr

    def sign(self, namespace="polyseat-unraid-seat-image"):
        sig = Path(str(self.manifest) + ".sig")
        sig.unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(self.sb.key), "-n", namespace,
                        str(self.manifest)], check=True, capture_output=True, stdin=subprocess.DEVNULL)
        return sig

    def stage(self, sig):
        return self.sb.ctl("seat-image-stage", self.image, self.manifest, sig, "--approve")

    def test_valid_image_is_staged_privately_and_reverified(self):
        res = self.stage(self.sign())
        self.assertEqual(res.code, 0, res.text)
        seat = self.sb.root / "var/lib/polyseat-unraid-seat-image"
        self.assertEqual(sorted(p.name for p in seat.iterdir()), ["image", "manifest.json", "manifest.json.sig"])
        self.assertEqual((seat / "image").read_bytes(), self.image.read_bytes())
        self.assertEqual(oct(seat.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((seat / "image").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.stage(Path(str(self.manifest) + ".sig")).code, 2)  # never overwritten

    def test_bundle_signature_cannot_be_replayed_for_an_image(self):
        self.assertEqual(self.stage(self.sign("polyseat-unraid-bundle")).code, 5)
        self.assertFalse((self.sb.root / "var/lib/polyseat-unraid-seat-image/image").exists())

    def test_manifest_must_bind_this_exact_image(self):
        sig = self.sign()
        self.image.write_bytes(os.urandom(4096))
        self.assertEqual(self.stage(sig).code, 5)
        data = json.loads(self.manifest.read_text())
        data["extra"] = True
        self.manifest.write_text(json.dumps(data))
        self.assertEqual(self.stage(self.sign()).code, 5)

    def test_uninstall_removes_the_staged_image_exactly_and_keeps_evidence(self):
        self.assertEqual(self.stage(self.sign()).code, 0)
        evidence = self.sb.run_dir / "desktop-evidence"
        evidence.mkdir()
        (evidence / "selftest-start.json").write_text("{}")
        stray = self.sb.root / "var/lib/polyseat-unraid-seat-image/unexpected"
        stray.write_text("not ours")
        res = self.sb.ctl("uninstall", "--approve")
        self.assertEqual(res.code, 0, res.text)
        self.assertFalse((self.sb.root / "var/lib/polyseat-unraid-seat-image/image").exists())
        self.assertTrue(stray.exists())  # unattributed files are kept, never deleted recursively
        self.assertTrue((evidence / "selftest-start.json").exists())

    def test_desktop_smoke_is_refused_outside_a_canary_build(self):
        res = self.sb.ctl("desktop-smoke", "--approve")
        self.assertEqual(res.code, 2, res.text)


if __name__ == "__main__":
    unittest.main()


class GpuCanaryPolicyTests(unittest.TestCase):
    INIT = ("init polyseat-desktop polyseat-desktop-gpu --storage default --config boot.autostart=false "
            "--config security.privileged=false --config security.nesting=false --config limits.cpu=2 "
            "--config limits.memory=2GiB --config limits.processes=1024 --config nvidia.runtime=true "
            "--config nvidia.driver.capabilities=compute,utility,video,graphics")

    def allowed(self, operation, command):
        return DesktopAllowListTests.allowed(self, operation, command)

    def test_gpu_sequence_is_exact_and_only_for_gpu_smoke(self):
        for command in (self.INIT,
                        "config device add polyseat-desktop-gpu gpu gpu pci=0000:01:00.0 mode=0666",
                        "start polyseat-desktop-gpu",
                        "exec polyseat-desktop-gpu -- /usr/local/bin/polyseat-selftest nvidia",
                        "stop polyseat-desktop-gpu --timeout 30", "delete polyseat-desktop-gpu"):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(3, command))
                self.assertFalse(self.allowed(2, command))
                self.assertFalse(self.allowed(1, command))
        for command in ("config device add polyseat-desktop-gpu gpu gpu pci=0000:00:02.0 mode=0666",
                        "config device add polyseat-desktop-gpu gpu gpu mode=0666",
                        "config device add polyseat-desktop-gpu gpu gpu gputype=mig pci=0000:01:00.0",
                        "config device add polyseat-desktop-gpu vfio pci address=0000:01:00.0",
                        self.INIT.replace("compute,utility,video,graphics", "all"),
                        "exec polyseat-desktop-gpu -- /usr/local/bin/polyseat-selftest desktop",
                        "exec polyseat-desktop-gpu -- nvidia-smi -pm 0"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(3, command))

    def test_policy_guard_accepts_only_the_exact_gpu_device_and_config(self):
        guard = DesktopPolicyGuardTests("test_fixed_desktop_instance_passes")
        guard.setUp()
        self.addCleanup(guard.tmp.cleanup)
        inst = json.loads(json.dumps(guard.instance))
        inst["name"] = "polyseat-desktop-gpu"
        for d in (inst["config"], inst["expanded_config"]):
            d["nvidia.runtime"] = "true"
            d["nvidia.driver.capabilities"] = "compute,utility,video,graphics"
        gpu = {"type": "gpu", "pci": "0000:01:00.0", "mode": "0666"}
        inst["devices"]["gpu"] = gpu
        inst["expanded_devices"]["gpu"] = gpu
        self.assertEqual(guard.guard(inst, profile="gpu").returncode, 0)
        self.assertNotEqual(guard.guard(inst, profile="desktop").returncode, 0)  # never in the CPU desktop run
        for mutate in (lambda i: i["devices"]["gpu"].update(pci="0000:00:02.0"),
                       lambda i: i["devices"]["gpu"].update(gputype="mdev"),
                       lambda i: i["config"].update({"nvidia.driver.capabilities": "all"}),
                       lambda i: i["config"].update({"nvidia.require.driver": ">=1"}),
                       lambda i: i["devices"].update(eth0={"type": "nic", "nictype": "bridged", "parent": "br0"})):
            bad = json.loads(json.dumps(inst))
            bad["devices"]["gpu"] = dict(bad["devices"]["gpu"])
            mutate(bad)
            with self.subTest(bad=bad["devices"], cfg=bad["config"]):
                self.assertNotEqual(guard.guard(bad, profile="gpu").returncode, 0)

    def test_gpu_token_needs_the_image_binding(self):
        app = DesktopApprovalTests("test_stage_and_smoke_are_bound_to_bundle_and_image")
        app.setUp()
        self.addCleanup(app.tmp.cleanup)
        app.token["actions"] = ["gpu-smoke"]
        app.sign()
        app.stage_copy(app.image.read_bytes())
        self.assertEqual(app.check("gpu-smoke"), 0)
        self.assertEqual(app.check("desktop-smoke"), 2)
        del app.token["seat_image_sha256"]
        app.sign()
        self.assertEqual(app.check("gpu-smoke"), 2)


class SteamCanaryPolicyTests(unittest.TestCase):
    """steam-smoke (owner decision 2026-10-31): the GPU seat plus compat32 and exactly one LAN NIC, a veth in br0."""
    INIT = ("init polyseat-desktop polyseat-steam-gpu --storage default --config boot.autostart=false "
            "--config security.privileged=false --config security.nesting=false --config limits.cpu=4 "
            "--config limits.memory=8GiB --config limits.processes=4096 --config nvidia.runtime=true "
            "--config nvidia.driver.capabilities=compute,utility,video,graphics,compat32")
    NIC = "config device add polyseat-steam-gpu eth0 nic nictype=bridged parent=br0 hwaddr=00:16:3e:50:53:01 name=eth0"
    ETH0 = {"type": "nic", "nictype": "bridged", "parent": "br0", "hwaddr": "00:16:3e:50:53:01", "name": "eth0"}

    def allowed(self, operation, command):
        return DesktopAllowListTests.allowed(self, operation, command)

    def test_steam_sequence_is_exact_and_only_for_steam_smoke(self):
        for command in (self.INIT, self.NIC,
                        "config device add polyseat-steam-gpu gpu gpu pci=0000:01:00.0 mode=0666",
                        "start polyseat-steam-gpu", "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-selftest steam",
                        "stop polyseat-steam-gpu --timeout 30", "delete polyseat-steam-gpu"):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(4, command))
                for other in (1, 2, 3):
                    self.assertFalse(self.allowed(other, command))
        for command in (self.NIC.replace("parent=br0", "parent=docker0"),
                        self.NIC.replace("nictype=bridged", "nictype=macvlan"),
                        self.NIC.replace("hwaddr=00:16:3e:50:53:01", "hwaddr=00:16:3e:50:53:02"),
                        self.NIC.replace(" hwaddr=00:16:3e:50:53:01", ""),
                        self.NIC + " security.mac_filtering=false",
                        self.NIC.replace("eth0 nic", "eth1 nic").replace("name=eth0", "name=eth1"),
                        "config device add polyseat-steam-gpu proxy proxy listen=tcp:0.0.0.0:47989 connect=tcp:127.0.0.1:47989",
                        self.INIT.replace(",compat32", ""), self.INIT.replace("security.nesting=false", "security.nesting=true"),
                        "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-selftest steam-cpu",
                        "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-selftest nvidia",
                        "exec polyseat-steam-gpu -- /usr/bin/steam"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(4, command))

    def steam_instance(self, guard):
        inst = json.loads(json.dumps(guard.instance))
        inst["name"] = "polyseat-steam-gpu"
        for d in (inst["config"], inst["expanded_config"]):
            d.update({"limits.cpu": "4", "limits.memory": "8GiB", "limits.processes": "4096", "nvidia.runtime": "true",
                      "nvidia.driver.capabilities": "compute,utility,video,graphics,compat32",
                      "volatile.eth0.host_name": "veth1a2b3c4d"})
        gpu = {"type": "gpu", "pci": "0000:01:00.0", "mode": "0666"}
        for d in (inst["devices"], inst["expanded_devices"]):
            d.update(gpu=gpu, eth0=dict(self.ETH0))
        return inst

    def test_policy_guard_accepts_only_the_exact_steam_instance(self):
        guard = DesktopPolicyGuardTests("test_fixed_desktop_instance_passes")
        guard.setUp()
        self.addCleanup(guard.tmp.cleanup)
        inst = self.steam_instance(guard)
        self.assertEqual(guard.guard(inst, profile="steam").returncode, 0, guard.guard(inst, profile="steam").stderr)
        self.assertNotEqual(guard.guard(inst, profile="gpu").returncode, 0)  # the M2 run never has a NIC
        for mutate in (lambda i: i["devices"]["eth0"].update(parent="docker0"),
                       lambda i: i["devices"]["eth0"].update(hwaddr="00:16:3e:50:53:02"),
                       lambda i: i["devices"]["eth0"].update(nictype="macvlan"),
                       lambda i: i["devices"]["eth0"].update({"security.mac_filtering": "false"}),
                       lambda i: i["devices"].update(eth1=dict(self.ETH0, name="eth1")),
                       lambda i: i["config"].update({"nvidia.driver.capabilities": "compute,utility,video,graphics"}),
                       lambda i: i["config"].update({"limits.memory": "2GiB"}),
                       lambda i: i["config"].update({"volatile.eth1.host_name": "veth0"}),
                       lambda i: i["config"].update({"security.nesting": "true"})):
            bad = json.loads(json.dumps(inst))
            mutate(bad)
            with self.subTest(dev=bad["devices"], cfg=bad["config"]):
                self.assertNotEqual(guard.guard(bad, profile="steam").returncode, 0)

    def test_steam_token_needs_the_image_binding(self):
        app = DesktopApprovalTests("test_stage_and_smoke_are_bound_to_bundle_and_image")
        app.setUp()
        self.addCleanup(app.tmp.cleanup)
        app.token["actions"] = ["steam-smoke"]
        app.sign()
        app.stage_copy(app.image.read_bytes())
        self.assertEqual(app.check("steam-smoke"), 0)
        self.assertEqual(app.check("gpu-smoke"), 2)
        del app.token["seat_image_sha256"]
        app.sign()
        self.assertEqual(app.check("steam-smoke"), 2)

    def lan(self, layout):
        root = Path(tempfile.mkdtemp(prefix="polyseat-lan-"))
        self.addCleanup(shutil.rmtree, root)
        net = root / "sys/class/net"
        for name, (bridge, mac, ports) in layout.items():
            (net / name).mkdir(parents=True)
            (net / name / "address").write_text(mac + "\n")
            if bridge:
                (net / name / "bridge").mkdir()
                (net / name / "brif").mkdir()
                for port in ports:
                    (net / name / "brif" / port).mkdir()
        return source_ctl('ROOT="$1"; steam_lan_precheck && echo "OK $(steam_lan_members)"', str(root))

    TOWER_ICD = ('{\n    "file_format_version" : "1.0.1",\n    "ICD": {\n        "library_path": "libGLX_nvidia.so.0",\n'
                 '        "api_version" : "1.4.329"\n    }\n}\n')  # Tower, read-only 2026-10-31

    def icd(self, content=None, path="etc/vulkan/icd.d/nvidia_icd.json", symlink=False):
        root = Path(tempfile.mkdtemp(prefix="polyseat-icd-"))
        self.addCleanup(shutil.rmtree, root)
        target = root / path
        target.parent.mkdir(parents=True)
        if symlink:
            (root / "real.json").write_text(self.TOWER_ICD)
            target.symlink_to(root / "real.json")
        elif content is not None:
            target.write_text(content)
        out = root / "evidence"
        out.mkdir()
        res = source_ctl('ROOT="$1"; steam_vulkan_icd "$2" && cat "$2/nvidia_icd.json"', str(root), str(out))
        return res

    def test_host_vulkan_manifest_is_copied_only_in_the_exact_driver_shape(self):
        res = self.icd(self.TOWER_ICD)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), {"file_format_version": "1.0.1",
                                                  "ICD": {"library_path": "libGLX_nvidia.so.0", "api_version": "1.4.329"}})
        self.assertEqual(self.icd(self.TOWER_ICD, path="usr/share/vulkan/icd.d/nvidia_icd.json").returncode, 0)
        good = json.loads(self.TOWER_ICD)
        for bad in (dict(good, ICD=dict(good["ICD"], library_path="/tmp/evil.so")),
                    dict(good, ICD=dict(good["ICD"], library_path="libvulkan_lvp.so")),
                    dict(good, extra="x"), dict(good, ICD=dict(good["ICD"], is_portability_driver=True)),
                    dict(good, ICD=dict(good["ICD"], api_version="1.4.329; rm -rf /"))):
            with self.subTest(bad=bad):
                self.assertNotEqual(self.icd(json.dumps(bad)).returncode, 0)
        self.assertNotEqual(self.icd(None).returncode, 0)  # missing
        self.assertNotEqual(self.icd(symlink=True).returncode, 0)
        self.assertNotEqual(self.icd(self.TOWER_ICD + " " * 5000).returncode, 0)  # oversized

    def test_vulkan_push_is_exact(self):
        ok = ("file push -p --uid 0 --gid 0 --mode 0644 /var/run/polyseat-unraid/steam-evidence/nvidia_icd.json "
              "polyseat-steam-gpu/usr/share/vulkan/icd.d/nvidia_icd.json")
        self.assertTrue(self.allowed(4, ok))
        self.assertFalse(self.allowed(3, ok))
        for bad in (ok.replace("nvidia_icd.json polyseat", "../../etc/shadow polyseat"),
                    ok.replace("/usr/share/vulkan/icd.d/nvidia_icd.json", "/etc/sudoers"),
                    ok.replace("--mode 0644", "--mode 4755"), "file pull polyseat-steam-gpu/etc/shadow /tmp/x"):
            with self.subTest(bad=bad):
                self.assertFalse(self.allowed(4, bad))

    def test_lan_precheck_needs_br0_as_a_bridge_and_a_free_mac(self):
        res = self.lan({"br0": (True, "aa:bb:cc:00:00:01", ["bond0", "vnet15"]), "bond0": (False, "aa:bb:cc:00:00:02", [])})
        self.assertEqual(res.stdout.strip(), "OK bond0 vnet15", res.stderr)
        self.assertNotIn("OK", self.lan({"br0": (False, "aa:bb:cc:00:00:01", [])}).stdout)  # not a bridge
        self.assertNotIn("OK", self.lan({"eth0": (False, "aa:bb:cc:00:00:01", [])}).stdout)  # no br0
        taken = self.lan({"br0": (True, "aa:bb:cc:00:00:01", []), "veth9": (False, "00:16:3e:50:53:01", [])})
        self.assertNotIn("OK", taken.stdout)
        self.assertIn("already used by veth9", taken.stderr)

    def test_steam_run_checks_token_time_gpu_lan_release_and_needs_the_hook(self):
        body = re.search(r"^cmd_steam_smoke\(\) \(\n(.*?)^\)", CTL.read_text(), re.MULTILINE | re.DOTALL).group(1)
        for needle in ("remaining >= STEAM_SELFTEST_TIMEOUT + 300", 'gpu_preflight_run "$GPU_CANARY_PCI"',
                       "steam_lan_precheck ||", "gpu_release_check ||", '[[ ! -e $(host_path "/sys/class/net/$veth") ]]',
                       'desktop_selftest start "$STEAM_INSTANCE" steam "$dir" "$STEAM_SELFTEST_TIMEOUT"'):
            self.assertIn(needle, body)
        self.assertLess(body.index("steam_lan_precheck ||"), body.index("svc_start"))
        self.assertIn("($CANARY_OPERATION -ne 3 && $CANARY_OPERATION -ne 4) || $LXC_HOOK_READY -eq 1", CTL.read_text())


class NftStubTests(unittest.TestCase):
    """Incus 7.5.1 only knows nftables; Tower has no nft. The canary daemon gets a read-only stub so that a bridged
    NIC's stop can finish and remove its veth (local Steam rehearsal 2026-10-31), and no rule can ever be written."""

    def tools(self):
        run = Path(tempfile.mkdtemp(prefix="polyseat-deny-"))
        self.addCleanup(shutil.rmtree, run)
        res = source_ctl('ROOT="$1"; mkdir -p "$1/var/lib/polyseat-lxc"; canary_deny_tools_prepare && '
                         'ls "$1/var/lib/polyseat-lxc/deny-tools"', str(run))
        self.assertEqual(res.returncode, 0, res.stderr)
        return run / "var/lib/polyseat-lxc/deny-tools", res.stdout.split()

    def test_only_incus_ruleset_read_is_answered_with_an_empty_ruleset(self):
        tools, names = self.tools()
        self.assertEqual(sorted(names), sorted(["modprobe", "insmod", "rmmod", "btrfs", "lvm", "zfs", "zpool",
                                                "qemu-system-x86_64", "nft"]))
        read = subprocess.run([str(tools / "nft"), "--terse", "--json", "-nn", "list", "ruleset"],
                              capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(read.returncode, 0)
        data = json.loads(read.stdout)
        self.assertEqual([list(item) for item in data["nftables"]], [["metainfo"]])  # no table, chain or rule
        for argv in (["create", "table", "incus_test_x"], ["delete", "table", "incus_test_x"], ["list", "ruleset"],
                     ["-f", "/tmp/rules.nft"], ["flush", "chain", "netdev", "incus", "egress_netprio"],
                     ["--terse", "--json", "-nn", "list", "ruleset", ";", "flush", "ruleset"]):
            with self.subTest(argv=argv):
                res = subprocess.run([str(tools / "nft"), *argv], capture_output=True, text=True,
                                     env={"PATH": "/usr/bin:/bin"})
                self.assertEqual(res.returncode, 126)
                self.assertIn("nftables changes are disabled", res.stderr)
        mod = subprocess.run([str(tools / "modprobe"), "nf_tables"], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(mod.returncode, 126)


class SupervisorEnvironmentTests(unittest.TestCase):
    """The expiry supervisor runs `env -i ... bash -u` with an explicit function list: every global those
    functions read must be passed, or `-u` kills the supervisor (CI run 41199384911 failed exactly so)."""

    def test_every_global_read_by_exported_functions_is_passed(self):
        text = CTL.read_text()
        block = re.search(r'^canary_watchdog_start\(\) \{\n(.*?)^\}', text, re.MULTILINE | re.DOTALL).group(1)
        launch = block[block.index("env -i"):block.index("canary-watchdog \"$expiry\"")]
        passed = set(re.findall(r'\b([A-Z][A-Z0-9_]*)=', launch))
        functions = re.search(r'declare -f ([^)]*)\)', launch).group(1).replace("\\\n", " ").split()
        res = subprocess.run(["bash", "-c", 'source <(sed \'$d\' "$1"); shift; declare -f "$@"', "x", str(CTL),
                              *functions], capture_output=True, text=True, check=True)
        used = set(re.findall(r'\$\{?([A-Z][A-Z0-9_]*)', res.stdout))
        allowed_unset = {"POLYSEAT_CI_REAL_SYSTEM", "CI", "GITHUB_ACTIONS", "RUNNER_ENVIRONMENT",  # read with ${X:-}
                         "BASHPID", "PATH"}
        missing = sorted(used - passed - allowed_unset)
        self.assertEqual(missing, [], "globals read under bash -u but not passed to the supervisor")


class FailurePathTests(unittest.TestCase):
    """Tower run 4 failed after start and kept a 1.3 GB container rootfs in RAM: the failure trap must first
    remove the test instance with allow-listed commands, then hard-stop."""

    def test_both_runs_clean_their_instance_before_the_hard_stop(self):
        text = CTL.read_text()
        for function, instance in (("cmd_desktop_smoke", "DESKTOP_INSTANCE"), ("cmd_gpu_smoke", "GPU_INSTANCE"),
                                   ("cmd_steam_smoke", "STEAM_INSTANCE")):
            body = re.search(rf"^{function}\(\) \(\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
            self.assertIn(f'canary_failure_cleanup "${instance}"; svc_stop full', body, function)

    def test_cleanup_uses_only_commands_the_operation_allows(self):
        for operation, instance in ((2, "polyseat-desktop-cpu"), (3, "polyseat-desktop-gpu"), (4, "polyseat-steam-gpu")):
            for command in (f"stop {instance} --force --timeout 5", f"delete {instance}", "image delete polyseat-desktop"):
                with self.subTest(op=operation, command=command):
                    self.assertTrue(DesktopAllowListTests.allowed(self, operation, command))



class LxcHookPathTests(unittest.TestCase):
    """Tower run 5 (M2): the nvidia mount hook runs as the container root inside its user namespace and could not
    reach the 0700 slot ("Script exited with status 126"). It must live in a 0755 chain outside the slot."""
    HOOK = b"#!/bin/bash\necho fixture-hook\n"

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def test_hook_is_materialized_with_the_manifest_hash_and_removed_by_uninstall(self):
        b = self.sb.make_bundle("7.5.1", extra={"share/lxc/hooks/nvidia": self.HOOK},
                                executable=("share/lxc/hooks/nvidia",))
        self.assertEqual(self.sb.install(b).code, 0)
        res = self.sb.ctl("start", "--approve")
        self.assertEqual(res.code, 0, res.text)
        hooks = self.sb.root / "var/lib/polyseat-lxc/hooks"
        upstream, wrapper, shim = hooks / "nvidia.upstream", hooks / "nvidia", hooks / "cli/nvidia-container-cli"
        self.assertEqual(upstream.read_bytes(), self.HOOK)  # verbatim bundle hook
        self.assertEqual(wrapper.read_bytes(), (CTL.parent / "lxc-nvidia-hook").read_bytes())  # diagnostic wrapper
        self.assertEqual(shim.read_bytes(), (CTL.parent / "lxc-nvidia-cli").read_bytes())  # --no-pivot (run 7)
        for f in (upstream, wrapper, shim, hooks, shim.parent):
            self.assertEqual(oct(f.stat().st_mode & 0o777), "0o755", f)
        self.assertTrue((self.sb.root / "var/lib/polyseat-lxc/hook-log").is_dir())
        self.assertEqual(self.sb.ctl("stop", "--approve").code, 0)
        upstream.write_bytes(b"#!/bin/bash\necho tampered\n")  # a changed copy is replaced on the next start
        wrapper.write_bytes(b"#!/bin/bash\nexit 0\n")
        shim.write_bytes(b"#!/bin/bash\nexit 0\n")
        self.assertEqual(self.sb.ctl("start", "--approve").code, 0)
        self.assertEqual(upstream.read_bytes(), self.HOOK)
        self.assertEqual(wrapper.read_bytes(), (CTL.parent / "lxc-nvidia-hook").read_bytes())
        self.assertEqual(shim.read_bytes(), (CTL.parent / "lxc-nvidia-cli").read_bytes())
        log = self.sb.root / "var/lib/polyseat-lxc/hook-log"
        (log / "hook.out").write_text("x")  # wrapper output is removed exactly
        self.assertEqual(self.sb.ctl("uninstall", "--approve").code, 0)
        self.assertFalse(hooks.exists())
        self.assertFalse(log.exists())
        self.assertFalse((self.sb.root / "var/lib/polyseat-lxc").exists())

    def test_daemon_is_never_pointed_into_the_private_slot(self):
        text = CTL.read_text()
        self.assertEqual(text.count('INCUS_LXC_HOOK="$(lxc_mount_real "$LXC_HOOK_DIR")"'), 2)
        self.assertNotIn('INCUS_LXC_HOOK="$slot_real', text)
        self.assertIn('setpriv --reuid="$uid" --regid="$uid" --clear-groups -- test -x "$dir/nvidia" -a -x "$dir/nvidia.upstream" \\\n'
                      '      -a -x "$dir/cli/nvidia-container-cli" -a -w "$logdir"', text)
