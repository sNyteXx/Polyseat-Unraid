"""Offline tests for the Polyseat desktop seat image sources: no Docker, no Incus, no network, no root."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
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
        self.assertIn("branches: [wip/seat-image-desktop]", self.text)
        # The WIP branch builds it once, through incus-feasibility.yml, for the desktop canary rehearsal.
        self.assertRegex(self.text, r"(?m)^  workflow_call:\n")
        caller = (Path(__file__).resolve().parents[2] / ".github/workflows/incus-feasibility.yml").read_text()
        self.assertRegex(caller, r"(?m)^  seat-image:\n    if: github.ref == 'refs/heads/wip/unraid-native-plugin'\n"
                                 r"    uses: \./\.github/workflows/seat-image\.yml\n")
        self.assertRegex(caller, r"(?m)^  desktop-canary:\n.*\n    needs: \[build, seat-image\]\n")
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

    def test_steam_modes_use_the_lan_only_when_asked(self):
        text = SELFTEST.read_text()
        self.assertIn("usage: polyseat-selftest desktop|nvidia|steam|steam-cpu", text)
        self.assertIn("steam) PROFILE=nvidia STEAM=1 ;;", text)
        self.assertIn("steam-cpu) PROFILE=cpu STEAM=1 ;;", text)
        self.assertIn("STEAM_WAIT=900", text)
        self.assertNotIn("vulkaninfo", text.split("# --- 6c. Steam:")[0].split("vulkaninfo cannot be used here")[1])  # Tower run 10
        for needle in ("glxinfo -B", "glxinfo32 -B", "/usr/lib32/libGLX_nvidia.so.0", "client-update.steamstatic.com",
                       "steamwebhelper", "steam -shutdown", "NETNS_ALLOW=$netns_allow",
                       "vkcube --wsi \"$wsi\" --c 120 --suppress_popups"):
            self.assertIn(needle, text)
        lan = text.index('# --- 1b. LAN (steam*)')
        self.assertIn('if [ "$STEAM" = 1 ]; then', text[lan:lan + 200])
        self.assertEqual(text.count("systemctl unmask"), 1)
        self.assertGreater(text.index("systemctl unmask"), lan)
        self.assertIn('if [ "$STEAM" = 1 ]; then netns_allow=eth0; else netns_allow=\'\'; fi', text)

    def test_steam_packages_cover_the_upstream_provisioner(self):
        upstream = (ROOT / "internal/seat/provision.go").read_text()
        step = re.search(r"func \(p \*Provisioner\) stepSteam.*?argv = append\(argv,(.*?)\)\n", upstream, re.S).group(1)
        wanted = set(re.findall(r'"([a-z0-9-]+)"', step))
        self.assertIn("steam", wanted)
        provision = (SEAT / "provision.sh").read_text()
        ours = set(re.search(r"STEAM_PACKAGES='([^']*)'", provision).group(1).split())
        self.assertLessEqual(wanted, ours, "the seat image must install everything upstream's stepSteam does")
        for flag in ("vulkan-driver", "lib32-vulkan-driver"):  # upstream's driverFlags on NVIDIA
            self.assertIn(f"--assume-installed {flag}", provision)

    def test_lan_config_is_eth0_dhcp4_only_and_sway_starts_xwayland_lazily(self):
        net = (OVERLAY / "etc/systemd/network/20-polyseat-lan.network").read_text()
        self.assertIn("[Match]\nName=eth0\n", net)
        for line in ("DHCP=ipv4", "LinkLocalAddressing=no", "IPv6AcceptRA=no", "UseDomains=no"):
            self.assertIn(line + "\n", net)
        self.assertRegex((OVERLAY / "etc/sway/config").read_text(), r"(?m)^xwayland enable$")
        provision = (SEAT / "provision.sh").read_text()
        self.assertIn("STEAM_PACKAGES='steam lib32-libglvnd lib32-mesa lib32-vulkan-icd-loader ttf-liberation zenity", provision)
        self.assertIn("STEAM_ASSUME='--assume-installed vulkan-driver --assume-installed lib32-vulkan-driver'", provision)

    def test_session_script_contract(self):
        text = (OVERLAY / "usr/local/bin/polyseat-session").read_text()
        self.assertTrue(text.startswith("#!/bin/bash\n"))
        self.assertTrue(os.access(OVERLAY / "usr/local/bin/polyseat-session", os.X_OK))
        for needle in ('[[ $pin =~ ^[0-9]{4}$ ]]', "[[ $id =~ ^[0-9a-fA-F]{32}$ ]]", '1) id=$(jq -r \'.pairings[0].id\'',
                       "nothing was paired", "SUNSHINE_URL=https://127.0.0.1:47990", "umask 077",
                       "sunshine --creds polyseat", '[ -e /sys/class/net/eth0 ] || fail', 'rm -f "$CRED"'):
            self.assertIn(needle, text)
        # Only Sunshine's own API on localhost: no other URL, no package manager.
        self.assertEqual(sorted(set(re.findall(r"https?://[^\s\"']+", text))), ["https://127.0.0.1:47990"])
        for forbidden in ("pacman", "wget", "apt", "uinput"):
            self.assertNotRegex(text, rf"(?m)^[^#]*\b{re.escape(forbidden)}", forbidden)

    def test_no_input_devices_and_a_stable_lan_address(self):
        conf = (OVERLAY / "usr/share/polyseat/sunshine.conf.in").read_text()
        for line in ("keyboard = disabled", "mouse = disabled", "controller = disabled", "upnp = disabled",
                     "origin_web_ui_allowed = pc"):
            self.assertIn(line + "\n", conf)
        self.assertIn("ClientIdentifier=mac\n", (OVERLAY / "etc/systemd/network/20-polyseat-lan.network").read_text())

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

    def test_stdin_stream_gives_the_same_bytes_and_a_truncated_export_leaves_nothing(self):
        script = SEAT / "pack-image.py"
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, _ = self.pack(Path(a))
            (Path(b) / "q.txt").write_text("sunshine 1-1\n")
            argv = [sys.executable, "-I", str(script), "--export", "-", "--pacman-q", str(Path(b) / "q.txt"),
                    "--out-dir", str(Path(b) / "out"), "--epoch", str(self.EPOCH), "--archive-date", "2026/10/29",
                    "--base-image", "archlinux@sha256:" + "a" * 64, "--sunshine-version", "1-1",
                    "--sunshine-url", "https://example.invalid/s.pkg.tar.zst", "--sunshine-sha256", "b" * 64,
                    "--build-commit", "c" * 40]
            subprocess.run(argv, input=fake_export(), check=True, capture_output=True)
            self.assertEqual((Path(b) / "out/polyseat-desktop.tar.xz").read_bytes(), first.read_bytes())
            (Path(b) / "out/polyseat-desktop.tar.xz").unlink()
            res = subprocess.run(argv, input=fake_export()[:1536], capture_output=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertFalse((Path(b) / "out/polyseat-desktop.tar.xz").exists())

    def test_package_list_and_sbom(self):
        packages = PACK.parse_pacman("sway 1.11-1\nsunshine 2026.914.233613-1.1\n")
        self.assertEqual(packages[1], {"name": "sunshine", "version": "2026.914.233613-1.1"})
        bom = PACK.cyclonedx(packages, self.EPOCH)
        self.assertEqual(bom["bomFormat"], "CycloneDX")
        self.assertEqual(bom["components"][0]["purl"], "pkg:alpm/arch/sway@1.11-1?arch=x86_64")


if __name__ == "__main__":
    unittest.main()


class NetnsClassify(unittest.TestCase):
    """The seat's network-isolation check (Tower run 4 saw `bonding_masters lo tunl0` and failed the old check)."""
    TOOL = OVERLAY / "usr/local/lib/polyseat/netns-classify"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.net = self.root / "net"
        self.net.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.addresses = self.root / "addresses"
        self.addresses.mkdir()
        # Stub `ip -o address show dev NAME`: prints whatever the fixture says that device has.
        (self.bin / "ip").write_text('#!/bin/sh\ncat "$ADDRS/$5" 2>/dev/null\n')
        (self.bin / "ip").chmod(0o755)

    def dev(self, name, flags="0x0", address=""):
        target = self.root / "devices" / name
        target.mkdir(parents=True)
        (target / "flags").write_text(flags + "\n")
        (self.net / name).symlink_to(target)
        if address:
            (self.addresses / name).write_text(address + "\n")

    def run_tool(self, allow=None):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "ADDRS": str(self.addresses)}
        if allow is not None:
            env["NETNS_ALLOW"] = allow
        return subprocess.run(["bash", str(self.TOOL), str(self.net)], capture_output=True, text=True, env=env)

    def test_lan_self_test_accepts_exactly_the_allowed_interface_when_up(self):
        self.dev("lo", "0x9")
        self.dev("tunl0", "0x80")
        self.dev("eth0", "0x1003")
        self.assertEqual(self.run_tool().returncode, 1)  # without the allowance eth0 stays unexpected
        res = self.run_tool(allow="eth0")
        self.assertEqual((res.returncode, res.stdout.strip()),
                         (0, "lo=yes inert=tunl0 unexpected= allowed=eth0 missing="))
        (self.root / "devices/eth0/flags").write_text("0x1002\n")  # down
        res = self.run_tool(allow="eth0")
        self.assertEqual((res.returncode, res.stdout.strip()),
                         (1, "lo=yes inert=tunl0 unexpected=eth0 allowed= missing=eth0"))
        (self.root / "devices/eth0/flags").write_text("0x1003\n")
        self.dev("eth1", "0x1003")
        self.assertEqual(self.run_tool(allow="eth0").returncode, 1)  # a second NIC is never allowed
        for p in (self.net / "eth0", self.net / "eth1"):
            p.unlink()
        res = self.run_tool(allow="eth0")
        self.assertEqual((res.returncode, res.stdout.strip()), (1, "lo=yes inert=tunl0 unexpected= allowed= missing=eth0"))

    def test_tower_run4_view_is_isolated(self):
        self.dev("lo", "0x9")
        self.dev("tunl0", "0x80")  # ipip fallback device: down, no address
        (self.net / "bonding_masters").write_text("\n")  # a file from the host's bonding module, not a device
        res = self.run_tool()
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(res.stdout.strip(), "lo=yes inert=tunl0 unexpected=")

    def test_lo_only_ci_view(self):
        self.dev("lo", "0x9")
        res = self.run_tool()
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "lo=yes inert= unexpected="))

    def test_any_real_or_active_interface_fails(self):
        cases = {"eth0": ("0x1003", ""), "veth1": ("0x0", ""), "tunl0-up": ("0x81", ""),
                 "tunl0-addr": ("0x80", "5: tunl0 inet 10.0.0.1/32 scope global tunl0")}
        for label, (flags, address) in cases.items():
            with self.subTest(label=label):
                for p in list(self.net.iterdir()):
                    p.unlink()
                for p in self.addresses.iterdir():
                    p.unlink()
                import shutil
                shutil.rmtree(self.root / "devices", ignore_errors=True)
                self.dev("lo", "0x9")
                self.dev(label.split("-")[0], flags, address)
                res = self.run_tool()
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn(label.split("-")[0], res.stdout.split("unexpected=")[1])

    def test_missing_loopback_fails(self):
        self.dev("tunl0", "0x80")
        self.assertNotEqual(self.run_tool().returncode, 0)
