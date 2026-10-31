"""Offline dependency overlay guards: no compiler, network, installation or services."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "unraid/scripts/prepare-incus-deps.py"
PLAN = ROOT / "unraid/dependency-overrides.json"
PINS = ROOT / "unraid/build-pins.env"
spec = importlib.util.spec_from_file_location("prepare_incus_deps", SCRIPT)
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)


class DependencyPlan(unittest.TestCase):
    def setUp(self):
        self.pins = deps.load_pins(PINS)
        self.plan = json.loads(PLAN.read_text())
        self.modules = deps.validate_plan(self.plan, self.pins)

    def test_committed_plan_is_exact_and_has_authenticated_sums(self):
        self.assertEqual(len(self.modules), 10)
        versions = {entry["path"]: entry["version"] for entry in self.modules}
        self.assertEqual(versions["golang.org/x/net"], "v0.62.0")
        self.assertEqual(versions["github.com/go-jose/go-jose/v4"], "v4.1.6")
        self.assertEqual(versions["github.com/go-chi/chi/v5"], "v5.4.0")
        self.assertFalse(next(m for m in self.modules if m["path"] == "golang.org/x/telemetry")["direct"])

    def test_rejects_wrong_release_mutable_versions_missing_sums_and_duplicate_modules(self):
        bad_plans = []
        for key in ("incus_commit", "incus_release", "schema"):
            bad = copy.deepcopy(self.plan)
            bad[key] = "wrong"
            bad_plans.append(bad)
        for key, value in (("version", "latest"), ("version", "v0.62.0;echo"), ("sum", ""),
                           ("go_mod_sum", "h1:wrong"), ("path", "../../local"), ("reason", ""),
                           ("direct", "false")):
            bad = copy.deepcopy(self.plan)
            bad["modules"][0][key] = value
            bad_plans.append(bad)
        bad = copy.deepcopy(self.plan)
        bad["modules"].append(bad["modules"][0])
        bad_plans.append(bad)
        for bad in bad_plans:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                deps.validate_plan(bad, self.pins)

    def test_missing_or_duplicate_tag_pin_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            pinfile = Path(directory) / "pins.env"
            for text in (PINS.read_text().replace("INCUS_TAG_OBJECT=", "OTHER="),
                         PINS.read_text() + "INCUS_RELEASE=v7.5.1\n"):
                pinfile.write_text(text)
                with self.assertRaises(ValueError):
                    deps.load_pins(pinfile)

    def test_only_reviewed_module_graph_changes_are_accepted(self):
        before = {m["path"]: m["from"] for m in self.modules}
        before["unchanged.example/dependency"] = "v1.0.0"
        after = {m["path"]: m["version"] for m in self.modules}
        after["unchanged.example/dependency"] = "v1.0.0"
        self.assertEqual(len(deps.check_graph(before, after, self.modules)), 10)
        for mutate in (lambda x: x.update({"unchanged.example/dependency": "v2.0.0"}),
                       lambda x: x.pop("unchanged.example/dependency"),
                       lambda x: x.update({"added.example/unreviewed": "v1.0.0"}),
                       lambda x: x.update({"golang.org/x/net": "v0.63.0"})):
            changed = dict(after)
            mutate(changed)
            with self.assertRaises(ValueError):
                deps.check_graph(before, changed, self.modules)
        before["golang.org/x/net"] = "v0.58.0"
        with self.assertRaisesRegex(ValueError, "baseline"):
            deps.check_graph(before, after, self.modules)

    def test_download_requires_path_version_zip_and_mod_checksums(self):
        expected = self.modules[0]
        actual = {"Path": expected["path"], "Version": expected["version"], "Sum": expected["sum"],
                  "GoModSum": expected["go_mod_sum"]}
        deps.check_download(actual, expected)
        for key in actual:
            with self.subTest(key=key), self.assertRaises(ValueError):
                deps.check_download({**actual, key: "tampered"}, expected)
        with self.assertRaises(ValueError):
            deps.check_download({**actual, "Error": "bad download"}, expected)

    def test_source_fixed_evidence_is_bound_to_module_checksums_and_actual_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "fixed.go").write_text("reviewed guard")
            expected = {"path": "example.org/module", "version": "v1.0.0", "sum": "h1:abc", "go_mod_sum": "h1:def",
                        "files": [{"path": "fixed.go", "sha256": deps.sha256(source / "fixed.go")}]}
            actual = {"Path": expected["path"], "Version": expected["version"], "Sum": expected["sum"],
                      "GoModSum": expected["go_mod_sum"], "Dir": str(source)}
            deps.check_source_evidence(actual, expected)
            (source / "fixed.go").write_text("guard removed")
            with self.assertRaisesRegex(ValueError, "changed"):
                deps.check_source_evidence(actual, expected)

    def test_authenticated_zip_sums_are_materialized_before_readonly_compile(self):
        upstream = "unchanged.example/a v1.0.0 h1:unchanged\n"
        merged = deps.materialize_verified_sums(upstream, self.modules)
        self.assertIn(upstream.strip(), merged.splitlines())
        self.assertEqual(len(merged.splitlines()), 21)
        for entry in self.modules:
            self.assertIn(f"{entry['path']} {entry['version']} {entry['sum']}", merged.splitlines())
            self.assertIn(f"{entry['path']} {entry['version']}/go.mod {entry['go_mod_sum']}", merged.splitlines())
        self.assertEqual(deps.materialize_verified_sums(merged, self.modules), merged)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            deps.materialize_verified_sums("golang.org/x/net v0.62.0 h1:wrong\n", self.modules)
        with self.assertRaisesRegex(ValueError, "malformed"):
            deps.materialize_verified_sums("bad entry\n", self.modules)
            expected["files"][0]["path"] = "../other"
            with self.assertRaisesRegex(ValueError, "unsafe"):
                deps.check_source_evidence(actual, expected)
            expected["files"] = []
            with self.assertRaisesRegex(ValueError, "actual file evidence"):
                deps.check_source_evidence(actual, expected)

    def test_json_stream_and_graph_reject_invalid_or_replaced_metadata(self):
        text = '{"Main":true,"Path":"github.com/lxc/incus/v7"}\n{"Path":"x.y/z","Version":"v1.0.0"}'
        self.assertEqual(deps.module_graph(text), {"x.y/z": "v1.0.0"})
        for bad in ("", "[]", text + "garbage", '{"Path":"x.y/z","Version":"v1.0.0","Replace": {"Dir":"/tmp/local"}}',
                    '{"Path":"x.y/z","Version":"v1.0.0","Error":{"Err":"missing"}}', text + text):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                deps.module_graph(bad)

    def test_public_checksums_and_no_auto_toolchain_cannot_be_overridden(self):
        with patch.dict(os.environ, {"GOSUMDB": "off", "GOPROXY": "https://untrusted.invalid", "GOTOOLCHAIN": "auto",
                                     "GONOSUMDB": "*", "GONOPROXY": "*", "GOINSECURE": "*"}):
            env = deps.public_go_environment()
        self.assertEqual(env["GOSUMDB"], "sum.golang.org")
        self.assertEqual(env["GOPROXY"], "https://proxy.golang.org")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOWORK"], "off")
        self.assertTrue(all(env[key] == "" for key in ("GONOSUMDB", "GONOPROXY", "GOPRIVATE", "GOINSECURE")))

    def test_dirty_source_is_refused_before_any_go_command(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            (source / ".git").mkdir(parents=True)
            replies = [self.pins["INCUS_COMMIT"], self.pins["INCUS_TAG_OBJECT"], self.pins["INCUS_COMMIT"], " M go.mod"]
            with patch.object(deps, "run", side_effect=replies) as run, self.assertRaisesRegex(ValueError, "already dirty"):
                deps.prepare(source, self.pins, PLAN, Path(directory) / "report.json")
            self.assertEqual(run.call_count, 4)
            self.assertTrue(all(call.args[1] == "git" for call in run.call_args_list))

    def test_wrong_source_or_tag_is_refused_before_go(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            (source / ".git").mkdir(parents=True)
            for replies in (["0" * 40], [self.pins["INCUS_COMMIT"], "0" * 40],
                            [self.pins["INCUS_COMMIT"], self.pins["INCUS_TAG_OBJECT"], "0" * 40]):
                with patch.object(deps, "run", side_effect=replies) as run, self.assertRaises(ValueError):
                    deps.prepare(source, self.pins, PLAN, Path(directory) / "report.json")
                self.assertTrue(all(call.args[1] == "git" for call in run.call_args_list))

    def test_host_guard_and_existing_output_refuse_before_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            (source / ".git").mkdir(parents=True)
            report = Path(directory) / "report.json"
            report.write_text("must stay")
            with patch.object(deps, "run") as run, self.assertRaisesRegex(ValueError, "overwrite"):
                deps.prepare(source, self.pins, PLAN, report)
            run.assert_not_called()
            self.assertEqual(report.read_text(), "must stay")
            with patch.object(Path, "exists", return_value=True), patch.object(deps, "run") as run, \
                    self.assertRaisesRegex(ValueError, "Unraid"):
                deps.prepare(source, self.pins, PLAN, report)
            run.assert_not_called()

    def test_core_enforces_correct_metadata_and_copies_provenance(self):
        core = (ROOT / "unraid/scripts/lib/build-core.sh").read_text()
        # Incus and squashfs-tools are only accepted from their pinned annotated tag objects, peeled and checked.
        self.assertIn("tag_object=$INCUS_TAG_OBJECT", core)
        self.assertIn("tag_object=$SQUASHFS_TOOLS_TAG_OBJECT", core)
        self.assertIn('"$tag_object^{}"', core)
        self.assertIn("release is not the pinned annotated tag", core)
        self.assertIn('wanted="$INCUS_RELEASE+dirty"', core)
        self.assertIn("vcs.modified=true", core)
        self.assertIn("incusd-go-packages.txt", core)
        self.assertIn("golang.org/x/crypto/openpgp", core)
        self.assertIn("dependency-overrides.json", core)
        self.assertIn("-mod=readonly", core)
        self.assertNotIn("-buildvcs=false", core)
        self.assertNotIn("go get -u", core)
        result = subprocess.run(["sh", "-n", str(ROOT / "unraid/scripts/lib/build-core.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
