"""Runtime SBOM and OSV helper tests (offline; the OSV network call itself is not exercised)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from unraid.lab import bundle

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "unraid" / "scripts"
PINS = ROOT / "unraid" / "build-pins.env"

BUILDINFO = """/x/incusd: go1.26.7
\tpath\tgithub.com/lxc/incus/v7/cmd/incusd
\tmod\tgithub.com/lxc/incus/v7\t(devel)\t
\tdep\tgolang.org/x/net\tv0.59.0\th1:abc=
\tdep\tgithub.com/example/old\tv1.0.0\th1:def=
\t=>\tgithub.com/example/fork\tv1.0.1\th1:ghi=
"""


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeSbom(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-sbom-test-")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        payload = self.tmp / "payload"
        (payload / "bin").mkdir(parents=True)
        (payload / "lib").mkdir()
        (payload / "bin/incusd").write_bytes(b"d")
        (payload / "bin/incusd").chmod(0o755)
        (payload / "lib/liblxc.so.1").write_bytes(b"l")
        out = self.tmp / "out"
        out.mkdir()
        _, self.manifest = bundle.build(payload, out, name="polyseat-incus", version="7.5.1-polyseat.test",
                                        incus_version="7.5.1", min_unraid="7.2.0", min_glibc="2.38",
                                        needed_libs=["libcap.so.2"])
        self.abi = self.tmp / "abi.json"
        self.abi.write_text(json.dumps({"ok": True, "host_libraries": ["libcap.so.2", "libc.so.6"], "glibc_max": "2.38"}))
        self.buildinfo = self.tmp / "buildinfo.txt"
        self.buildinfo.write_text(BUILDINFO)

    def run_sbom(self, *extra, label="ci"):
        output = self.tmp / "sbom.json"
        result = subprocess.run(["python3", str(SCRIPTS / "make-runtime-sbom.py"), "--manifest", str(self.manifest),
                                 "--abi-report", str(self.abi), "--buildinfo", str(self.buildinfo), "--pins", str(PINS),
                                 "--label", label, "--output", str(output), *extra], capture_output=True, text=True)
        return result, output

    def test_inventory_covers_files_pins_modules_and_host_libraries(self):
        result, output = self.run_sbom()
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(output.read_text())
        refs = [c["bom-ref"] for c in doc["components"]]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertIn("file:payload/bin/incusd", refs)
        self.assertIn("golang:golang.org/x/net@v0.59.0", refs)
        self.assertIn("golang:github.com/example/fork@v1.0.1", refs)       # replacement resolved
        self.assertIn("host:libcap.so.2", refs)
        self.assertTrue(any(r.startswith("git:lxc/incus@b20ec6c3") for r in refs))
        self.assertTrue(any(r.startswith("git:lxc/lxc@2597434a") for r in refs))
        for component in doc["components"]:
            if component["type"] == "file":
                self.assertRegex(component["hashes"][0]["content"], r"^[0-9a-f]{64}$")
        props = {p["name"]: p["value"] for p in doc["metadata"]["properties"]}
        self.assertEqual(props["polyseat:vulnerability-review"], "not-performed")
        self.assertEqual(props["polyseat:unraid-certified"], "false")
        self.assertEqual(props["polyseat:signed"], "false")
        self.assertEqual(props["polyseat:built-without"], "apparmor,selinux")

    def test_osv_findings_are_only_ever_in_triage(self):
        osv = self.tmp / "osv.json"
        osv.write_text(json.dumps({"findings": [
            {"scope": "linked", "component": "golang.org/x/net@v0.59.0", "id": "GO-2026-0001"},
            {"scope": "graph-only", "component": "github.com/example/never@v0", "id": "GHSA-nope"}]}))
        result, output = self.run_sbom("--osv-report", str(osv))
        self.assertEqual(result.returncode, 0, result.stderr)
        vulns = json.loads(output.read_text())["vulnerabilities"]
        self.assertEqual([v["id"] for v in vulns], ["GO-2026-0001"])          # graph-only modules are not shipped
        self.assertEqual(vulns[0]["analysis"]["state"], "in_triage")
        self.assertEqual(vulns[0]["affects"][0]["ref"], "golang:golang.org/x/net@v0.59.0")

    def test_never_overwrites(self):
        self.assertEqual(self.run_sbom()[0].returncode, 0)
        self.assertNotEqual(self.run_sbom()[0].returncode, 0)


class OsvHelpers(unittest.TestCase):
    def test_buildinfo_and_module_graph_parsers(self):
        osv = load("osv-check")
        self.assertEqual(osv.parse_buildinfo(BUILDINFO), [("github.com/example/fork", "v1.0.1"),
                                                           ("github.com/example/old", "v1.0.0"),
                                                           ("golang.org/x/net", "v0.59.0")])
        self.assertEqual(osv.parse_modules("a.b/c v1.2.3\nd.e/f v0.1.0 => ../local\n# x\n"),
                         [("a.b/c", "v1.2.3"), ("d.e/f", "v0.1.0")])

    def test_osv_helper_refuses_to_run_on_unraid_by_source(self):
        text = (SCRIPTS / "osv-check.py").read_text()
        self.assertIn('Path("/etc/unraid-version").exists()', text)
        self.assertIn("api.osv.dev", text)
        self.assertNotIn("subprocess", text)


if __name__ == "__main__":
    unittest.main()
