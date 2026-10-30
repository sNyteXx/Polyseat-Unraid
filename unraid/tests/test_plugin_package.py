"""PLG template, package builder and denylist checks. Output goes to /tmp only."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PLUGIN = ROOT / "plugin"
TEMPLATE = PLUGIN / "template" / "polyseat-unraid.plg.template"

FORBIDDEN = [
    r"\bdocker\b", r"\bvirsh\b", r"\blibvirt", r"\bvfio", r"\bmodprobe\b", r"\brmmod\b", r"\binsmod\b",
    r"\bbrctl\b", r"\bip\s+(link|addr|route|rule)\b", r"\biptables\b", r"\bnft\b", r"\bbr0\b",
    r"\bvirbr", r"\bsysctl\b", r"\bumount\b", r"\bmount\s", r"\breboot\b", r"\bshutdown\s+-", r"/etc/rc\.d/",
    r"\bnvidia-", r"\bdnsmasq\b",
]


def code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def run(*args, cwd=None):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, cwd=cwd)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-plg-test-")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def render(self, **overrides):
        package = self.tmp / "polyseat-unraid-2026.10.30-noarch-1.txz"
        package.write_bytes(b"package")
        sha = "ab" * 32
        bundles = self.tmp / "bundles"
        bundles.mkdir(exist_ok=True)
        (bundles / f"{sha}.manifest.json").write_text("{}")
        (bundles / f"{sha}.manifest.json.sig").write_text("sig")
        signer = self.tmp / "key.pub"
        signer.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest comment\n")
        out = self.tmp / "rendered.plg"
        args = {"version": "2026.10.30", "base": "https://example.invalid/releases/v1", "sha": sha}
        args.update(overrides)
        result = run(SCRIPTS / "render-plg.sh", args["version"], args["base"], package, args["sha"],
                     bundles, signer, out)
        return result, out, sha

    def test_template_is_not_a_plugin_file_and_cannot_work_unrendered(self):
        self.assertFalse(TEMPLATE.name.endswith(".plg"))
        self.assertFalse((PLUGIN / "polyseat-unraid.plg").exists())
        text = TEMPLATE.read_text()
        for token in ("@VERSION@", "@RELEASE_BASE_URL@", "@BUNDLE_SHA256@", "@PACKAGE_SHA256@", "@SIGNER_LINE@"):
            self.assertIn(token, text)
        self.assertEqual(sorted(p.name for p in PLUGIN.rglob("*.plg*")),
                         ["polyseat-unraid.plg.disabled", "polyseat-unraid.plg.template"])

    def test_rendered_plugin_is_wellformed_and_hash_pinned(self):
        result, out, sha = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.fromstring(out.read_text())
        self.assertEqual(root.tag, "PLUGIN")
        self.assertEqual(root.get("name"), "polyseat-unraid")
        self.assertEqual(root.get("version"), "2026.10.30")
        self.assertEqual(root.get("min"), "7.2.0")
        self.assertTrue(root.get("pluginURL").startswith("https://"))
        downloads = [f for f in root.findall("FILE") if f.find("URL") is not None]
        self.assertEqual(len(downloads), 4)
        for entry in downloads:
            self.assertTrue(entry.get("Name").startswith("/boot/config/plugins/polyseat-unraid/"))
            self.assertRegex(entry.find("SHA256").text.strip(), r"^[0-9a-f]{64}$")
            self.assertTrue(entry.find("URL").text.strip().startswith("https://"))
        names = [f.get("Name") for f in downloads]
        self.assertIn(f"/boot/config/plugins/polyseat-unraid/bundles/{sha}.tar.gz", names)
        self.assertIn("2026.10.30", names[0])
        package_hash = hashlib.sha256(b"package").hexdigest()
        self.assertEqual(downloads[0].find("SHA256").text.strip(), package_hash)

    def test_no_placeholder_survives_rendering(self):
        result, out, _ = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(re.search(r"@[A-Z_]+@", out.read_text()))

    def test_inline_scripts_parse_and_stay_in_scope(self):
        result, out, _ = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.fromstring(out.read_text())
        scripts = [f for f in root.findall("FILE") if f.get("Run") == "/bin/bash"]
        self.assertEqual(len(scripts), 3)
        install_order = [f.get("Method") for f in scripts]
        self.assertEqual(install_order, [None, None, "remove"])
        for index, entry in enumerate(scripts):
            path = self.tmp / f"inline{index}.sh"
            path.write_text(entry.find("INLINE").text)
            check = run("bash", "-n", path)
            self.assertEqual(check.returncode, 0, check.stderr)
            body = entry.find("INLINE").text
            self.assertNotIn("rm -r", body)
            self.assertNotIn("rm -f -r", body)
            for pattern in FORBIDDEN:
                self.assertIsNone(re.search(pattern, body), (index, pattern))
        activate = scripts[1].find("INLINE").text
        self.assertIn("recover --approve", activate)
        self.assertLess(activate.index("recover --approve"), activate.index("activate " + "ab" * 32))
        remove = scripts[2].find("INLINE").text
        self.assertIn("uninstall --approve", remove)
        for forbidden_target in ("/mnt", "DATA_DIR", ".cfg", "appdata"):
            self.assertNotIn(forbidden_target, remove.replace("/boot/config/plugins/polyseat-unraid/pkg", ""))

    def test_plugin_never_writes_settings_and_defaults_start_disabled(self):
        result, out, _ = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        root = ET.fromstring(out.read_text())
        self.assertEqual(root.get("launch"), "Settings/PolyseatSettings")
        names = [f.get("Name") or "" for f in root.findall("FILE")]
        self.assertFalse([n for n in names if n.endswith(".cfg")])   # user settings are never touched
        schema = json.loads((PLUGIN / "src/emhttp/schema.json").read_text())
        self.assertEqual(schema["SERVICE"]["default"], "disabled")
        self.assertEqual(schema["DATA_DIR"]["default"], "")
        self.assertEqual(schema["ACCEPT_NO_LSM"]["default"], "no")

    def test_unsafe_render_inputs_are_rejected(self):
        for overrides in ({"version": "1.0"}, {"version": "2026.10.30;rm"}, {"base": "http://x.invalid/a"},
                          {"base": "https://x.invalid/a&b"}, {"sha": "xyz"}, {"sha": "ab" * 31}):
            result, out, _ = self.render(**overrides)
            self.assertEqual(result.returncode, 2, (overrides, result.stderr))
            self.assertFalse(out.exists())

    def test_render_refuses_to_write_inside_the_repository(self):
        package = self.tmp / "p.txz"
        package.write_bytes(b"p")
        sha = "cd" * 32
        (self.tmp / f"{sha}.manifest.json").write_text("{}")
        (self.tmp / f"{sha}.manifest.json.sig").write_text("s")
        signer = self.tmp / "k.pub"
        signer.write_text("ssh-ed25519 AAAA\n")
        target = PLUGIN / "polyseat-unraid.plg"
        result = run(SCRIPTS / "render-plg.sh", "2026.10.30", "https://x.invalid/a", package, sha, self.tmp,
                     signer, target)
        self.assertEqual(result.returncode, 2)
        self.assertIn("inside the repository", result.stderr)
        self.assertFalse(target.exists())


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-pkg-test-")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def build(self, version="2026.10.30", gate="closed", out=None):
        out = out or self.tmp
        return run(SCRIPTS / "build-plugin-package.sh", version, out, gate)

    def test_package_contents_modes_and_gate(self):
        result = self.build(gate="test-host")
        self.assertEqual(result.returncode, 0, result.stderr)
        package = self.tmp / "polyseat-unraid-2026.10.30-noarch-1.txz"
        with tarfile.open(package, "r:xz") as tar:
            members = {m.name: m for m in tar.getmembers()}
            base = "usr/local/emhttp/plugins/polyseat-unraid"
            for name in ("scripts/polyseatctl", "event/disks_mounted", "event/stopping_svcs"):
                self.assertEqual(members[f"{base}/{name}"].mode, 0o755, name)
            for name in ("PolyseatTool.page", "PolyseatSettings.page", "PolyseatTile.page", "Polyseat.page", "schema.json",
                         "include/lib.php", "include/api.php", "js/psu.js", "css/psu.css", "icons/polyseat-unraid.svg"):
                self.assertEqual(members[f"{base}/{name}"].mode, 0o644, name)
            for member in members.values():
                self.assertTrue(member.isreg() or member.isdir(), member.name)
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
            ctl = tar.extractfile(members[f"{base}/scripts/polyseatctl"]).read().decode()
            self.assertIn('POLYSEAT_RELEASE_GATE="test-host"', ctl)
            self.assertEqual(ctl.count("POLYSEAT_RELEASE_GATE="), 1)  # exactly one assignment
            self.assertIn("install/slack-desc", members)
        self.assertIn('POLYSEAT_RELEASE_GATE="closed"',
                      (PLUGIN / "src/emhttp/scripts/polyseatctl").read_text())

    def test_package_is_reproducible_and_gate_open_is_refused(self):
        first, second = self.tmp / "a", self.tmp / "b"
        first.mkdir()
        second.mkdir()
        self.assertEqual(self.build(out=first).returncode, 0)
        self.assertEqual(self.build(out=second).returncode, 0)
        name = "polyseat-unraid-2026.10.30-noarch-1.txz"
        self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        refused = self.build(gate="open")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("separately approved", refused.stderr)
        again = self.build(out=first)
        self.assertEqual(again.returncode, 2)
        self.assertIn("overwrite", again.stderr)

    def test_bad_versions_are_rejected(self):
        for version in ("1.0", "2026.10.30;x", "2026.10.30/../x"):
            self.assertEqual(self.build(version=version).returncode, 2, version)


class DenylistTests(unittest.TestCase):
    """The plugin must never contain commands that alter Docker, libvirt, networking, GPUs or modules."""

    FILES = [
        PLUGIN / "src/emhttp/scripts/polyseatctl",
        PLUGIN / "src/emhttp/event/disks_mounted",
        PLUGIN / "src/emhttp/event/stopping_svcs",
        PLUGIN / "src/emhttp/PolyseatTool.page",
        PLUGIN / "src/emhttp/PolyseatSettings.page",
        PLUGIN / "src/emhttp/PolyseatTile.page",
        PLUGIN / "src/emhttp/include/lib.php",
        PLUGIN / "src/emhttp/include/api.php",
    ]

    def test_no_forbidden_host_mutations_in_shipped_code(self):
        for path in self.FILES:
            for line in code_lines(path):
                for pattern in FORBIDDEN:
                    if re.search(pattern, line):
                        # The controller may *read* or log about these; it may not run them.
                        self.assertRegex(line, r"(log |echo |printf |die |#|\".*\"|'.*')",
                                         f"{path.name}: {line}")

    def test_shipped_code_never_runs_python_or_other_missing_host_tools(self):
        for path in self.FILES:
            body = "\n".join(code_lines(path))
            self.assertNotRegex(body, r"\bpython3?\b")
            self.assertNotRegex(body, r"\bsystemctl\b")

    def test_event_scripts_only_call_the_controller(self):
        for name, expected in (("disks_mounted", "boot"), ("stopping_svcs", "stop")):
            body = (PLUGIN / "src/emhttp/event" / name).read_text()
            self.assertIn(f'"$CTL" {expected}', body)
            check = run("bash", "-n", PLUGIN / "src/emhttp/event" / name)
            self.assertEqual(check.returncode, 0, check.stderr)
        self.assertNotIn("--approve", (PLUGIN / "src/emhttp/event/disks_mounted").read_text())



if __name__ == "__main__":
    unittest.main()
