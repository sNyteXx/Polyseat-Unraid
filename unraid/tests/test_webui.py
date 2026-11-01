"""WebUI tests: PHP library/handler (php or docker), JS helpers (node), controller/PHP cross-checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL, Sandbox

ROOT = Path(__file__).resolve().parents[2]
UNRAID = ROOT / "unraid"
PHP_RUNNER = UNRAID / "tests" / "php" / "run.php"
STUB = UNRAID / "tests" / "php" / "stubctl.sh"
JS = UNRAID / "plugin" / "src" / "emhttp" / "js" / "psu.js"
EMHTTP = UNRAID / "plugin" / "src" / "emhttp"


def php_command(workdir: Path) -> list[str] | None:
    """Prefer a local php; fall back to the official image. CI has php installed."""
    if shutil.which("php"):
        return ["php"]
    if os.environ.get("CI") == "true":
        raise RuntimeError("php is required in CI")
    if shutil.which("docker"):
        return ["docker", "run", "--rm", "-v", f"{ROOT}:{ROOT}:ro", "-v", f"{workdir}:{workdir}", "-e",
                f"PSU_TEST_DIR={workdir}", "-w", str(ROOT), "php:8.4-cli", "php"]
    return None


class PhpLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="polyseat-webui-test-")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        os.chmod(self.dir, 0o777)
        shutil.copy(STUB, self.dir / "stubctl")
        os.chmod(self.dir / "stubctl", 0o755)

    def test_php_library_suite(self):
        command = php_command(self.dir)
        if command is None:
            self.skipTest("neither php nor docker available")
        env = dict(os.environ, PSU_TEST_DIR=str(self.dir))
        result = subprocess.run([*command, str(PHP_RUNNER)], capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(result.stdout.count("\nok ") + result.stdout.startswith("ok "), 14, result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_every_php_file_lints(self):
        command = php_command(self.dir)
        if command is None:
            self.skipTest("neither php nor docker available")
        files = [EMHTTP / "include" / "lib.php", EMHTTP / "include" / "api.php"]
        for page in EMHTTP.glob("*.page"):
            body = page.read_text().split("\n---\n", 1)
            if len(body) == 2 and "<?" in body[1]:
                converted = self.dir / (page.stem + ".php")
                converted.write_text(body[1])
                files.append(converted)
        for path in files:
            args = [*command, "-l", str(path)]
            result = subprocess.run(args, capture_output=True, text=True, timeout=120,
                                    env=dict(os.environ, PSU_TEST_DIR=str(self.dir)))
            self.assertEqual(result.returncode, 0, f"{path.name}: {result.stdout}{result.stderr}")


class SchemaConsistencyTests(unittest.TestCase):
    """The controller and the PHP/JS layers read the same schema.json; spot-check they agree."""

    def test_controller_and_schema_accept_the_same_values(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        schema = json.loads((EMHTTP / "schema.json").read_text())
        cases = {"SERVICE": (["enabled", "disabled"], ["", "Enabled", "yes"]),
                 "ACCEPT_NO_LSM": (["yes", "no"], ["true", "YES"]),
                 "HEALTH_TIMEOUT": (["10", "300", "60"], ["9", "301", "1e2", "-1", "", " 5"]),
                 "SUBID_COUNT": (["65536", "1000000000"], ["65535", "2000000001", "abc"])}
        for key, (good, bad) in cases.items():
            for value in good:
                self.assertEqual(sb.ctl("config-set", f"{key}={value}").code, 0, (key, value))
            for value in bad:
                self.assertNotEqual(sb.ctl("config-set", f"{key}={value}").code, 0, (key, value))
        # Play settings: the same bounds as the PHP and JS validators (run.php, test_setting_validation_mirrors_schema).
        cases.update({"PLAY_CPU_PERCENT": (["0", "1", "100", "007"], ["101", "-1", "", "5.5", "1e1", " 5"]),
                      "PLAY_MEMORY_MB": (["0", "1024", "4194304"], ["4194305", "-1", "", "8G", "99999999999"]),
                      "LIBRARY_PATH": (["", "/mnt/download/polyseat-games", "/mnt/disk1/a.b-c_d"],
                                       ["/etc", "/mnt/", "/mnt/a/../b", "/mnt/a/", "/mnt//a", "/mnt/a b"])})
        for key, (good, bad) in cases.items():
            if key not in ("PLAY_CPU_PERCENT", "PLAY_MEMORY_MB", "LIBRARY_PATH"):
                continue
            for value in good:
                self.assertEqual(sb.ctl("config-set", f"{key}={value}").code, 0, (key, value))
            for value in bad:
                self.assertNotEqual(sb.ctl("config-set", f"{key}={value}").code, 0, (key, value))
        self.assertEqual(set(cases), {k for k in schema if k != "DATA_DIR" and k != "SUBID_START"} | set())

    def test_schema_is_well_formed(self):
        schema = json.loads((EMHTTP / "schema.json").read_text())
        for key, rule in schema.items():
            self.assertRegex(key, r"^[A-Z_]+$")
            self.assertIn(rule["type"], ("enum", "int", "path"))
            self.assertIn("default", rule)
            self.assertIn(rule["tab"], ("general", "storage", "advanced", "play"))
            if rule["type"] == "enum":
                self.assertIn(rule["default"], rule["values"])
            if rule["type"] == "int":
                self.assertTrue(rule["min"] <= int(rule["default"]) <= rule["max"])
            if rule["type"] == "path":
                self.assertNotIn("~", rule["pattern"])


@unittest.skipUnless(shutil.which("node"), "node not available")
class JsTests(unittest.TestCase):
    def run_node(self, script: str):
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_pure_helpers(self):
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          console.log(JSON.stringify({{
            kb: [PSU.fmtKb(512), PSU.fmtKb(2048), PSU.fmtKb(5*1024*1024), PSU.fmtKb(null)],
            pct: [PSU.pct(50, 200), PSU.pct(5, 0), PSU.pct(300, 100), PSU.pct(null, 10)],
            level: [PSU.level(10), PSU.level(75), PSU.level(95)],
            state: ['RUNNING','ACTIVE','ABSENT','JOURNAL_PENDING','CORRUPT','x'].map(s => PSU.stateView({{state: s}}).cls),
            gate: ['closed','test-host','open'].map(g => PSU.gateView(g).cls),
            phrase: [PSU.confirmPhrase('activate', 'abcdef0123456789'), PSU.confirmPhrase('stop'), PSU.confirmPhrase('start')],
            log: [PSU.classifyLogLine('x [error] y'), PSU.classifyLogLine('[warn] y'), PSU.classifyLogLine('fine')],
            err: PSU.errorText({{error: 'locked'}}), unknown: PSU.errorText({{error: 'zzz'}})
          }}));""")
        self.assertEqual(out["kb"], ["512 KB", "2.0 MB", "5.0 GB", "-"])
        self.assertEqual(out["pct"], [25, 0, 100, 0])
        self.assertEqual(out["level"], ["", "warn", "crit"])
        self.assertEqual(out["state"], ["ok", "muted", "muted", "crit", "crit", "crit"])
        self.assertEqual(out["gate"], ["warn", "warn", "ok"])
        self.assertEqual(out["phrase"], ["ACTIVATE abcdef01", "STOP", ""])
        self.assertEqual(out["log"], ["l-error", "l-warn", ""])
        self.assertIn("release gate", out["err"])
        self.assertEqual(out["unknown"], "The action failed.")

    def test_confirmation_phrases_match_php(self):
        sha = "0123456789abcdef" * 4
        php = {"activate": f"ACTIVATE {sha[:8]}", "rollback": "ROLLBACK", "recover": "RECOVER", "stop": "STOP",
               "restart": "RESTART", "config_reset": "RESET"}
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          const a = ['activate','rollback','recover','stop','restart','config_reset'];
          console.log(JSON.stringify(Object.fromEntries(a.map(x => [x, PSU.confirmPhrase(x, {json.dumps(sha)})]))));""")
        self.assertEqual(out, php)

    def test_setting_validation_mirrors_schema(self):
        schema = json.loads((EMHTTP / "schema.json").read_text())
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          const s = {json.dumps(schema)};
          const t = (k, v) => PSU.validateSetting(s[k], v);
          console.log(JSON.stringify([t('SERVICE','enabled'), t('SERVICE','x'), t('HEALTH_TIMEOUT','60'), t('HEALTH_TIMEOUT','9'),
            t('DATA_DIR','/mnt/cache/a'), t('DATA_DIR','/etc'), t('DATA_DIR',''), t('NOPE','1')]));""")
        self.assertEqual(out, [True, False, True, False, True, False, True, False])

    def test_no_dynamic_html_sinks_or_external_requests(self):
        import re
        text = re.sub(r"/\*.*?\*/", "", JS.read_text(), flags=re.S)
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(sink, text)
        self.assertIsNone(re.search(r"https?://", text))
        css = re.sub(r"/\*.*?\*/", "", (EMHTTP / "css" / "psu.css").read_text(), flags=re.S)
        self.assertNotIn("!important", css)
        self.assertIsNone(re.search(r"url\(\s*['\"]?https?:", css))
        self.assertNotIn("@import", css)


class PagesAndControllerIntegrationTests(unittest.TestCase):
    def test_pages_declare_expected_menus_and_never_post_inline(self):
        headers = {}
        for page in EMHTTP.glob("*.page"):
            head = page.read_text().split("\n---\n", 1)[0]
            headers[page.name] = dict(line.split("=", 1) for line in head.splitlines() if "=" in line)
        self.assertEqual(headers["PolyseatSettings.page"]["Menu"], '"Utilities"')
        self.assertEqual(headers["PolyseatTile.page"]["Menu"], '"Dashboard:0"')
        self.assertEqual(headers["Polyseat.page"]["Menu"], '"Tools"')
        self.assertEqual(headers["PolyseatTool.page"]["Menu"], '"Polyseat"')
        for page in EMHTTP.glob("*.page"):
            body = page.read_text()
            for forbidden in ("$_POST", "$_GET", "$_REQUEST", "shell_exec", "exec(", "system(", "passthru", "eval("):
                self.assertNotIn(forbidden, body, f"{page.name}: {forbidden}")
        self.assertIn("is_executable", headers["PolyseatTile.page"]["Cond"])
        self.assertIn("6.11.9", headers["PolyseatTile.page"]["Cond"])

    def test_php_sources_have_no_shell_execution(self):
        for path in (EMHTTP / "include").glob("*.php"):
            text = path.read_text()
            for forbidden in ("shell_exec", "passthru", "popen(", "system(", "`", "eval(", "assert("):
                self.assertNotIn(forbidden, text, f"{path.name}: {forbidden}")
        lib = (EMHTTP / "include" / "lib.php").read_text()
        self.assertEqual(lib.count("@proc_open("), 1)
        self.assertIn("psu_ctl_args_ok($args)", lib)

    def test_real_controller_serves_the_json_the_ui_expects(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        status = json.loads(sb.ctl("status", "--json").out)
        for key in ("state", "release_gate", "version", "current", "previous", "controller", "data_dir"):
            self.assertIn(key, status)
        metrics = json.loads(sb.ctl("metrics").out)
        for key in ("load", "cpus", "memory", "daemon", "data_dir", "incus"):
            self.assertIn(key, metrics)
        config = json.loads(sb.ctl("config-get").out)
        for entry in config:
            for key in ("key", "type", "tab", "title", "label", "default", "value", "user_set", "locked_while_running"):
                self.assertIn(key, entry)
        self.assertEqual(json.loads(sb.ctl("list").out), [])


if __name__ == "__main__":
    unittest.main()
