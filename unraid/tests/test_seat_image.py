"""Offline tests for the Polyseat desktop seat image sources: no Docker, no Incus, no network, no root."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEAT = ROOT / "unraid" / "seat-image"
OVERLAY = SEAT / "rootfs"
PINS = ROOT / "unraid" / "build-pins.env"
WORKFLOW = ROOT / ".github" / "workflows" / "seat-image.yml"
FEASIBILITY = ROOT / ".github" / "workflows" / "incus-feasibility.yml"
SMOKE = ROOT / "unraid" / "scripts" / "ci-desktop-smoke.sh"
PROFILE = OVERLAY / "usr/local/bin/polyseat-profile"
SELFTEST = OVERLAY / "usr/local/bin/polyseat-selftest"
UPSTREAM = ROOT / "internal" / "seat" / "provision.go"

SPEC = importlib.util.spec_from_file_location("pack_image", SEAT / "pack-image.py")
PACK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACK)


def pins() -> dict[str, str]:
    return dict(line.split("=", 1) for line in PINS.read_text().splitlines() if line and not line.startswith("#"))


class Pins(unittest.TestCase):
    def test_seat_pins_are_present_and_well_formed(self):
        p = pins()
        self.assertRegex(p["SEAT_BASE_IMAGE"], r"^archlinux@sha256:[0-9a-f]{64}$")
        self.assertRegex(p["SEAT_ARCHIVE_DATE"], r"^20\d\d/(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])$")
        self.assertRegex(p["SUNSHINE_PKG_SHA256"], r"^[0-9a-f]{64}$")
        self.assertRegex(p["SUNSHINE_PKG_VERSION"], r"^\d+\.\d+\.\d+-\d+\.\d+$")
        self.assertEqual(p["SUNSHINE_PKG_URL"].count(p["SUNSHINE_PKG_VERSION"]), 2)
        self.assertTrue(p["SUNSHINE_PKG_URL"].startswith("https://github.com/"))

    def test_sunshine_pin_equals_the_upstream_provisioner(self):
        source = UPSTREAM.read_text()
        p = pins()
        pin = re.search(r'const SunshinePin = "([^"]+)"', source).group(1)
        build = re.search(r'const SunshineBuild = "([^"]+)"', source).group(1)
        sha = re.search(r'const sunshinePackageSHA256 = "([0-9a-f]{64})"', source).group(1)
        self.assertEqual(p["SUNSHINE_PKG_VERSION"], f"{pin}-{build}")
        self.assertEqual(p["SUNSHINE_PKG_SHA256"], sha)

    def test_every_pin_line_loads_through_the_dev_build_guard(self):
        guard = ROOT / "unraid/scripts/lib/dev-guards.sh"
        result = subprocess.run(["sh", "-c", f". {guard}; dev_load_pins {PINS} && echo $SEAT_ARCHIVE_DATE"],
                                capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), pins()["SEAT_ARCHIVE_DATE"])

    def test_dockerfile_takes_every_input_from_build_args_not_literals(self):
        text = (SEAT / "Dockerfile").read_text()
        self.assertIn("FROM ${SEAT_BASE_IMAGE}", text)
        self.assertNotRegex(text, r"sha256:[0-9a-f]{64}")
        provision = (SEAT / "provision.sh").read_text()
        self.assertIn("sha256sum --check --strict", provision)
        self.assertNotIn("SigLevel = Never", provision)
        self.assertNotIn("--noconfirm --overwrite", provision)
        self.assertIn("archive.archlinux.org/repos/%s/$repo/os/$arch", provision)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_actions_are_pinned_to_the_same_shas_as_the_feasibility_workflow(self):
        feasibility = FEASIBILITY.read_text()
        for action in ("actions/checkout", "actions/upload-artifact"):
            mine = set(re.findall(rf"{action}@([0-9a-f]{{40}}) # (v[\d.]+)", self.text))
            theirs = set(re.findall(rf"{action}@([0-9a-f]{{40}}) # (v[\d.]+)", feasibility))
            self.assertTrue(mine and mine <= theirs, action)
        self.assertRegex(self.text, r"actions/download-artifact@[0-9a-f]{40} # v7\.\d+\.\d+")
        self.assertNotRegex(self.text, r"uses: [^@\s]+@(?![0-9a-f]{40}\b)")

    def test_triggers_jobs_permissions_and_artifacts(self):
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn("branches: [wip/seat-image-desktop, wip/unraid-native-plugin]", self.text)
        for path in ("unraid/seat-image/**", "unraid/scripts/ci-desktop-smoke.sh", "unraid/build-pins.env",
                     ".github/workflows/seat-image.yml"):
            self.assertIn(f"- '{path}'", self.text)
        self.assertRegex(self.text, r"(?m)^  seat-image:\n")
        self.assertRegex(self.text, r"(?m)^  desktop-smoke:\n    needs: seat-image\n")
        self.assertIn("name: polyseat-seat-image-NOT-FOR-UNRAID", self.text)
        self.assertIn("name: polyseat-desktop-smoke-NOT-FOR-UNRAID", self.text)
        self.assertNotIn("pull_request_target", self.text)
        self.assertNotIn("secrets.", self.text)


class Units(unittest.TestCase):
    def test_nothing_autostarts_and_nothing_is_network_facing_by_default(self):
        for unit in (OVERLAY / "etc/systemd/user").glob("*.service"):
            text = unit.read_text()
            self.assertNotIn("[Install]", text.replace("# Deliberately no [Install]", ""), unit.name)
            self.assertNotIn("WantedBy", text, unit.name)
        provision = (SEAT / "provision.sh").read_text()
        enabled = re.findall(r"systemctl (?:--global )?enable ([^\n]+)", provision)
        self.assertEqual(enabled, ["pipewire.socket pipewire-pulse.socket wireplumber.service"])
        for masked in ("systemd-networkd.service", "systemd-resolved.service", "systemd-timesyncd.service"):
            self.assertIn(masked, provision)

    def test_sway_unit_has_the_headless_cpu_environment(self):
        text = (OVERLAY / "etc/systemd/user/polyseat-sway.service").read_text()
        for line in ("WLR_BACKENDS=headless", "WLR_RENDERER=pixman", "WLR_HEADLESS_OUTPUTS=1", "LIBSEAT_BACKEND=noop",
                     "WLR_LIBINPUT_NO_DEVICES=1"):
            self.assertIn(f"Environment={line}", text)

    def test_sunshine_cpu_config_template(self):
        text = (OVERLAY / "usr/share/polyseat/sunshine.conf.in").read_text()
        self.assertRegex(text, r"(?m)^capture = wlr$")
        self.assertRegex(text, r"(?m)^encoder = @ENCODER@$")
        self.assertRegex(text, r"(?m)^origin_web_ui_allowed = pc$")

    def test_scripts_are_executable_and_parse(self):
        for script in (PROFILE, SELFTEST, OVERLAY / "usr/local/bin/polyseat-sunshine-run", SEAT / "provision.sh",
                       SEAT / "build-seat-image.sh", SMOKE):
            self.assertTrue(os.access(script, os.X_OK), script)
            shell = "bash" if script.read_text().startswith("#!/bin/bash") else "sh"
            self.assertEqual(subprocess.run([shell, "-n", str(script)], capture_output=True).returncode, 0, script)


class ProfileSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "usr/share/polyseat").mkdir(parents=True)
        (self.root / "usr/share/polyseat/sunshine.conf.in").write_bytes(
            (OVERLAY / "usr/share/polyseat/sunshine.conf.in").read_bytes())
        self.env = {"PATH": os.environ["PATH"], "POLYSEAT_ROOT": str(self.root)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_profile(self, *args):
        return subprocess.run([str(PROFILE), *args], capture_output=True, text=True, env=self.env)

    def dropin(self, unit):
        return (self.root / f"home/player/.config/systemd/user/{unit}.service.d/20-gpu.conf").read_text()

    def test_cpu_profile(self):
        self.assertEqual(self.run_profile("cpu").returncode, 0)
        self.assertEqual(self.run_profile("status").stdout.strip(), "cpu")
        for unit in ("polyseat-sway", "polyseat-sunshine"):
            self.assertEqual(self.dropin(unit), "[Service]\nEnvironment=WLR_RENDERER=pixman\n")
        conf = (self.root / "home/player/.config/sunshine/sunshine.conf").read_text()
        self.assertRegex(conf, r"(?m)^encoder = software$")
        self.assertRegex(conf, r"(?m)^capture = wlr$")
        self.assertFalse((self.root / "usr/lib/gbm/nvidia-drm_gbm.so").exists(follow_symlinks=False))

    def test_nvidia_profile_matches_upstream_userspace_fixups(self):
        self.assertEqual(self.run_profile("nvidia").returncode, 0)
        self.assertEqual(self.run_profile("status").stdout.strip(), "nvidia")
        for unit in ("polyseat-sway", "polyseat-sunshine"):
            text = self.dropin(unit)
            for line in ("WLR_RENDERER=gles2", "GBM_BACKEND=nvidia-drm", "__GLX_VENDOR_LIBRARY_NAME=nvidia"):
                self.assertIn(f"Environment={line}\n", text)
        conf = (self.root / "home/player/.config/sunshine/sunshine.conf").read_text()
        self.assertRegex(conf, r"(?m)^encoder = nvenc$")
        link = self.root / "usr/lib/gbm/nvidia-drm_gbm.so"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../libnvidia-allocator.so.1")
        glvnd = (self.root / "usr/share/glvnd/egl_vendor.d/10_nvidia.json").read_text()
        self.assertEqual(json.loads(glvnd), {"file_format_version": "1.0.0", "ICD": {"library_path": "libEGL_nvidia.so.0"}})
        # Byte-identical to what upstream's stepNvidiaUserspace writes.
        upstream = ('{\n    "file_format_version" : "1.0.0",\n    "ICD" : {\n        "library_path" : "libEGL_nvidia.so.0"\n'
                    '    }\n}\n')
        self.assertEqual(glvnd, upstream)

    def test_switching_back_to_cpu_undoes_the_nvidia_files_and_is_idempotent(self):
        for profile in ("nvidia", "cpu", "cpu"):
            self.assertEqual(self.run_profile(profile).returncode, 0)
        self.assertEqual(self.run_profile("status").stdout.strip(), "cpu")
        self.assertFalse((self.root / "usr/lib/gbm/nvidia-drm_gbm.so").is_symlink())
        self.assertFalse((self.root / "usr/share/glvnd/egl_vendor.d/10_nvidia.json").exists())
        self.assertNotIn("nvidia", self.dropin("polyseat-sway"))

    def test_cpu_leaves_foreign_files_with_the_same_path_alone(self):
        other = self.root / "usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        other.parent.mkdir(parents=True)
        other.write_text("{}\n")
        self.assertEqual(self.run_profile("cpu").returncode, 0)
        self.assertEqual(other.read_text(), "{}\n")

    def test_unknown_arguments_are_rejected(self):
        for args in ((), ("amd",), ("--help",)):
            self.assertEqual(self.run_profile(*args).returncode, 2, args)


class Selftest(unittest.TestCase):
    def test_usage_and_root_requirements(self):
        self.assertEqual(subprocess.run([str(SELFTEST)], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run([str(SELFTEST), "gpu"], capture_output=True).returncode, 2)
        if os.geteuid() != 0:
            result = subprocess.run([str(SELFTEST), "desktop"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")  # nothing but the JSON object may ever reach stdout

    def test_contract_strings(self):
        text = SELFTEST.read_text()
        self.assertTrue(text.startswith("#!/bin/bash\n"))
        self.assertRegex(text, r"(?m)^set -u$")
        self.assertIn("SYSTEM_WAIT=90", text)
        for needle in ("is-system-running", "user@$PUID.service", "get_outputs", "HEADLESS-", "grim", "/proc/net/",
                       "/var/lib/polyseat-selftest", "nvidia-smi -L", "Found H\\.264 encoder"):
            self.assertIn(needle, text)
        for forbidden in ("pacman", "curl", "wget", "ping ", "apt"):
            self.assertNotRegex(text, rf"(?m)^[^#]*\b{re.escape(forbidden)}", forbidden)

    def test_smoke_script_uses_the_fixed_names_and_config(self):
        text = SMOKE.read_text()
        for needle in ("CT=polyseat-desktop-cpu", "ALIAS=polyseat-desktop", "boot.autostart=false",
                       "security.privileged=false", "security.nesting=false", "limits.cpu=2", "limits.memory=2GiB",
                       "limits.processes=1024", "polyseat-selftest desktop"):
            self.assertIn(needle, text)

    def test_smoke_refuses_outside_a_github_runner(self):
        result = subprocess.run(["sh", str(SMOKE)], capture_output=True, text=True,
                                env={"PATH": os.environ["PATH"], "CI": "true", "GITHUB_ACTIONS": "true"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("GitHub-hosted disposable Linux runner required", result.stderr)


def fake_export() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add(name, data=b"", type=tarfile.REGTYPE, mode=0o644, mtime=1_700_000_000, **kw):
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.mtime, info.size = type, mode, mtime, len(data) if type == tarfile.REGTYPE else 0
            info.uid = info.gid = kw.pop("uid", 0)
            info.uname = info.gname = "junk"
            info.pax_headers = kw.pop("pax", {})
            for key, value in kw.items():
                setattr(info, key, value)
            tar.addfile(info, io.BytesIO(data) if type == tarfile.REGTYPE else None)
        add("etc/", type=tarfile.DIRTYPE, mode=0o755)
        add(".dockerenv")
        add("etc/hosts", b"127.0.0.1 x\n")
        add("etc/hostname", b"abc\n")
        add("etc/resolv.conf", b"nameserver 1.1.1.1\n")
        add("dev/null", type=tarfile.CHRTYPE)
        add("usr/bin/tool", b"binary", mode=0o755, mtime=1_600_000_000,
            pax={"SCHILY.xattr.security.capability": "cap", "atime": "1", "ctime": "2"})
        add("usr/bin/tool2", type=tarfile.LNKTYPE, linkname="usr/bin/tool")
        add("home/player/.bashrc", b"x", uid=1000, mtime=1_900_000_000)
        add("usr/bin/alias", type=tarfile.SYMTYPE, linkname="tool")
    return buf.getvalue()


class PackImage(unittest.TestCase):
    EPOCH = 1_793_000_000

    def pack(self, directory: Path):
        export = directory / "export.tar"
        export.write_bytes(fake_export())
        image = directory / "image.tar.xz"
        result = PACK.pack(export, image, self.EPOCH, "2026/10/29", "archlinux@sha256:" + "a" * 64, "1-1")
        return image, result

    def test_layout_normalisation_and_determinism(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, result = self.pack(Path(a))
            second, again = self.pack(Path(b))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(result["sha256"], hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual(result["size_bytes"], first.stat().st_size)
            with tarfile.open(first) as tar:
                names = tar.getnames()
                self.assertEqual(names[:2], ["metadata.yaml", "rootfs"])
                meta = tar.extractfile("metadata.yaml").read().decode()
                self.assertIn(f"creation_date: {self.EPOCH}\n", meta)
                self.assertIn("architecture: x86_64\n", meta)
                for dropped in ("rootfs/.dockerenv", "rootfs/etc/hosts", "rootfs/etc/hostname",
                                "rootfs/etc/resolv.conf", "rootfs/dev/null"):
                    self.assertNotIn(dropped, names)
                tool = tar.getmember("rootfs/usr/bin/tool")
                self.assertEqual(tool.mtime, 1_600_000_000)  # older than the epoch: kept
                self.assertEqual(tool.mode, 0o755)
                self.assertEqual(tool.pax_headers, {"SCHILY.xattr.security.capability": "cap"})
                self.assertEqual(tar.getmember("rootfs/home/player/.bashrc").mtime, self.EPOCH)  # clamped
                self.assertEqual(tar.getmember("rootfs/home/player/.bashrc").uid, 1000)
                self.assertEqual(tar.getmember("rootfs/usr/bin/tool2").linkname, "rootfs/usr/bin/tool")
                self.assertEqual(tar.getmember("rootfs/usr/bin/alias").linkname, "tool")
                self.assertTrue(all(m.uname == "" and m.gname == "" for m in tar.getmembers()))
            self.assertEqual(result["unpacked_bytes"], len(b"binary") + 1)
            self.assertIn(".dockerenv", result["dropped"])

    def test_package_list_and_sbom(self):
        packages = PACK.parse_pacman("sway 1.11-1\nsunshine 2026.914.233613-1.1\n")
        self.assertEqual(packages[1], {"name": "sunshine", "version": "2026.914.233613-1.1"})
        bom = PACK.cyclonedx(packages, self.EPOCH)
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(bom["components"][0]["purl"], "pkg:alpm/arch/sway@1.11-1?arch=x86_64")


if __name__ == "__main__":
    unittest.main()
