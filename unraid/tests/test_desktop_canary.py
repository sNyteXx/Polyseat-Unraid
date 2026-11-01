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
            "--config nvidia.driver.capabilities=compute,utility,video,graphics,compat32,display")
    NIC = "config device add polyseat-steam-gpu eth0 nic nictype=bridged parent=br0 hwaddr=00:16:3e:50:53:01 name=eth0"
    ETH0 = {"type": "nic", "nictype": "bridged", "parent": "br0", "hwaddr": "00:16:3e:50:53:01", "name": "eth0"}
    DISK = "config device add polyseat-steam-gpu steam disk source=/mnt/cache/polyseat/steam-seat path=/home/player/.local/share/Steam"
    STEAM_DISK = {"type": "disk", "source": "/mnt/cache/polyseat/steam-seat", "path": "/home/player/.local/share/Steam"}

    def allowed(self, operation, command):
        return DesktopAllowListTests.allowed(self, operation, command)

    def test_steam_sequence_is_exact_and_only_for_steam_smoke(self):
        for command in (self.INIT, self.NIC, self.DISK,
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
                        self.INIT.replace(",compat32", ""), self.INIT.replace(",display", ""), self.INIT.replace(",display", ",display,all"), self.INIT.replace("security.nesting=false", "security.nesting=true"),
                        "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-selftest steam-cpu",
                        "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-selftest nvidia",
                        "exec polyseat-steam-gpu -- /usr/bin/steam",
                        self.DISK.replace("/mnt/cache/polyseat/steam-seat", "/mnt/cache/appdata"),
                        self.DISK.replace("/mnt/cache/polyseat/steam-seat", "/"),
                        self.DISK.replace("path=/home/player/.local/share/Steam", "path=/etc"),
                        self.DISK + " shift=true", self.DISK + " readonly=false recursive=true"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(4, command))

    def steam_instance(self, guard):
        inst = json.loads(json.dumps(guard.instance))
        inst["name"] = "polyseat-steam-gpu"
        for d in (inst["config"], inst["expanded_config"]):
            d.update({"limits.cpu": "4", "limits.memory": "8GiB", "limits.processes": "4096", "nvidia.runtime": "true",
                      "nvidia.driver.capabilities": "compute,utility,video,graphics,compat32,display",
                      "volatile.eth0.host_name": "veth1a2b3c4d"})
        gpu = {"type": "gpu", "pci": "0000:01:00.0", "mode": "0666"}
        for d in (inst["devices"], inst["expanded_devices"]):
            d.update(gpu=gpu, eth0=dict(self.ETH0), steam=dict(self.STEAM_DISK))
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
                       lambda i: i["config"].update({"security.nesting": "true"}),
                       lambda i: i["devices"]["steam"].update(source="/mnt/cache/appdata"),
                       lambda i: i["devices"]["steam"].update(path="/root"),
                       lambda i: i["devices"]["steam"].update(shift="true"),
                       lambda i: i["devices"].update(data=dict(self.STEAM_DISK, path="/srv"))):
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

    def storage(self, uid=1001000, mode=0o700, symlink=False, missing=False):
        root = Path(tempfile.mkdtemp(prefix="polyseat-storage-"))
        self.addCleanup(shutil.rmtree, root)
        target = root / "mnt/cache/polyseat/steam-seat"
        target.parent.mkdir(parents=True)
        if symlink:
            (root / "elsewhere").mkdir(mode=0o700)
            target.symlink_to(root / "elsewhere")
        elif not missing:
            target.mkdir(mode=mode)
            target.chmod(mode)
        # The fixture cannot chown without root: stat reports the uid/gid under test for the storage directory.
        body = ('init_paths; real_stat=$(command -v stat); '
                'stat() { if [[ ${4:-} == *steam-seat ]]; then echo "$UIDX $UIDX $("$real_stat" -c %a -- "$4")"; '
                'else "$real_stat" "$@"; fi; }; steam_storage_precheck && echo STORAGE_OK')
        return source_ctl(body, env={"PATH": "/usr/bin:/bin", "UIDX": str(uid), "POLYSEAT_ROOT": str(root)})

    def test_storage_precheck_needs_the_players_private_directory(self):
        self.assertIn("STORAGE_OK", self.storage().stdout)
        self.assertNotIn("STORAGE_OK", self.storage(uid=0).stdout)        # root-owned: the player could not write
        self.assertNotIn("STORAGE_OK", self.storage(uid=1000).stdout)     # host uid 1000 is not the seat's player
        self.assertNotIn("STORAGE_OK", self.storage(mode=0o755).stdout)   # others could read the Steam login
        self.assertNotIn("STORAGE_OK", self.storage(symlink=True).stdout)
        self.assertNotIn("STORAGE_OK", self.storage(missing=True).stdout)

    def test_storage_is_checked_before_start_and_never_removed(self):
        text = CTL.read_text()
        body = re.search(r"^steam_seat_run\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(body.index("steam_storage_precheck ||"), body.index("svc_start"))
        pre = re.search(r"^steam_storage_precheck\(\) \{.*?^\}", text, re.MULTILINE | re.DOTALL).group(0)
        for needle in ('[[ $src == "$STEAM_STORAGE_DATASET zfs" ]]', "zfs get -Hp -o value quota", "STEAM_STORAGE_MIN_FREE_KB"):
            self.assertIn(needle, pre)
        self.assertNotIn("STEAM_STORAGE_HOST", re.search(r"^remove_lxc_rootfs_mount\(\) \{.*?^\}", text,
                                                           re.MULTILINE | re.DOTALL).group(0))

    def test_lan_precheck_needs_br0_as_a_bridge_and_a_free_mac(self):
        res = self.lan({"br0": (True, "aa:bb:cc:00:00:01", ["bond0", "vnet15"]), "bond0": (False, "aa:bb:cc:00:00:02", [])})
        self.assertEqual(res.stdout.strip(), "OK bond0 vnet15", res.stderr)
        self.assertNotIn("OK", self.lan({"br0": (False, "aa:bb:cc:00:00:01", [])}).stdout)  # not a bridge
        self.assertNotIn("OK", self.lan({"eth0": (False, "aa:bb:cc:00:00:01", [])}).stdout)  # no br0
        taken = self.lan({"br0": (True, "aa:bb:cc:00:00:01", []), "veth9": (False, "00:16:3e:50:53:01", [])})
        self.assertNotIn("OK", taken.stdout)
        self.assertIn("already used by veth9", taken.stderr)

    def test_steam_run_checks_token_time_gpu_lan_release_and_needs_the_hook(self):
        body = re.search(r"^steam_seat_run\(\) \([^\n]*\n(.*?)^\)", CTL.read_text(), re.MULTILINE | re.DOTALL).group(1)
        for needle in ("(( remaining >= need ))", 'gpu_preflight_run "$GPU_CANARY_PCI"',
                       "steam_lan_precheck ||", "gpu_release_check ||", '[[ ! -e $(host_path "/sys/class/net/$veth") ]]',
                       'desktop_selftest start "$STEAM_INSTANCE" steam "$dir" "$STEAM_SELFTEST_TIMEOUT"'):
            self.assertIn(needle, body)
        self.assertLess(body.index("steam_lan_precheck ||"), body.index("svc_start"))
        self.assertIn("($CANARY_OPERATION -ne 3 && $CANARY_OPERATION -ne 4 && $CANARY_OPERATION -ne 5 && $CANARY_OPERATION -ne 8) ||\n"
                      "     $LXC_HOOK_READY -eq 1", CTL.read_text())


class SteamSessionTests(unittest.TestCase):
    """M3 (owner, 2026-10-31): one bounded LAN session; PIN and stop reach it through root-only files."""
    SESSION = "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-session"

    def allowed(self, operation, command):
        return DesktopAllowListTests.allowed(self, operation, command)

    def test_session_commands_are_exact_and_only_for_the_session_operation(self):
        for command in (f"{self.SESSION} start nvidia", f"{self.SESSION} status", f"{self.SESSION} stop",
                        f"{self.SESSION} pin 0427", SteamCanaryPolicyTests.NIC, SteamCanaryPolicyTests.DISK,
                        SteamCanaryPolicyTests.INIT):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(5, command))
        for command in (f"{self.SESSION} start nvidia", f"{self.SESSION} pin 0427"):
            self.assertFalse(self.allowed(4, command))  # the smoke test never opens a session
        for command in (f"{self.SESSION} pin 427", f"{self.SESSION} pin 04271", f"{self.SESSION} pin 04a7",
                        f"{self.SESSION} pin '0427;id'", f"{self.SESSION} start cpu", f"{self.SESSION} start",
                        "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-selftest steam",
                        "exec polyseat-steam-gpu -- /bin/sh -c id"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(5, command))

    def run_dir(self):
        d = Path(tempfile.mkdtemp(prefix="polyseat-session-"))
        self.addCleanup(shutil.rmtree, d)
        return d

    def op(self, run, *args, answer=None):
        # The session's side of the handoff: answer the PIN file once it appears.
        script = ('POLYSEAT_RELEASE_GATE=canary; RUN_DIR="$1"; shift; '
                  'if [[ -n ${ANSWER:-} ]]; then (for _ in $(seq 50); do [[ -f $RUN_DIR/session-pin ]] && '
                  '{ printf "%s\\n" "$(cat "$RUN_DIR/session-pin")" > "$RUN_DIR/seen-pin"; rm -f "$RUN_DIR/session-pin"; '
                  'printf "%s\\n" "$ANSWER" > "$RUN_DIR/session-pin.result"; break; }; sleep 0.1; done) & fi; '
                  'cmd="$1"; shift; "$cmd" "$@"')
        env = {"PATH": "/usr/bin:/bin"}
        if answer:
            env["ANSWER"] = answer
        return source_ctl(script, str(run), *args, env=env)

    def test_pin_status_and_stop_need_a_running_session(self):
        run = self.run_dir()
        for args in (("cmd_steam_session_pin", "0427"), ("cmd_steam_session_status",), ("cmd_steam_session_stop",)):
            with self.subTest(args=args):
                self.assertNotEqual(self.op(run, *args).returncode, 0)
        self.assertFalse((run / "session-pin").exists())
        self.assertFalse((run / "session-stop").exists())

    def test_pin_is_four_digits_handed_over_and_answered(self):
        run = self.run_dir()
        (run / "session-status.json").write_text('{"ok": true, "ip": "192.168.178.150"}\n')
        for bad in ("427", "04271", "abcd", "04 7"):
            with self.subTest(bad=bad):
                self.assertNotEqual(self.op(run, "cmd_steam_session_pin", bad).returncode, 0)
                self.assertFalse((run / "session-pin").exists())
        res = self.op(run, "cmd_steam_session_pin", "0427", answer='{"ok": true, "paired": true}')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('"paired": true', res.stdout)
        self.assertEqual((run / "seen-pin").read_text().strip(), "0427")
        res = self.op(run, "cmd_steam_session_pin", "1111", answer='{"ok": false, "error": "not paired"}')
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.op(run, "cmd_steam_session_status").stdout.strip(), '{"ok": true, "ip": "192.168.178.150"}')
        self.assertEqual(self.op(run, "cmd_steam_session_stop").returncode, 0)
        self.assertTrue((run / "session-stop").exists())
        self.assertEqual(oct((run / "session-stop").stat().st_mode & 0o777), "0o600")

    def test_operator_commands_need_no_token_and_do_not_queue_behind_the_session(self):
        # The session itself holds the lock and its token for up to 30 minutes.
        for cmd in ("steam-session-pin", "steam-session-status", "steam-session-stop"):
            with self.subTest(cmd=cmd):
                res = source_ctl('POLYSEAT_RELEASE_GATE=canary ROOT=""; id() { printf "0\\n"; }; '
                                 'canary_approval_for() { exit 99; }; require_mutation_allowed "$1"', cmd)
                self.assertEqual(res.returncode, 0, res.stderr)
        main = CTL.read_text().split("\nmain() {", 1)[1]
        early = main[:main.index("  acquire_lock\n")]
        for fn in ("cmd_steam_session_pin", "cmd_steam_session_status", "cmd_steam_session_stop"):
            self.assertIn(fn, early)
        self.assertNotIn("cmd_steam_session_pin", main[main.index("  acquire_lock\n"):])
        self.assertLess(early.index('if [[ $POLYSEAT_RELEASE_GATE == canary ]]; then\n    # The running session'),
                        early.index("cmd_steam_session_pin"))
        # The session start itself still needs its token and the lock.
        res = source_ctl('POLYSEAT_RELEASE_GATE=canary ROOT=""; id() { printf "0\\n"; }; '
                         'canary_approval_for() { exit 99; }; require_mutation_allowed steam-session')
        self.assertEqual(res.returncode, 99)

    def test_session_token_needs_the_image_binding(self):
        app = DesktopApprovalTests("test_stage_and_smoke_are_bound_to_bundle_and_image")
        app.setUp()
        self.addCleanup(app.tmp.cleanup)
        app.token["actions"] = ["steam-session"]
        app.sign()
        app.stage_copy(app.image.read_bytes())
        self.assertEqual(app.check("steam-session"), 0)
        self.assertEqual(app.check("steam-smoke"), 2)

    def test_session_loop_respects_vm_priority_budget_and_token_time(self):
        body = re.search(r"^steam_seat_run\(\) \([^\n]*\n(.*?)^\)", CTL.read_text(), re.MULTILINE | re.DOTALL).group(1)
        loop = body[body.index("while (( $(date -u +%s) < deadline ))"):body.index("done", body.index("while (( $(date"))]
        for needle in ('gpu_guard_quick "$GPU_CANARY_PCI"', "canary_budget_check", "session-stop", "session-pin"):
            self.assertIn(needle, loop)
        self.assertIn("deadline=$(( $(jq -er .expires_at \"$CANARY_APPROVAL\") - STEAM_SESSION_TEARDOWN_SECONDS ))", body)


class PersistentStateTests(unittest.TestCase):
    """Step 2 of the upstream integration plan (owner, 2026-11-01): the Incus state persists on the cache dataset.
    A persisted database is started again only after a recorded clean stop; the persisted seat is never deleted."""
    SEAT = "polyseat-persist-cpu"
    INCUS = "/mnt/cache/polyseat/incus/incus"
    INIT = ("init polyseat-desktop polyseat-persist-cpu --storage default --config boot.autostart=false "
            "--config security.privileged=false --config security.nesting=false --config limits.cpu=2 "
            "--config limits.memory=2GiB --config limits.processes=1024")

    def allowed(self, operation, command):
        body = r"""
          POLYSEAT_RELEASE_GATE=canary
          init_paths
          INCUS_DIR=/mnt/cache/polyseat/incus/incus
          CANARY_OPERATION=$1
          eval "set -- $2"
          canary_command_allowed "$@"
        """
        return source_ctl(body, str(operation), command).returncode == 0

    def test_operation_6_allows_exactly_the_persistent_seat(self):
        for command in (f"storage create default dir source={self.INCUS}/storage-pools/default",
                        "profile device add default root disk path=/ pool=default",
                        "image import /var/lib/polyseat-unraid-seat-image/image --alias polyseat-desktop",
                        "image delete polyseat-desktop", self.INIT, f"start {self.SEAT}",
                        f"query /1.0/instances/{self.SEAT}/state",
                        f"exec {self.SEAT} -- /usr/local/bin/polyseat-selftest desktop",
                        f"stop {self.SEAT} --timeout 30", f"stop {self.SEAT} --force --timeout 5",
                        "admin shutdown --timeout 60", "query /1.0/instances?recursion=1"):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(6, command))
        for command in (f"delete {self.SEAT}", f"delete {self.SEAT} --force", "admin shutdown",
                        self.INIT.replace("boot.autostart=false", "boot.autostart=true"),
                        self.INIT.replace("limits.cpu=2", "limits.cpu=8"), "start polyseat-steam-gpu",
                        f"exec {self.SEAT} -- /bin/sh -c id", "exec polyseat-steam-gpu -- /usr/local/bin/polyseat-selftest steam",
                        f"config set {self.SEAT} boot.autostart=true", "storage delete default"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(6, command))
        for operation in (2, 3, 4, 5):
            self.assertFalse(self.allowed(operation, f"start {self.SEAT}"))
            self.assertFalse(self.allowed(operation, "admin shutdown --timeout 60"))

    def test_token_needs_the_image_binding(self):
        app = DesktopApprovalTests("test_stage_and_smoke_are_bound_to_bundle_and_image")
        app.setUp()
        self.addCleanup(app.tmp.cleanup)
        app.token["actions"] = ["persist-smoke"]
        app.sign()
        app.stage_copy(app.image.read_bytes())
        self.assertEqual(app.check("persist-smoke"), 0)
        self.assertEqual(app.check("steam-smoke"), 2)

    def workspace(self):
        d = Path(tempfile.mkdtemp(prefix="polyseat-persist-"))
        self.addCleanup(shutil.rmtree, d)
        (d / "manifest.json").write_text('{"incus_version": "7.5.1"}\n')
        return d

    def prepare(self, d, version="7.5.1"):
        # The controller runs as root; the tests do not, so the owner column of stat reads as root here.
        script = r"""
          POLYSEAT_RELEASE_GATE=canary
          me=$(id -u)
          stat() { local out; out=$(command stat "$@") || return; printf '%s\n' "${out//$me /0 }"; }
          bundle_manifest() { printf '%s\n' "$1/manifest.json"; }
          DATA_REAL="$1/data" INCUS_DIR="$1/data/incus" CANARY_SESSION="$1/session.json"
          jq -n --arg v "$2" '{incus_version: $v}' > "$1/manifest.json"
          persist_state_prepare "$1" && echo "FRESH=$PERSIST_FRESH"
        """
        return source_ctl(script, str(d), version)

    def clean_stop(self, d, instances, mode=0o600):
        f = d / "data" / "clean-stop.json"
        f.write_text(json.dumps({"schema": 1, "archive_sha256": "a" * 64, "stopped_at": 1, "instances": instances}))
        f.chmod(mode)
        return f

    def test_first_run_creates_the_workspace_and_later_runs_need_a_clean_stop(self):
        d = self.workspace()
        res = self.prepare(d)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("FRESH=1", res.stdout)
        self.assertEqual(oct((d / "data").stat().st_mode & 0o777), "0o711")
        self.assertEqual(oct((d / "data/incus").stat().st_mode & 0o777), "0o711")
        self.assertEqual(json.loads((d / "data/persist.json").read_text())["kind"], "persistent")
        # Second run without a recorded clean stop: refused, nothing changes.
        res = self.prepare(d)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no clean stop", res.stderr)
        # With a clean stop: reopened, and the record is consumed before the daemon starts.
        self.clean_stop(d, [{"name": self.SEAT, "status": "Stopped", "autostart": "false"}])
        res = self.prepare(d)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("FRESH=0", res.stdout)
        self.assertFalse((d / "data/clean-stop.json").exists())
        self.assertTrue((d / "data/clean-stop.used.json").exists())
        self.assertNotEqual(self.prepare(d).returncode, 0)  # consumed: a crash now means inspection

    def test_a_record_that_does_not_prove_a_clean_stop_is_refused(self):
        d = self.workspace()
        self.assertEqual(self.prepare(d).returncode, 0)
        stopped = {"name": self.SEAT, "status": "Stopped", "autostart": "false"}
        for instances, mode in (([dict(stopped, status="Running")], 0o600), ([dict(stopped, status="Running", autostart="")], 0o600),
                                ([dict(stopped, autostart="true")], 0o600), ([dict(stopped, name="Not A Seat")], 0o600),
                                ([stopped, stopped], 0o600), ([stopped], 0o644)):
            with self.subTest(instances=instances, mode=oct(mode)):
                self.clean_stop(d, instances, mode)
                self.assertNotEqual(self.prepare(d).returncode, 0)
                self.assertTrue((d / "data/clean-stop.json").exists())  # left for inspection
        self.clean_stop(d, [stopped])
        self.assertNotEqual(self.prepare(d, version="7.6.0").returncode, 0)  # no silent up- or downgrade
        self.assertEqual(self.prepare(d).returncode, 0)

    def test_clean_stop_needs_stopped_seats_and_a_daemon_that_exits_by_itself(self):
        d = self.workspace()
        (d / "data").mkdir()
        script = r"""
          POLYSEAT_RELEASE_GATE=canary
          D=$1 MODE=$2 DATA_REAL="$1/data" PTR_CURRENT=abc
          run_incus() { case "$*" in 'query /1.0/instances?recursion=1') cat "$D/instances.json" ;;
                                     'admin shutdown --timeout 60') echo shutdown >> "$D/calls"; [[ $MODE == exits ]] && kill "$DAEMON" ;; esac; }
          sleep 300 & DAEMON=$!
          if [[ $MODE == hangs ]]; then sleep() { :; }; fi  # the 30 s wait for an exit, without waiting
          svc_pid() { kill -0 "$DAEMON" 2>/dev/null && echo "$DAEMON"; }
          persist_clean_stop; rc=$?; kill "$DAEMON" 2>/dev/null; exit $rc
        """
        stopped = {"name": self.SEAT, "status": "Stopped", "config": {"boot.autostart": "false"}}
        (d / "instances.json").write_text(json.dumps([dict(stopped, status="Running")]))
        self.assertNotEqual(source_ctl(script, str(d), "exits").returncode, 0)
        self.assertFalse((d / "calls").exists())  # a running seat: no shutdown, no record
        (d / "instances.json").write_text(json.dumps([stopped]))
        res = source_ctl(script, str(d), "exits")
        self.assertEqual(res.returncode, 0, res.stderr)
        record = json.loads((d / "data/clean-stop.json").read_text())
        self.assertEqual(record["instances"], [{"name": self.SEAT, "status": "Stopped", "autostart": "false"}])
        self.assertEqual(oct((d / "data/clean-stop.json").stat().st_mode & 0o777), "0o600")
        (d / "data/clean-stop.json").unlink()
        res = source_ctl(script, str(d), "hangs")
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse((d / "data/clean-stop.json").exists())  # killed or hung: never recorded as clean

    def test_failure_path_stops_but_never_deletes_the_persisted_seat(self):
        d = self.workspace()
        res = source_ctl(r"""
          POLYSEAT_RELEASE_GATE=canary D=$1
          svc_pid() { echo 1; }; canary_watchdog_check() { :; }; canary_keep_diagnostics() { :; }
          run_incus() { echo "CALL $*" >> "$D/calls"; }
          canary_failure_cleanup polyseat-persist-cpu
        """, str(d))
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = (d / "calls").read_text()
        self.assertIn("CALL stop polyseat-persist-cpu --force --timeout 5", calls)
        self.assertNotIn("delete", calls)

    def test_budget_is_free_space_on_the_dataset_not_a_walk_of_the_rootfs(self):
        d = self.workspace()
        script = r"""
          POLYSEAT_RELEASE_GATE=canary CANARY_OPERATION=6 DATA_REAL="$1" LOG_DIR="$1/logs" PERSIST_MIN_FREE_KB=$2
          du() { echo "du must not run" >&2; return 1; }
          canary_budget_check
        """
        self.assertEqual(source_ctl(script, str(d), "1").returncode, 0)
        self.assertNotEqual(source_ctl(script, str(d), str(2 ** 50)).returncode, 0)

    def test_the_mount_root_must_be_root_owned(self):
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-persist"))
        self.addCleanup(shutil.rmtree, d)
        (d / "mnt/cache/polyseat").mkdir(parents=True)
        res = source_ctl(r"""
          POLYSEAT_RELEASE_GATE=canary ROOT="$1" PERSIST_MIN_FREE_KB=1
          sandbox() { return 0; }; ci_real_system() { return 1; }
          persist_validate_data_dir
        """, str(d))
        self.assertNotEqual(res.returncode, 0)  # owned by the test user, not root
        self.assertIn("must be root:root", res.stderr)

    def test_the_command_is_wired_and_documented(self):
        text = CTL.read_text()
        self.assertIn("persist-smoke) cmd_persist_smoke", text)
        self.assertIn("6) canary_approval_for persist-smoke ;;", text)
        self.assertIn('if [[ $CANARY_OPERATION -ge 6 && $CANARY_OPERATION -le 8 ]]; then persist_state_prepare "$active_sha"', text)
        body = re.search(r"^cmd_persist_smoke\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
        self.assertIn('canary_failure_cleanup "$PERSIST_INSTANCE"; svc_stop full', body)
        self.assertLess(body.index("persist_clean_stop || exit"), body.index("  svc_stop full || exit"))
        self.assertNotIn("run_incus delete", body)


class PolyseatdTests(unittest.TestCase):
    """Step 3 (owner, 2026-11-01: "muss in LAN sein"): upstream's polyseatd on the persistent Incus, its interface on
    the host bridge's LAN address only, the password set before its first start, the binary pinned by the package."""
    PIN = "ab" * 32

    def allowed(self, operation, command):
        return PersistentStateTests.allowed(self, operation, command)

    def test_the_controller_itself_only_reads_and_shuts_incus_down(self):
        for command in ("admin shutdown --timeout 60", "stop polyseat-persist-cpu --force --timeout 5",
                        "query /1.0/instances?recursion=1", "query /1.0/profiles?recursion=1"):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(7, command))
        for command in ("start polyseat-persist-cpu", PersistentStateTests.INIT, "delete polyseat-persist-cpu",
                        "image import /var/lib/polyseat-unraid-seat-image/image --alias polyseat-desktop",
                        "exec polyseat-persist-cpu -- /usr/local/bin/polyseat-selftest desktop",
                        "storage create default dir source=/mnt/cache/polyseat/incus/incus/storage-pools/default"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(7, command))

    def test_listener_address_is_the_little_endian_form_proc_shows(self):
        res = source_ctl('polyseatd_hex_ip 192.168.178.24; polyseatd_hex_ip 10.77.0.1')
        self.assertEqual(res.stdout.split(), ["18B2A8C0:BAB8", "01004D0A:BAB8"])

    def test_only_a_single_ipv4_on_the_bridge_is_accepted(self):
        script = r"""
          ip() { printf '%s' "$IPOUT"; }
          polyseatd_lan_ip
        """
        one = "5: br0    inet 192.168.178.24/24 brd 192.168.178.255 scope global br0\n"
        res = source_ctl(script, env={"PATH": "/usr/bin:/bin", "IPOUT": one})
        self.assertEqual(res.stdout.strip(), "192.168.178.24")
        for out in ("", one + one.replace(".24/", ".25/")):
            self.assertNotEqual(source_ctl(script, env={"PATH": "/usr/bin:/bin", "IPOUT": out}).returncode, 0)

    def fixture(self, pin=PIN, staged=True, creds=True, listeners="", signed=False):
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-psd"))
        self.addCleanup(shutil.rmtree, d)
        daemon = d / "var/lib/polyseat-unraid-daemon"
        if staged:
            (daemon / "bin").mkdir(parents=True)
            daemon.chmod(0o700)
            (daemon / "bin/polyseatd").write_bytes(b"pinned-daemon")
            (daemon / "bin/polyseatd").chmod(0o755)
            (daemon / "MANIFEST").write_text(hashlib.sha256(b"pinned-daemon").hexdigest() + "  bin/polyseatd\n")
            (daemon / ".bundle-sha256").write_text(hashlib.sha256(b"bundle").hexdigest() + "\n")
            if signed:
                (daemon / ".bundle-signed").write_text(hashlib.sha256(b"bundle").hexdigest() + "\n")
        state = d / "mnt/cache/polyseat/polyseatd"
        state.mkdir(parents=True)
        state.chmod(0o700)
        (d / "mnt/download/polyseat-games").mkdir(parents=True)
        (d / "mnt/download/polyseat-games").chmod(0o755)
        if creds:
            (state / "credentials.json").write_text(json.dumps(
                {"username": "owner", "algorithm": "argon2id", "salt": "c2FsdA==", "hash": "aGFzaA=="}))
            (state / "credentials.json").chmod(0o600)
        pin = hashlib.sha256(b"bundle").hexdigest() if pin == self.PIN else pin
        script = r"""
          POLYSEAT_RELEASE_GATE=canary ROOT="$1"
          init_paths 2>/dev/null; ROOT="$1"; POLYSEATD_DIR="$1/var/lib/polyseat-unraid-daemon" POLYSEATD_BIN="$POLYSEATD_DIR/bin/polyseatd"
          POLYSEATD_SHA256="$2" LISTENERS=$3 SCHEMA="$4"   # init_paths cannot find the schema from a sourced copy
          me=$(id -u)
          stat() { local out; out=$(command stat "$@") || return; printf '%s\n' "${out//$me/0}"; }
          polyseatd_lan_ip() { echo 192.168.178.24; }
          polyseatd_listeners() { printf '%s' "$LISTENERS"; }
          polyseatd_precheck
        """
        return source_ctl(script, str(d), pin, listeners, str(CTL.parent.parent / "schema.json"))

    def library(self, path="/mnt/download/polyseat-games", mounted=None, fstype="zfs", quota="500000000000", cfg=None, mode=0o755):
        """polyseatd_library_check as on a real host, with the mount table and zfs answering from the fixture."""
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-lib"))
        self.addCleanup(shutil.rmtree, d)
        (d / "boot/config/plugins/polyseat-unraid").mkdir(parents=True)
        if cfg is not None:
            (d / "boot/config/plugins/polyseat-unraid/polyseat-unraid.cfg").write_text(f'LIBRARY_PATH="{cfg}"\n')
        mount = d / path.lstrip("/")
        mount.mkdir(parents=True)
        mount.chmod(mode)
        script = r"""
          POLYSEAT_RELEASE_GATE=canary ROOT="$1"
          init_paths 2>/dev/null; ROOT="$1"; SCHEMA="$2"; CFG="$1/boot/config/plugins/polyseat-unraid/polyseat-unraid.cfg"
          me=$(id -u)
          stat() { local out; out=$(command stat "$@") || return; printf '%s\n' "${out//$me/0}"; }
          ci_real_system() { return 0; }
          findmnt() { [[ -n $DATASET ]] && printf '%s %s %s\n' "$DATASET" "$FSTYPE" "$ROOT$MOUNTED"; }
          zfs() { [[ $* == "get -Hp -o value quota $DATASET" ]] && printf '%s\n' "$QUOTA"; }
          polyseatd_library_check && echo "OK $POLYSEATD_LIBRARY_MOUNT $POLYSEATD_LIBRARY"
        """
        env = {"PATH": "/usr/bin:/bin", "DATASET": mounted if mounted is not None else "download/polyseat-games",
               "FSTYPE": fstype, "QUOTA": quota, "MOUNTED": path}
        return source_ctl(script, str(d), str(CTL.parent.parent / "schema.json"), env=env), d

    def test_the_library_is_the_setting_and_its_dataset_comes_from_the_mount_table(self):
        res, _ = self.library()                                                  # the default: today's dataset
        self.assertIn("OK /mnt/download/polyseat-games /mnt/download/polyseat-games/library", res.stdout, res.stderr)
        res, _ = self.library("/mnt/tank/games/steam", mounted="tank/games/steam", cfg="/mnt/tank/games/steam")
        self.assertIn("OK /mnt/tank/games/steam /mnt/tank/games/steam/library", res.stdout, res.stderr)
        res, _ = self.library("/mnt/tank/games/steam", mounted="tank/games/steam", cfg="")  # empty = the default path
        self.assertNotEqual(res.returncode, 0)

    def test_the_library_still_needs_a_dataset_of_its_own_a_quota_and_a_root_only_root(self):
        mine = "/mnt/tank/games"
        for kwargs, needle in (({"fstype": "fuse.shfs"}, "not the mount point of a ZFS dataset"),
                               ({"mounted": "tank"}, "not the mount point of a ZFS dataset"),    # a pool root has no quota of its own
                               ({"mounted": "tank/ga mes"}, "not the mount point of a ZFS dataset"),
                               ({"mounted": ""}, "not the mount point of a ZFS dataset"),
                               ({"quota": "0"}, "has no quota"), ({"quota": "-"}, "has no quota"),
                               ({"mode": 0o775}, "root:root and writable by root only"),
                               ({"cfg": "/etc"}, "not a path under /mnt"), ({"cfg": "/mnt/a/../b"}, "not a path under /mnt")):
            with self.subTest(**kwargs):
                args = {"path": mine, "mounted": "tank/games", "cfg": mine, **kwargs}
                res, _ = self.library(**args)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(needle, res.stderr)

    def test_precheck_needs_the_pin_the_staged_binary_a_password_and_a_free_port(self):
        self.assertEqual(self.fixture().returncode, 0, self.fixture().stderr)
        self.assertEqual(self.fixture(pin="", signed=True).returncode, 0)  # the fast lane: staged from a signed bundle
        for kwargs, needle in (({"pin": ""}, "neither the pinned nor a signed one"),
                               ({"pin": "cd" * 32}, "neither the pinned nor a signed one"),
                               ({"staged": False}, "no staged daemon"), ({"creds": False}, "no password set"),
                               ({"listeners": "18B2A8C0:BAB8"}, "already in use")):
            with self.subTest(**kwargs):
                res = self.fixture(**kwargs)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(needle, res.stderr)

    def stage(self, members, pin=True, namespace=None, sign_other=False):
        """members: (name, kind, data-or-target); returns (rc, stderr, staged dir). Without the pin, a signature under
        `namespace` (the fast lane) is all the controller has to go on."""
        import io, tarfile
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-stage"))
        self.addCleanup(shutil.rmtree, d)
        d.chmod(0o700)
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for name, kind, data in members:
                info = tarfile.TarInfo(name)
                if kind == "dir":
                    info.type, info.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(info)
                elif kind == "link":
                    info.type, info.linkname = tarfile.SYMTYPE, data
                    tar.addfile(info)
                else:
                    info.size, info.mode = len(data), 0o755
                    tar.addfile(info, io.BytesIO(data))
        (d / "bundle.tar").write_bytes(raw.getvalue())
        if namespace:
            key = d / "key"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            pub = (d / "key.pub").read_text().split()
            (d / "allowed_signers").write_text(
                f'polyseat-unraid-release namespaces="polyseat-unraid-bundle,{namespace}" {pub[0]} {pub[1]}\n')
            signed = d / ("other" if sign_other else "bundle.tar")
            if sign_other:
                signed.write_bytes(b"another bundle")
            subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", namespace, str(signed)], check=True)
            if sign_other:
                (d / "other.sig").rename(d / "bundle.tar.sig")
        script = r"""
          POLYSEAT_RELEASE_GATE=canary; init_paths 2>/dev/null; ROOT="$1"
          POLYSEATD_DIR="$1/var/lib/polyseat-unraid-daemon" POLYSEATD_BIN="$POLYSEATD_DIR/bin/polyseatd"
          POLYSEATD_SHA256=$(sha256sum < "$1/bundle.tar" | cut -c1-64); SIGNERS="$1/allowed_signers" REVOKED="$1/revoked"
          [[ $2 == pin ]] || POLYSEATD_SHA256=""
          require_approval() { :; }; require_no_journal() { :; }
          me=$(id -u); stat() { local out; out=$(command stat "$@") || return; printf '%s\n' "${out//$me /0 }"; }
          mkdir -p "$1/var/lib"; cmd_daemon_stage "$1/bundle.tar"
        """
        res = source_ctl(script, str(d), "pin" if pin else "")
        return res.returncode, res.stderr, d / "var/lib/polyseat-unraid-daemon"

    def test_daemon_stage_unpacks_only_a_clean_bundle_and_verifies_every_file(self):
        good = b"#!/bin/sh\n"
        manifest = hashlib.sha256(good).hexdigest().encode() + b"  bin/polyseatd\n"
        base = [("MANIFEST", "file", manifest), ("bin", "dir", None), ("bin/polyseatd", "file", good)]
        rc, err, staged = self.stage(base + [("bin/polyseatd-alias", "link", "polyseatd")])
        self.assertEqual(rc, 0, err)
        self.assertTrue((staged / "bin/polyseatd").is_file())
        for extra, needle in (([("bin/escape", "link", "/etc/shadow")], "leaves its directory"),
                              ([("bin/up", "link", "../MANIFEST")], "leaves its directory"),
                              ([("usr", "dir", None), ("usr/evil", "file", b"x")], "unexpected bundle entry"),
                              ([("bin/../../x", "file", b"x")], "unexpected bundle entry")):
            with self.subTest(extra=extra[-1][0]):
                rc, err, staged = self.stage(base + extra)
                self.assertNotEqual(rc, 0)
                self.assertIn(needle, err)
                self.assertFalse(staged.exists())
        rc, err, staged = self.stage([("MANIFEST", "file", b"0" * 64 + b"  bin/polyseatd\n"), ("bin", "dir", None),
                                      ("bin/polyseatd", "file", good)])
        self.assertNotEqual(rc, 0)
        self.assertIn("differs from its MANIFEST", err)

    def test_daemon_stage_takes_a_bundle_the_release_key_signed_for_daemons_only(self):
        good = b"#!/bin/sh\n"
        base = [("MANIFEST", "file", hashlib.sha256(good).hexdigest().encode() + b"  bin/polyseatd\n"),
                ("bin", "dir", None), ("bin/polyseatd", "file", good)]
        rc, err, staged = self.stage(base, pin=False, namespace="polyseat-unraid-daemon")
        self.assertEqual(rc, 0, err)
        self.assertEqual((staged / ".bundle-signed").read_text(), (staged / ".bundle-sha256").read_text())
        rc, err, staged = self.stage(base, pin=True)
        self.assertEqual(rc, 0, err)
        self.assertFalse((staged / ".bundle-signed").exists())
        for kwargs in ({}, {"namespace": "polyseat-unraid-seat-image"},
                       {"namespace": "polyseat-unraid-daemon", "sign_other": True}):
            with self.subTest(**kwargs):
                rc, err, staged = self.stage(base, pin=False, **kwargs)
                self.assertNotEqual(rc, 0)
                self.assertIn("neither the controller's pin nor signed", err)
                self.assertFalse(staged.exists())

    def test_daemon_stage_needs_the_pin_and_removal_is_exact(self):
        text = CTL.read_text()
        body = re.search(r"^cmd_daemon_stage\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(body.index("neither the controller's pin nor signed"), body.index('tar -xf "$src"'))
        self.assertIn("require_approval daemon-stage", body)
        self.assertIn('polyseatd_bundle_remove "$POLYSEATD_DIR"', text.split("cmd_uninstall()", 1)[1])

    def test_the_session_starts_only_on_the_lan_address_with_a_password_and_watches_incus(self):
        text = CTL.read_text()
        body = re.search(r"^cmd_polyseatd_session\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(body.index("polyseatd_precheck ||"), body.index("svc_start"))
        self.assertIn('polyseatd_config "$ip" "$cfg"', body)
        config = text[text.index("polyseatd_config() {"):text.index("play_seat_guard() {")]
        for flag in ('.listen == $listen', ".update_check == false", ".web_update == false", ".web_uninstall == false",
                     ".web_lan_bridge == false", 'test("@[A-Z]+@") | not'):
            self.assertIn(flag, config)
        self.assertIn('.listeners == [$l] and .session.setup == false', body)
        loop = body[body.index("while (( $(date -u +%s) < deadline ))"):body.index("done", body.index("while (( $(date"))]
        for needle in ("canary_profile_guard", "persist_instances", "canary_budget_check", "polyseatd_alive"):
            self.assertIn(needle, loop)
        self.assertLess(body.index('polyseatd_stop "$pid" || exit'), body.index("persist_clean_stop"))

    def test_run_state_cleanup_removes_plain_files_only(self):
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-run"))
        self.addCleanup(shutil.rmtree, d)
        script = r"""
          ROOT="$1"; me=$(id -u)
          stat() { local out; out=$(command stat "$@") || return; printf '%s\n' "${out//$me/0}"; }
          sandbox() { return 0; }; ci_real_system() { return 1; }
          polyseatd_run_state_remove
        """
        run = d / "run/polyseat"
        run.mkdir(parents=True)
        (run / "uhid-owner-1").write_text("x")
        (run / ".hidden").write_text("x")
        self.assertEqual(source_ctl(script, str(d)).returncode, 0)
        self.assertFalse(run.exists())
        run.mkdir()
        (run / "sub").mkdir()
        res = source_ctl(script, str(d))
        self.assertNotEqual(res.returncode, 0)
        self.assertTrue((run / "sub").is_dir())  # left for inspection, never recursed into
        shutil.rmtree(run)
        marks = run / "sealed/devices/virtual/misc/uhid"
        marks.mkdir(parents=True)
        (run / "sealed/devices/virtual/input").mkdir()
        (run / "uhid-owners.json").write_text("{}")
        self.assertEqual(source_ctl(script, str(d)).returncode, 0)  # the broker's marks (Tower run 28)
        self.assertFalse(run.exists())
        marks.mkdir(parents=True)
        (marks / "stray").write_text("x")
        res = source_ctl(script, str(d))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("more than the broker's marks", res.stderr)
        self.assertTrue((marks / "stray").is_file())

    def test_operator_commands_need_no_token_and_do_not_queue(self):
        for cmd in ("polyseatd-status", "polyseatd-stop"):
            res = source_ctl('POLYSEAT_RELEASE_GATE=canary ROOT=""; id() { printf "0\\n"; }; '
                             'canary_approval_for() { exit 99; }; require_mutation_allowed "$1"', cmd)
            self.assertEqual(res.returncode, 0, res.stderr)
        main = CTL.read_text().split("\nmain() {", 1)[1]
        early = main[:main.index("  acquire_lock\n")]
        self.assertIn("polyseatd-stop) cmd_polyseatd_stop; return ;;", early)
        res = source_ctl('POLYSEAT_RELEASE_GATE=canary ROOT=""; id() { printf "0\\n"; }; '
                         'canary_approval_for() { exit 99; }; require_mutation_allowed polyseatd-session')
        self.assertEqual(res.returncode, 99)

    def test_package_build_pins_the_daemon_only_when_asked(self):
        build = CTL.parents[5] / "unraid/scripts/build-plugin-package.sh"
        for pin, ok in (("", True), ("ab" * 32, True), ("AB" * 32, False), ("ab" * 31, False)):
            with self.subTest(pin=pin):
                out = Path(tempfile.mkdtemp(prefix="polyseat-pkg-"))
                self.addCleanup(shutil.rmtree, out)
                env = {"PATH": os.environ["PATH"]}
                if pin:
                    env["POLYSEATD_SHA256"] = pin
                res = subprocess.run(["sh", str(build), "2026.11.01", str(out), "closed"], env=env,
                                     capture_output=True, text=True)
                self.assertEqual(res.returncode == 0, ok, res.stderr)
                if ok:
                    ctl = subprocess.run(["tar", "-xJOf", str(next(out.glob("*.txz"))),
                                          "usr/local/emhttp/plugins/polyseat-unraid/scripts/polyseatctl"],
                                         capture_output=True, text=True, check=True).stdout
                    self.assertIn(f'POLYSEATD_SHA256="{pin}"\n', ctl)


class PlayTests(unittest.TestCase):
    """Steps 5+ (owner 2026-11-01): polyseatd builds and runs its own seats with input; the controller keeps the
    Incus state to upstream's seat shape, the console guarded and the host restored exactly."""
    SEAT = {"name": "seat1", "type": "container", "profiles": ["default"],
            "config": {"boot.autostart": "false", "security.nesting": "true", "limits.kernel.nice": "40",
                       "nvidia.runtime": "true", "nvidia.driver.capabilities": "all", "image.os": "Archlinux",
                       "volatile.eth0.hwaddr": "10:66:6a:00:00:01"},
            "devices": {"eth1": {"type": "nic", "nictype": "bridged", "parent": "br0", "name": "eth1"},
                        "gpu": {"type": "gpu", "mode": "0666"},
                        "uinput": {"type": "unix-char", "source": "/dev/uinput", "path": "/dev/uinput", "mode": "0666", "required": "false"},
                        "polyseat-event7": {"type": "unix-char", "source": "/dev/input/event7", "path": "/dev/input/event7",
                                            "mode": "0666", "required": "false"},
                        "library": {"type": "disk", "source": "/mnt/download/polyseat-games/library/seats/seat1", "path": "/srv/library"},
                        "nvidia-profiles": {"type": "disk", "source": "/usr/share/nvidia", "path": "/usr/share/nvidia",
                                            "readonly": "true", "required": "false"}}}
    PROFILE = {"name": "default", "config": {}, "devices": {
        "root": {"type": "disk", "path": "/", "pool": "default"},
        "eth0": {"type": "nic", "nictype": "bridged", "parent": "br0", "name": "eth0"}}}

    def allowed(self, command):
        return PersistentStateTests.allowed(self, 8, command)

    def test_the_controller_only_prepares_the_profile_and_stops_seats(self):
        for command in ("profile device add default eth0 nic nictype=bridged parent=br0 name=eth0",
                        "storage create default dir source=/mnt/cache/polyseat/incus/incus/storage-pools/default",
                        "stop seat1 --timeout 90", "stop my-seat-2 --force --timeout 5", "admin shutdown --timeout 60",
                        "exec seat2 -- sudo -u player env XDG_RUNTIME_DIR=/run/user/1000 "
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user stop polyseat-sway.service",
                        "query /1.0/profiles/default", "query /1.0/instances?recursion=1", "config set seat1 boot.autostart=false"):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(command))
        for command in ("config set seat1 boot.autostart=true", "config set seat1 raw.lxc=x", "config set seat1 security.privileged=true"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))
        for command in ("delete seat1", "start seat1", "stop Seat1 --timeout 90", "stop seat1;id --timeout 90",
                        "stop seat1 --timeout 60", "exec seat1 -- sudo -u player systemctl --user stop polyseat-sway.service",
                        "exec seat1 -- sudo -u root env XDG_RUNTIME_DIR=/run/user/1000 "
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user stop polyseat-sway.service",
                        "profile device add default eth0 nic nictype=macvlan parent=br0 name=eth0",
                        "exec seat1 -- /bin/sh", "config set seat1 raw.lxc=x", "network create polyseatbr0"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(command))

    def test_seat_compositors_run_above_the_game_and_nothing_else_is_touched(self):
        """Run 30: the play daemon no longer runs at nice 19, and a seat's sway, gamescope, Xwayland and wineserver are
        lifted to -12 thread by thread (upstream relies on ananicy for that); host processes and the game are not."""
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-prio"))
        self.addCleanup(shutil.rmtree, d)
        def proc(pid, comm, cgroup, threads):
            base = d / str(pid)
            base.mkdir()
            (base / "comm").write_text(comm + "\n")
            (base / "cgroup").write_text(cgroup + "\n")
            for tid, nice in threads:
                (base / "task" / str(tid)).mkdir(parents=True)
                fields = ["S"] + ["0"] * 15 + [str(nice)] + ["0"] * 20
                (base / "task" / str(tid) / "stat").write_text(f"{tid} ({comm}) " + " ".join(fields) + "\n")
        proc(100, "sway", "0::/polyseat-unraid-canary/lxc.payload.seat1", [(100, 19), (101, 8), (102, -12)])
        proc(200, "gamescope-wl", "0::/polyseat-unraid-canary/lxc.payload.seat2/init.scope", [(200, 19)])
        proc(300, "sway", "0::/user.slice", [(300, 0)])
        proc(400, "nx:MainThread", "0::/polyseat-unraid-canary/lxc.payload.seat1", [(400, -8)])
        proc(500, "wineserver", "0::/polyseat-unraid-canary/lxc.payload.seat1", [(500, -12)])
        script = r"""
          renice() { printf '%s\n' "$*" >> "$1/calls"; }
          PLAY_PROC="$1" play_seat_priorities
        """
        res = source_ctl(script.replace("$1/calls", str(d / "calls")), str(d))
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = sorted((d / "calls").read_text().split("\n")[:-1])
        self.assertEqual(calls, ["-n -12 -p 100", "-n -12 -p 101", "-n -12 -p 200"])
        launch = re.search(r"^canary_launch\(\) \{.*?^\}", CTL.read_text(), re.MULTILINE | re.DOTALL).group(0)
        self.assertIn('if [[ $CANARY_OPERATION -ne 8 ]]; then renice -n 19 -p "$BASHPID"', launch)

    def test_only_a_play_token_may_cover_six_hours(self):
        app = DesktopApprovalTests("test_stage_and_smoke_are_bound_to_bundle_and_image")
        app.setUp()
        self.addCleanup(app.tmp.cleanup)
        app.token["expires_at"] = app.token["issued_at"] + 21600
        for actions, want in ((["polyseatd-play", "seat-image-stage"], 0), (["polyseatd-session", "seat-image-stage"], 2),
                              (["steam-session"], 2)):
            with self.subTest(actions=actions):
                app.token["actions"] = actions
                app.sign()
                app.stage_copy(app.image.read_bytes())
                self.assertEqual(app.check(actions[0]), want)

    def guard(self, instances, profile=None):
        script = r"""
          POLYSEAT_RELEASE_GATE=canary; init_paths 2>/dev/null; ROOT=""
          INCUS_DIR=/mnt/cache/polyseat/incus/incus GPU_CANARY_PCI=0000:01:00.0
          run_incus() { case "$2" in
            /1.0) echo '{"config": {}}' ;; '/1.0/projects?recursion=1') echo '[{"name": "default"}]' ;;
            '/1.0/profiles?recursion=1') printf '[%s]' "$PROFILE" ;; '/1.0/instances?recursion=1') printf '%s' "$INSTANCES" ;;
            '/1.0/storage-pools?recursion=1') echo '[{"name": "default", "driver": "dir", "config": {"source": "/mnt/cache/polyseat/incus/incus/storage-pools/default"}}]' ;;
            '/1.0/networks?recursion=1') echo '[{"name": "br0", "managed": false}]' ;; esac; }
          play_seat_guard
        """
        env = {"PATH": "/usr/bin:/bin", "INSTANCES": json.dumps(instances), "PROFILE": json.dumps(profile or self.PROFILE)}
        return source_ctl(script, env=env)

    def test_seat_policy_allows_upstreams_seat_and_nothing_else(self):
        persisted = {"name": "polyseat-persist-cpu", "type": "container", "profiles": ["default"],
                     "config": {"boot.autostart": "false", "security.privileged": "false", "security.nesting": "false",
                                "limits.cpu": "2", "limits.memory": "2GiB", "limits.processes": "1024"},
                     "devices": {"root": {"type": "disk", "path": "/", "pool": "default"}}}
        res = self.guard([self.SEAT, persisted])  # step 2's persisted seat stays next to polyseatd's
        self.assertEqual(res.returncode, 0, res.stderr)
        res = source_ctl('POLYSEAT_RELEASE_GATE=canary CANARY_OPERATION=8; play_seat_guard() { echo PLAY-GUARD; }; canary_profile_guard')
        self.assertIn("PLAY-GUARD", res.stdout)
        bad = []
        for key, value in (("raw.lxc", "lxc.cap.drop ="), ("security.privileged", "true"), ("boot.autostart", "true"),
                           ("linux.kernel_modules", "kvm")):
            seat = json.loads(json.dumps(self.SEAT)); seat["config"][key] = value; bad.append((key, seat))
        for name, device in (("evil", {"type": "disk", "source": "/mnt/user/appdata", "path": "/x"}),
                             ("escape", {"type": "disk", "source": "/mnt/cache/polyseat/library/../incus", "path": "/x"}),
                             ("dev", {"type": "unix-char", "source": "/dev/sda", "path": "/dev/sda", "mode": "0666", "required": "false"}),
                             ("othergpu", {"type": "gpu", "mode": "0666", "pci": "0000:00:02.0"}),
                             ("macvlan", {"type": "nic", "nictype": "macvlan", "parent": "br0", "name": "eth1"})):
            seat = json.loads(json.dumps(self.SEAT)); seat["devices"][name] = device; bad.append((name, seat))
        seat = json.loads(json.dumps(self.SEAT)); seat["profiles"] = ["default", "extra"]; bad.append(("profiles", seat))
        for label, seat in bad:
            with self.subTest(label=label):
                res = self.guard([seat])
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("outside the seat policy", res.stderr)
        profile = json.loads(json.dumps(self.PROFILE)); profile["config"] = {"raw.lxc": "x"}
        self.assertNotEqual(self.guard([self.SEAT], profile).returncode, 0)

    def test_host_changes_are_recorded_and_put_back_exactly(self):
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-play"))
        self.addCleanup(shutil.rmtree, d)
        (d / "proc/sys/kernel").mkdir(parents=True)
        (d / "proc/sys/kernel/sysrq").write_text("1\n")
        (d / "etc/udev/rules.d").mkdir(parents=True)
        (d / "usr/local/lib").mkdir(parents=True)
        bundle = d / "bundle"
        (bundle / "bin").mkdir(parents=True); (bundle / "rules").mkdir(); (bundle / "lib/polyseat").mkdir(parents=True)
        (bundle / "bin/polyseat-console").write_text('#!/bin/sh\necho "$@" >> "$CONSOLE_LOG"; '
                                                     '[ "$1" = off ] && echo "{}" > "$3"; [ "$1" = restore ] && rm -f "$2"; exit 0\n')
        (bundle / "bin/polyseat-console").chmod(0o755)
        (bundle / "rules/72-polyseat-hide.rules").write_text("# rule\n")
        (bundle / "lib/polyseat/device_owner.udev").write_text("#!@BUNDLE@/py/bin/interp -I\nprint(1)\n")
        run = d / "run"; run.mkdir()
        script = r"""
          POLYSEAT_RELEASE_GATE=canary ROOT="$1" RUN_DIR="$1/run" POLYSEATD_DIR="$1/bundle"
          sandbox() { return 0; }; ci_real_system() { return 1; }
          play_host_on || exit 7
          printf 'ON sysrq=%s rule=%s helper=%s\n' "$(cat "$1/proc/sys/kernel/sysrq")" "$([ -f "$1/etc/udev/rules.d/72-polyseat-hide.rules" ] && echo y)" \
            "$(head -n 1 "$1/usr/local/lib/polyseat/device_owner.py")"
          play_host_on && exit 8
          play_host_off || exit 9
          printf 'OFF sysrq=%s rule=%s helper=%s state=%s\n' "$(cat "$1/proc/sys/kernel/sysrq")" \
            "$([ -e "$1/etc/udev/rules.d/72-polyseat-hide.rules" ] && echo y || echo n)" \
            "$([ -e "$1/usr/local/lib/polyseat" ] && echo y || echo n)" "$([ -e "$1/run/play-host.json" ] && echo y || echo n)"
        """
        res = source_ctl(script, str(d), env={"PATH": "/usr/bin:/bin", "CONSOLE_LOG": str(d / "console.log")})
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"ON sysrq=0 rule=y helper=#!{bundle}/py/bin/interp -I", res.stdout)
        self.assertIn("OFF sysrq=1 rule=n helper=n state=n", res.stdout)
        self.assertEqual((d / "console.log").read_text().split()[0:2], ["off", f"{d}/dev/tty1"])
        self.assertIn("restore", (d / "console.log").read_text())

    def play_budget(self, cfg=None, cpuset=None):
        """The play budget as the controller selects it, with the real schema and an optional settings file."""
        d = Path(tempfile.mkdtemp(prefix="polyseat-ctl-budget"))
        self.addCleanup(shutil.rmtree, d)
        if cfg is not None:
            (d / "cfg").write_text("".join(f'{k}="{v}"\n' for k, v in cfg.items()))
        res = source_ctl(r"""
          POLYSEAT_RELEASE_GATE=canary; CFG="$1/cfg" SCHEMA="$2"; [[ -z $3 ]] || CANARY_HOST_CPUSET=$3
          canary_budget_select play || { echo REFUSED; exit 0; }
          printf '%s|%s|%s|%s|%s\n' "$CANARY_CPUSET" "$CANARY_CPU_MAX" "$CANARY_MEMORY_MAX" "$CANARY_PIDS_MAX" "$CANARY_GPU_PCI"
        """, str(d), str(CTL.parent.parent / "schema.json"), cpuset or "")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.strip()

    def test_play_is_unlimited_but_keeps_the_memory_floor(self):
        self.assertEqual(self.play_budget(), "0-27|max 100000|max|max|0000:01:00.0")
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "0", "PLAY_MEMORY_MB": "0"}),
                         "0-27|max 100000|max|max|0000:01:00.0")  # the explicit defaults are today's values
        script = r"""
          CANARY_OPERATION=$1 CANARY_HOST_MIN_AVAILABLE_KB=$2 CANARY_HOST_MAX_LOAD=0
          canary_host_pressure_check
        """
        self.assertEqual(source_ctl(script, "8", "1").returncode, 0)          # any load
        self.assertNotEqual(source_ctl(script, "8", str(2 ** 50)).returncode, 0)  # but never below the memory floor
        self.assertNotEqual(source_ctl(script, "4", "1").returncode, 0)       # other operations keep the load limit

    def test_play_limits_become_exact_cgroup_values_and_bad_ones_refuse(self):
        # percent of the whole host (28 CPUs here): quota = percent * CPUs * 1000 per 100000 us period
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "50"}), "0-27|1400000 100000|max|max|0000:01:00.0")
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "100"}), "0-27|2800000 100000|max|max|0000:01:00.0")
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "007"}), "0-27|196000 100000|max|max|0000:01:00.0")
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "25"}, cpuset="0-3,8-11"), "0-3,8-11|200000 100000|max|max|0000:01:00.0")
        self.assertEqual(self.play_budget({"PLAY_MEMORY_MB": "8192"}), "0-27|max 100000|8589934592|max|0000:01:00.0")
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "10", "PLAY_MEMORY_MB": "1"}),
                         "0-27|280000 100000|1048576|max|0000:01:00.0")
        for cfg in ({"PLAY_CPU_PERCENT": "101"}, {"PLAY_CPU_PERCENT": "-5"}, {"PLAY_CPU_PERCENT": "5%"},
                    {"PLAY_MEMORY_MB": "4194305"}, {"PLAY_MEMORY_MB": "8G"}, {"PLAY_MEMORY_MB": "$(id)"}):
            with self.subTest(cfg=cfg):
                self.assertEqual(self.play_budget(cfg), "REFUSED")
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "50"}, cpuset="garbage"), "REFUSED")
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "50"}, cpuset="5-2"), "REFUSED")
        # 0 stays unlimited whatever the cpuset reads like; the other budgets never see the play settings
        self.assertEqual(self.play_budget({"PLAY_CPU_PERCENT": "0"}, cpuset="garbage"), "garbage|max 100000|max|max|0000:01:00.0")

    def test_the_same_values_that_created_the_cgroup_are_the_ones_it_is_checked_against(self):
        text = CTL.read_text()
        rest = text.replace(text[text.index("play_limits() {"):text.index("# Exactly two fixed budgets")], "")
        self.assertEqual(re.findall(r"^\s*(PLAY_(?:CPU|MEMORY)_MAX)=", rest, re.M), ["PLAY_CPU_MAX", "PLAY_MEMORY_MAX"])
        self.assertIn("CANARY_CPU_MAX=$PLAY_CPU_MAX CANARY_MEMORY_MAX=$PLAY_MEMORY_MAX", text)
        play = re.search(r"^cmd_polyseatd_play\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
        self.assertLess(play.index("play_limits ||"), play.index("mkdir -- \"$dir\""))  # a bad value starts nothing
        self.assertIn("canary_budget_select play || exit", play)

    def test_a_clean_stop_record_may_name_upstreams_seats(self):
        d = PersistentStateTests.workspace(self)
        self.assertEqual(PersistentStateTests.prepare(self, d).returncode, 0)
        PersistentStateTests.clean_stop(self, d, [{"name": "seat1", "status": "Stopped", "autostart": "false"},
                                                   {"name": "polyseat-persist-cpu", "status": "Stopped", "autostart": "false"}])
        self.assertEqual(PersistentStateTests.prepare(self, d).returncode, 0)
        PersistentStateTests.clean_stop(self, d, [{"name": "Seat 1", "status": "Stopped", "autostart": "false"}])
        self.assertNotEqual(PersistentStateTests.prepare(self, d).returncode, 0)
        # A stopped seat whose build ended early has no boot.autostart; Incus starts it only if it ran at shutdown.
        PersistentStateTests.clean_stop(self, d, [{"name": "seat1", "status": "Stopped", "autostart": ""}])
        self.assertEqual(PersistentStateTests.prepare(self, d).returncode, 0)
        PersistentStateTests.clean_stop(self, d, [{"name": "seat1", "status": "Stopped", "autostart": "true"}])
        self.assertNotEqual(PersistentStateTests.prepare(self, d).returncode, 0)

    def test_the_play_session_order(self):
        text = CTL.read_text()
        body = re.search(r"^cmd_polyseatd_play\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
        order = ["polyseatd_precheck ||", "gpu_preflight_run", "svc_start ||", "play_seat_guard ||", "play_host_on ||",
                 "polyseatd_launch", "polyseatd_stop \"$pid\" || exit", "play_stop_seats", "persist_clean_stop",
                 "svc_stop full ||", "play_host_off ||"]
        positions = [body.index(o) for o in order[:7]] + [body.rindex(o) for o in order[7:]]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("play_host_off || rc=4", body)  # the failure path restores the host too


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
                                   ("steam_seat_run", "STEAM_INSTANCE")):
            body = re.search(rf"^{function}\(\) \([^\n]*\n(.*?)^\)", text, re.MULTILINE | re.DOTALL).group(1)
            self.assertIn(f'canary_failure_cleanup "${instance}"; svc_stop full', body, function)

    def test_cleanup_uses_only_commands_the_operation_allows(self):
        for operation, instance in ((2, "polyseat-desktop-cpu"), (3, "polyseat-desktop-gpu"), (4, "polyseat-steam-gpu"),
                                    (5, "polyseat-steam-gpu")):
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
