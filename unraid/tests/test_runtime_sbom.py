"""Runtime SBOM and OSV helper tests (offline; the OSV network call itself is not exercised)."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
        self.assertNotIn("golang:github.com/example/old@v1.0.0", refs)     # not both versions
        self.assertIn("host:libcap.so.2", refs)
        self.assertTrue(any(r.startswith("git:lxc/incus@b20ec6c3") for r in refs))
        self.assertTrue(any(r.startswith("git:lxc/lxc@2597434a") for r in refs))
        incus = next(c for c in doc["components"] if c["bom-ref"].startswith("git:lxc/incus@"))
        self.assertIn({"name": "polyseat:official-tag-object", "value": "f3242b56ed7ab0127f5716d03e270224e24c0762"},
                      incus["properties"])
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

    def test_pinned_source_findings_reference_the_actual_source_components(self):
        sbom = load("make-runtime-sbom")
        pins = sbom.pins_from(PINS.read_text())
        findings = []
        expected_refs = []
        for index, (key, repo) in enumerate(load("osv-check").COMMIT_REPOS.items()):
            findings.append({"scope": "pinned-source", "component": f"{repo}@{pins[key][:12]} ({key})",
                             "id": f"GO-2026-test-{index}"})
            expected_refs.append(f"git:{repo.removeprefix('https://github.com/')}@{pins[key]}")
        osv = self.tmp / "sources-osv.json"
        osv.write_text(json.dumps({"findings": findings}))
        result, output = self.run_sbom("--osv-report", str(osv))
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(output.read_text())
        refs = {component["bom-ref"] for component in doc["components"]}
        self.assertEqual([v["affects"][0]["ref"] for v in doc["vulnerabilities"]], expected_refs)
        for vulnerability in doc["vulnerabilities"]:
            self.assertIn(vulnerability["affects"][0]["ref"], refs)
            self.assertEqual(vulnerability["analysis"]["state"], "in_triage")
            self.assertIn("pinned source commit", vulnerability["analysis"]["detail"])

    def test_unmatched_runtime_findings_cannot_create_dangling_references(self):
        for scope, component in (
                ("linked", "github.com/example/absent@v1.0.0"),
                ("pinned-source", "https://github.com/lxc/lxc@000000000000 (LXC_COMMIT)")):
            with self.subTest(scope=scope):
                osv = self.tmp / f"unmatched-{scope}.json"
                osv.write_text(json.dumps({"findings": [
                    {"scope": scope, "component": component, "id": "GO-2026-test-unmatched"}]}))
                result, output = self.run_sbom("--osv-report", str(osv))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("does not match runtime inventory", result.stderr)
                self.assertFalse(output.exists())

    def test_never_overwrites(self):
        self.assertEqual(self.run_sbom()[0].returncode, 0)
        self.assertNotEqual(self.run_sbom()[0].returncode, 0)

    def test_cli_only_modules_are_included_and_coverage_is_explicit(self):
        cli = self.tmp / "incus-buildinfo.txt"
        cli.write_text(BUILDINFO + "\tdep\tcli.example/only\tv1.2.3\th1:example\n")
        result, output = self.run_sbom("--cli-buildinfo", str(cli))
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(output.read_text())
        self.assertIn("golang:cli.example/only@v1.2.3", [c["bom-ref"] for c in doc["components"]])
        props = {p["name"]: p["value"] for p in doc["metadata"]["properties"]}
        self.assertEqual(props["polyseat:binary-inventory-coverage"], "incusd-and-incus")

    def test_empty_cli_inventory_cannot_claim_two_binary_coverage(self):
        cli = self.tmp / "empty-cli-buildinfo.txt"
        cli.write_text("")
        result, output = self.run_sbom("--cli-buildinfo", str(cli))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("empty CLI binary inventory", result.stderr)
        self.assertFalse(output.exists())

    def test_canary_image_has_its_pinned_ubuntu_package_in_the_inventory(self):
        sbom = load("make-runtime-sbom")
        manifest = json.loads(self.manifest.read_text())
        manifest["files"].append({"path": "payload/share/canary/busybox.tar.gz", "sha256": "0" * 64,
                                  "mode": 0o644, "size": 1})
        pins = sbom.pins_from(PINS.read_text())
        doc = sbom.build(manifest, {}, BUILDINFO, pins, "localdev", None)
        busybox = next(c for c in doc["components"] if c["name"] == "busybox-static")
        self.assertEqual(busybox["version"], pins["BUSYBOX_DEB_VERSION"])
        self.assertEqual(busybox["hashes"][0]["content"], pins["BUSYBOX_DEB_SHA256"])
        pins.pop("BUSYBOX_BINARY_SHA256")
        with self.assertRaisesRegex(ValueError, "without pinned source"):
            sbom.build(manifest, {}, BUILDINFO, pins, "localdev", None)


class OsvHelpers(unittest.TestCase):
    def test_buildinfo_and_module_graph_parsers(self):
        osv = load("osv-check")
        self.assertEqual(osv.parse_buildinfo(BUILDINFO), [("github.com/example/fork", "v1.0.1"),
                                                           ("golang.org/x/net", "v0.59.0")])
        self.assertEqual(osv.parse_modules("a.b/c v1.2.3\nd.e/f v0.1.0 => g.h/f v0.1.1\n# x\n"),
                         [("a.b/c", "v1.2.3"), ("g.h/f", "v0.1.1")])

    def test_local_or_unbound_replacements_are_not_misreported_as_original_modules(self):
        osv = load("osv-check")
        sbom = load("make-runtime-sbom")
        for text in (BUILDINFO.replace("github.com/example/fork\tv1.0.1", "../local\t(devel)"),
                     "\t=>\tgithub.com/example/fork\tv1.0.1\n"):
            for parser in (osv.parse_buildinfo, sbom.linked_modules):
                with self.subTest(parser=parser, text=text), self.assertRaises(ValueError):
                    parser(text)
        with self.assertRaises(ValueError):
            osv.parse_modules("d.e/f v0.1.0 => ../local\n")

    def test_truthful_tagged_main_module_is_included_in_osv_and_runtime_inventory(self):
        text = BUILDINFO.replace("(devel)", "v7.5.1+dirty")
        for parser in (load("osv-check").parse_buildinfo, load("make-runtime-sbom").linked_modules):
            self.assertIn(("github.com/lxc/incus/v7", "v7.5.1+dirty"), parser(text))

    def test_osv_helper_refuses_to_run_on_unraid_by_source(self):
        text = (SCRIPTS / "osv-check.py").read_text()
        self.assertIn('Path("/etc/unraid-version").exists()', text)
        self.assertIn("api.osv.dev", text)
        self.assertNotIn("subprocess", text)

    def test_osv_refuses_incomplete_or_error_results_and_inventories_cli_union(self):
        osv = load("osv-check")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "modules.txt").write_text("golang.org/x/net v0.59.0\n")
            (base / "daemon.txt").write_text(BUILDINFO)
            (base / "cli.txt").write_text("\tdep\tcli.example/only\tv1.2.3\th1:abc\n")
            argv = ["osv-check.py", "--modules", str(base / "modules.txt"), "--buildinfo", str(base / "daemon.txt"),
                    "--cli-buildinfo", str(base / "cli.txt"), "--pins", str(PINS),
                    "--out-json", str(base / "report.json"), "--out-text", str(base / "report.txt")]
            for query in (lambda q: [], lambda q: [{"error": "failed"}] * len(q),
                          lambda q: [{"next_page_token": "more"}] * len(q)):
                with patch("sys.argv", argv), patch.object(osv, "query", side_effect=query), redirect_stderr(io.StringIO()):
                    self.assertEqual(osv.main(), 3)
                self.assertFalse((base / "report.json").exists())
            with patch("sys.argv", argv), patch.object(osv, "query", side_effect=lambda q: [{} for _ in q]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(osv.main(), 0)
            report = json.loads((base / "report.json").read_text())
            self.assertEqual(report["binary_inventory_coverage"], "incusd-and-incus")
            self.assertEqual(report["components_checked"]["linked"], 3)


if __name__ == "__main__":
    unittest.main()
