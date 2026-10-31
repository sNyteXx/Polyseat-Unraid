"""`polyseatctl gpu-preflight`: read-only GPU ownership decision against fixture trees.

The fixture mirrors what was measured read-only on the owner's Tower on 2026-10-31
(RTX 5070 Ti at 0000:01:00.0 on the host nvidia driver, its HDMI audio function in the
same IOMMU group, a GamingVM that passes both through with managed='yes', shut off).
Nothing here touches a real GPU, libvirt or /proc of the test host.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from unraid.tests.ctl_support import Sandbox

GPU = "0000:01:00.0"
AUDIO = "0000:01:00.1"

GAMING_VM_XML = """<domain type='kvm'>
  <name>GamingVM</name>
  <memory unit='KiB'>16777216</memory>
  <devices>
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <driver name='vfio'/>
      <source>
        <address domain='0x0000' bus='0x01' slot='0x00' function='0x0'/>
      </source>
      <rom file='/mnt/user/domains/RTX5070ti_fixed.rom'/>
      <address type='pci' domain='0x0000' bus='0x05' slot='0x00' function='0x0'/>
    </hostdev>
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source>
        <address domain='0x0000' bus='0x01' slot='0x00' function='0x1'/>
      </source>
      <address type='pci' domain='0x0000' bus='0x06' slot='0x00' function='0x0'/>
    </hostdev>
    <hostdev mode='subsystem' type='usb' managed='no'>
      <source><vendor id='0x046d'/><product id='0xc52b'/></source>
    </hostdev>
  </devices>
</domain>
"""

# A VM whose guest-side address happens to equal the GPU's host address must not count.
DECOY_VM_XML = """<domain type='kvm'>
  <name>Windows 11</name>
  <memory unit='KiB'>8388608</memory>
  <devices>
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source>
        <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
      </source>
      <address type='pci' domain='0x0000' bus='0x01' slot='0x00' function='0x0'/>
    </hostdev>
  </devices>
</domain>
"""


class TowerFixture:
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.r = self.sb.root
        self.tower_like()

    # ---- fixture -----------------------------------------------------------
    def w(self, rel: str, text: str = "", mode: int = 0o644):
        p = self.r / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        p.chmod(mode)
        return p

    def link(self, rel: str, target: str):
        p = self.r / rel.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink() or p.exists():
            p.unlink()
        p.symlink_to(target)

    def pci(self, addr, cls, vendor, device, driver, group):
        base = f"sys/bus/pci/devices/{addr}"
        self.w(f"{base}/class", cls + "\n")
        self.w(f"{base}/vendor", vendor + "\n")
        self.w(f"{base}/device", device + "\n")
        self.w(f"{base}/driver_override", "(null)\n")
        if driver:
            (self.r / f"sys/bus/pci/drivers/{driver}").mkdir(parents=True, exist_ok=True)
            self.link(f"{base}/driver", str(self.r / f"sys/bus/pci/drivers/{driver}"))
        grp = self.r / f"sys/kernel/iommu_groups/{group}/devices"
        grp.mkdir(parents=True, exist_ok=True)
        self.link(f"{base}/iommu_group", str(self.r / f"sys/kernel/iommu_groups/{group}"))
        self.link(f"sys/kernel/iommu_groups/{group}/devices/{addr}", str(self.r / base))

    def tower_like(self):
        self.pci(GPU, "0x030000", "0x10de", "0x2c05", "nvidia", 14)
        self.pci(AUDIO, "0x040300", "0x10de", "0x22e9", None, 14)
        (self.r / f"sys/bus/pci/devices/{GPU}/drm/card0").mkdir(parents=True)
        (self.r / f"sys/bus/pci/devices/{GPU}/drm/renderD128").mkdir(parents=True)
        self.w("sys/module/nvidia_drm/parameters/modeset", "Y\n")
        self.w(f"proc/driver/nvidia/gpus/{GPU}/information",
               "Model: \t\t NVIDIA GeForce RTX 5070 Ti\nBus Location: \t 0000:01:00.0\n"
               "Device Minor: \t 0\nGPU Excluded:\t No\n")
        for node in ("dev/nvidiactl", "dev/nvidia0", "dev/nvidia-uvm", "dev/dri/card0", "dev/dri/renderD128"):
            self.w(node)
        self.w("etc/libvirt/qemu/GamingVM.xml", GAMING_VM_XML, 0o600)
        self.w("etc/libvirt/qemu/Windows 11.xml", DECOY_VM_XML, 0o600)
        (self.r / "etc/libvirt/qemu/autostart").mkdir(parents=True, exist_ok=True)
        (self.r / "var/run/libvirt/qemu").mkdir(parents=True, exist_ok=True)
        self.w("var/run/gamingvm-transition.lock")
        self.w("proc/locks", "1: POSIX  ADVISORY  WRITE 500891 00:35:1106369 1073741826 1073742335\n")
        self.w("proc/meminfo", "MemTotal:       82116948 kB\nMemAvailable:   19866060 kB\n")
        self.w("usr/bin/nvidia-container-cli", "#!/bin/sh\nexit 0\n", 0o755)



class GpuPreflightTests(TowerFixture, unittest.TestCase):
    def run_pf(self, *extra):
        res = self.sb.ctl("gpu-preflight", "--pci", GPU, "--json", *extra)
        data = json.loads(res.out) if res.out.strip().startswith("{") else None
        return res, data

    def failed(self, data):
        return {c["id"] for c in data["checks"] if c["status"] == "FAIL"}

    # ---- tests -------------------------------------------------------------
    def test_measured_tower_state_allows_device_access(self):
        res, data = self.run_pf()
        self.assertEqual(res.code, 0, res.text)
        self.assertTrue(data["gpu_seat_allowed"])
        self.assertTrue(data["read_only"])
        vm = [c for c in data["checks"] if c["id"] == "vm"]
        self.assertEqual(len(vm), 1, vm)  # the decoy's guest-side address does not count
        self.assertIn("GamingVM", vm[0]["detail"])
        self.assertIn("16384 MiB", vm[0]["detail"])

    def test_running_gaming_vm_blocks(self):
        self.w("var/run/libvirt/qemu/GamingVM.pid", "4711\n")
        (self.r / "proc/4711").mkdir(parents=True)
        res, data = self.run_pf()
        self.assertEqual(res.code, 2, res.text)
        self.assertIn("vm", self.failed(data))

    def test_vm_being_started_blocks_before_qemu_has_a_pid(self):
        self.w("var/run/libvirt/qemu/GamingVM.xml", "<domstatus/>")
        _, data = self.run_pf()
        self.assertIn("vm", self.failed(data))

    def test_stale_pid_file_without_process_is_not_running(self):
        self.w("var/run/libvirt/qemu/GamingVM.pid", "999999\n")
        res, _ = self.run_pf()
        self.assertEqual(res.code, 0, res.text)

    def test_vfio_bound_gpu_is_refused_and_never_touched(self):
        self.link(f"sys/bus/pci/devices/{GPU}/driver", str(self.r / "sys/bus/pci/drivers/vfio-pci"))
        (self.r / "sys/bus/pci/drivers/vfio-pci").mkdir(parents=True, exist_ok=True)
        before = os.readlink(self.r / f"sys/bus/pci/devices/{GPU}/driver")
        _, data = self.run_pf()
        self.assertIn("driver", self.failed(data))
        self.assertEqual(before, os.readlink(self.r / f"sys/bus/pci/devices/{GPU}/driver"))

    def test_unbound_gpu_is_refused(self):
        (self.r / f"sys/bus/pci/devices/{GPU}/driver").unlink()
        _, data = self.run_pf()
        self.assertIn("driver", self.failed(data))

    def test_group_member_on_vfio_means_handover_in_progress(self):
        self.link(f"sys/bus/pci/devices/{AUDIO}/driver", str(self.r / "sys/bus/pci/drivers/vfio-pci"))
        (self.r / "sys/bus/pci/drivers/vfio-pci").mkdir(parents=True, exist_ok=True)
        _, data = self.run_pf()
        self.assertIn("iommu", self.failed(data))

    def test_held_transition_lock_blocks(self):
        st = os.stat(self.r / "var/run/gamingvm-transition.lock")
        dev = st.st_dev
        key = "%02x:%02x:%d" % ((dev >> 8) & 0xfff, (dev & 0xff) | ((dev >> 12) & 0xfff00), st.st_ino)
        self.w("proc/locks", f"1: FLOCK  ADVISORY  WRITE 12345 {key} 0 EOF\n")
        _, data = self.run_pf()
        self.assertIn("transition", self.failed(data))

    def test_existing_but_unlocked_lock_file_passes(self):
        res, data = self.run_pf()
        self.assertEqual(res.code, 0, res.text)
        self.assertTrue(any(c["id"] == "transition" and "not locked" in c["detail"] for c in data["checks"]))

    def test_unhealthy_marker_blocks(self):
        self.w("var/run/gamingvm-transition.unhealthy")
        _, data = self.run_pf()
        self.assertIn("transition", self.failed(data))

    def test_autostart_of_owning_vm_blocks(self):
        self.link("etc/libvirt/qemu/autostart/GamingVM.xml", "/etc/libvirt/qemu/GamingVM.xml")
        _, data = self.run_pf()
        self.assertIn("autostart", self.failed(data))

    def test_missing_device_nodes_are_reported_not_created(self):
        (self.r / "dev/nvidia0").unlink()
        _, data = self.run_pf()
        self.assertIn("node", self.failed(data))
        self.assertFalse((self.r / "dev/nvidia0").exists())

    def test_missing_toolkit_blocks(self):
        (self.r / "usr/bin/nvidia-container-cli").unlink()
        _, data = self.run_pf()
        self.assertIn("toolkit", self.failed(data))

    def test_low_memory_blocks(self):
        self.w("proc/meminfo", "MemAvailable:   1048576 kB\n")
        _, data = self.run_pf()
        self.assertIn("memory", self.failed(data))

    def test_existing_holders_are_reported_but_sharing_is_allowed(self):
        fd = self.r / "proc/2222/fd"
        fd.mkdir(parents=True)
        (fd / "5").symlink_to("/dev/nvidia0")
        (fd / "6").symlink_to("/dev/nvidiactl")
        (fd / "7").symlink_to("/etc/passwd")
        self.w("proc/2222/comm", "jellyfin\n")
        self.w("proc/2222/cgroup", "0::/docker/abc123\n")
        res, data = self.run_pf()
        self.assertEqual(res.code, 0, res.text)
        holders = [c["detail"] for c in data["checks"] if c["id"] == "holder" and c["status"] == "INFO"]
        self.assertEqual(holders, ["2222 jellyfin /docker/abc123 nvidia0,nvidiactl"])

    def test_non_nvidia_card_is_refused(self):
        self.w(f"sys/bus/pci/devices/{GPU}/vendor", "0x8086\n")
        _, data = self.run_pf()
        self.assertIn("pci", self.failed(data))

    def test_text_output_and_read_only(self):
        before = sorted(str(p.relative_to(self.r)) for p in self.r.rglob("*"))
        res = self.sb.ctl("gpu-preflight", "--pci", GPU)
        self.assertEqual(res.code, 0, res.text)
        self.assertIn("VERDICT: a seat MAY use 0000:01:00.0", res.out)
        after = sorted(str(p.relative_to(self.r)) for p in self.r.rglob("*"))
        self.assertEqual(before, after)

    def test_bad_address_is_a_usage_error(self):
        res = self.sb.ctl("gpu-preflight", "--pci", "01:00.0;reboot")
        self.assertEqual(res.code, 1, res.text)


if __name__ == "__main__":
    unittest.main()


class GpuGuardQuickTests(TowerFixture, unittest.TestCase):
    """The 1-second VM-priority check the canary supervisor runs during gpu-smoke."""

    def quick(self):
        script = r'''
          source <(sed '$d' "$1")
          POLYSEAT_ROOT="$2"
          init_paths
          gpu_guard_quick 0000:01:00.0 && echo HOLD || echo RELEASE
        '''
        import subprocess
        from unraid.tests.ctl_support import CTL
        res = subprocess.run(["bash", "-c", script, "quick", str(CTL), str(self.r)],
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False)
        return res.stdout.strip()

    def test_quiet_tower_state_keeps_the_gpu(self):
        self.assertEqual(self.quick(), "HOLD")

    def test_vm_start_hand_over_or_driver_change_releases_immediately(self):
        cases = {
            "vm-status-file": lambda: self.w("var/run/libvirt/qemu/GamingVM.xml", "<domstatus/>"),
            "vm-running": lambda: (self.w("var/run/libvirt/qemu/GamingVM.pid", "77\n"),
                                   (self.r / "proc/77").mkdir(parents=True, exist_ok=True)),
            "marker": lambda: self.w("var/run/gamingvm-transition.unhealthy"),
            "audio-on-vfio": lambda: ((self.r / "sys/bus/pci/drivers/vfio-pci").mkdir(parents=True, exist_ok=True),
                                      self.link(f"sys/bus/pci/devices/{AUDIO}/driver",
                                                str(self.r / "sys/bus/pci/drivers/vfio-pci"))),
            "unbound": lambda: (self.r / f"sys/bus/pci/devices/{GPU}/driver").unlink(),
            "decoy-vm-running": None,
        }
        for name, change in cases.items():
            with self.subTest(change=name):
                self.tearDown_fixture()
                if change is None:  # a VM that does not pass this GPU through may run freely
                    self.w("var/run/libvirt/qemu/Windows 11.pid", "77\n")
                    (self.r / "proc/77").mkdir(parents=True, exist_ok=True)
                    self.assertEqual(self.quick(), "HOLD")
                    continue
                change()
                self.assertEqual(self.quick(), "RELEASE")

    def tearDown_fixture(self):
        import shutil
        for rel in ("var/run/libvirt", "var/run/gamingvm-transition.unhealthy", "proc/77", "sys"):
            p = self.r / rel
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        self.tower_like()

