"""WebUI tests: PHP library/handler (php or docker), JS helpers (node), controller/PHP cross-checks."""

from __future__ import annotations

import json
import os
import re
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
                      "PLAY_IDLE_STOP_MINUTES": (["0", "1", "1440", "030"], ["1441", "-1", "", "30m", "1e1", " 5"]),
                      "LIBRARY_PATH": (["", "/mnt/download/polyseat-games", "/mnt/disk1/a.b-c_d"],
                                       ["/etc", "/mnt/", "/mnt/a/../b", "/mnt/a/", "/mnt//a", "/mnt/a b"]),
                      "PLAY_SESSION_HOURS": (["1", "6", "24", "06"], ["0", "25", "", "1.5", "6h", " 6"]),
                      "PLAY_CPU_PERFORMANCE": (["yes", "no"], ["true", "YES", "", "1"]),
                      # shape only here; the host's CPU count is the controller's own check (test_owner_mode)
                      "PLAY_SEAT_CPUS": (["", "0-15", "0,2,4-7", "seat1=0-7;seat2=8-15"],
                                         ["0-", "-3", "seat1=", "Seat1=0-7", "seat1=0-7;", "0-7;seat1=1", "12345", "$(id)"]),
                      # the permanent workloads daemon (owner builds)
                      "WORKLOADS_ENABLED": (["yes", "no"], ["true", "YES", "", "1"]),
                      "WORKLOADS_INSTANCES": (["", "ubuntudev", "ubuntudev ubuntudev-test"],
                                              ["Ubuntudev", "a  b", " a", "a ", "a;b", "-a", "a b c d e f g h i j k l m n o p q"]),
                      "WORKLOADS_MEMORY_MB": (["0", "28672", "4194304"], ["4194305", "-1", "", "28G", "1e3"]),
                      "WORKLOADS_CPUS": (["", "0-15", "0-7,16-19"], ["0-", "0-7;1", "seat1=0-7", "12345", "$(id)"])})
        for key, (good, bad) in cases.items():
            if key not in ("PLAY_CPU_PERCENT", "PLAY_MEMORY_MB", "PLAY_IDLE_STOP_MINUTES", "LIBRARY_PATH", "PLAY_SESSION_HOURS",
                           "PLAY_CPU_PERFORMANCE", "PLAY_SEAT_CPUS", "WORKLOADS_ENABLED", "WORKLOADS_INSTANCES",
                           "WORKLOADS_MEMORY_MB", "WORKLOADS_CPUS"):
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
            self.assertIn(rule["type"], ("enum", "int", "path", "string"))
            self.assertIn("default", rule)
            self.assertIn(rule["tab"], ("general", "storage", "advanced", "play", "workloads"))
            if rule["type"] == "enum":
                self.assertIn(rule["default"], rule["values"])
            if rule["type"] == "int":
                self.assertTrue(rule["min"] <= int(rule["default"]) <= rule["max"])
            if rule["type"] in ("path", "string"):
                self.assertNotIn("~", rule["pattern"])
                self.assertTrue(rule["default"] == "" or re.search(rule["pattern"], rule["default"]), key)


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
               "restart": "RESTART", "config_reset": "RESET", "play_stop": "STOP", "workloads_stop": "STOP WORKLOADS"}
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          const a = ['activate','rollback','recover','stop','restart','config_reset','play_stop','workloads_stop'];
          console.log(JSON.stringify(Object.fromEntries(a.map(x => [x, PSU.confirmPhrase(x, {json.dumps(sha)})]))));""")
        self.assertEqual(out, php)

    def test_setting_validation_mirrors_schema(self):
        schema = json.loads((EMHTTP / "schema.json").read_text())
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          const s = {json.dumps(schema)};
          const t = (k, v) => PSU.validateSetting(s[k], v);
          console.log(JSON.stringify([t('SERVICE','enabled'), t('SERVICE','x'), t('HEALTH_TIMEOUT','60'), t('HEALTH_TIMEOUT','9'),
            t('DATA_DIR','/mnt/cache/a'), t('DATA_DIR','/etc'), t('DATA_DIR',''), t('NOPE','1'),
            t('PLAY_CPU_PERCENT','0'), t('PLAY_CPU_PERCENT','100'), t('PLAY_CPU_PERCENT','101'), t('PLAY_CPU_PERCENT','5.5'),
            t('PLAY_MEMORY_MB','4194304'), t('PLAY_MEMORY_MB','4194305'), t('PLAY_MEMORY_MB',''),
            t('PLAY_IDLE_STOP_MINUTES','0'), t('PLAY_IDLE_STOP_MINUTES','1440'), t('PLAY_IDLE_STOP_MINUTES','1441'),
            t('PLAY_IDLE_STOP_MINUTES','5.5'),
            t('LIBRARY_PATH','/mnt/download/polyseat-games'), t('LIBRARY_PATH','/etc'), t('LIBRARY_PATH',''),
            t('PLAY_SESSION_HOURS','24'), t('PLAY_SESSION_HOURS','25'), t('PLAY_CPU_PERFORMANCE','yes'), t('PLAY_CPU_PERFORMANCE','true'),
            t('PLAY_SEAT_CPUS',''), t('PLAY_SEAT_CPUS','seat1=0-7;seat2=8-15'), t('PLAY_SEAT_CPUS','seat1='), t('PLAY_SEAT_CPUS','0-15\\n'),
            t('WORKLOADS_ENABLED','yes'), t('WORKLOADS_ENABLED','true'), t('WORKLOADS_INSTANCES','ubuntudev ubuntudev-test'),
            t('WORKLOADS_INSTANCES','Bad'), t('WORKLOADS_INSTANCES',''), t('WORKLOADS_MEMORY_MB','28672'), t('WORKLOADS_MEMORY_MB','4194305'),
            t('WORKLOADS_CPUS','0-7,16-19'), t('WORKLOADS_CPUS','0-')]));""")
        self.assertEqual(out, [True, False, True, False, True, False, True, False,
                               True, True, False, False, True, False, False, True, True, False, False, True, False, True,
                               True, False, True, False, True, True, False, False,
                               True, False, True, False, True, True, False, True, False])

    def test_play_card_helpers(self):
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          const up = {{ok: true, now: 1000, data: {{url: 'https://192.168.178.24:47800', alive: true, expires_at: 1000 + 3 * 3600 + 120,
                      seats: [{{name: 'seat1', status: 'Running'}}, {{name: 'seat2', status: 'Stopped'}}]}}}};
          console.log(JSON.stringify({{
            url: ['https://192.168.178.24:47800', 'javascript:alert(1)', 'http://192.168.1.1:47800', 'https://evil.example:47800',
                  'https://192.168.1.1:47800/x', 'https://192.168.1.1:47800" onclick="x', null].map(PSU.playUrl),
            dur: [PSU.fmtDuration(0), PSU.fmtDuration(-5), PSU.fmtDuration(30), PSU.fmtDuration(600), PSU.fmtDuration(3 * 3600 + 120), PSU.fmtDuration(null)],
            up: PSU.playView(up),
            down: PSU.playView({{ok: true, now: 1, data: {{alive: false, url: 'https://192.168.1.1:47800', seats: [], expires_at: null}}}}),
            none: PSU.playView({{ok: true, data: null}}),
            bad: PSU.playView({{ok: false, error: 'unavailable'}}), nothing: PSU.playView(undefined),
            evil: PSU.playView({{ok: true, now: 1, data: {{alive: true, url: 'javascript:alert(1)', seats: null}}}}),
            empty: PSU.playView({{ok: true, now: 1, data: {{alive: true, url: 'https://10.0.0.1:47800', seats: []}}}}).seats
          }}));""")
        self.assertEqual(out["url"], ["https://192.168.178.24:47800", "", "", "", "", "", ""])
        self.assertEqual(out["dur"], ["ended", "ended", "under 1 min", "10 min", "3 h 2 min", "-"])
        self.assertEqual(out["up"], {"text": "Play session running", "cls": "ok", "url": "https://192.168.178.24:47800",
                                     "seats": "2: seat1 (Running), seat2 (Stopped)", "remaining": "3 h 2 min"})
        self.assertEqual((out["down"]["cls"], out["down"]["url"]), ("crit", ""))     # nothing to open while it is down
        self.assertEqual((out["none"]["text"], out["none"]["cls"]), ("No play session", "muted"))
        self.assertEqual((out["bad"]["cls"], out["nothing"]["cls"]), ("warn", "warn"))
        self.assertEqual((out["evil"]["url"], out["evil"]["seats"]), ("", "-"))
        self.assertEqual(out["empty"], "none yet")

    def test_play_buttons_follow_mode_gate_and_session(self):
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          const st = (mode, state, gate) => ({{release_gate: gate || 'canary', approval_mode: mode, state: state}});
          const idle = {{ok: true, data: null}}, up = {{ok: true, data: {{alive: true}}}};
          console.log(JSON.stringify([
            PSU.playButtons(st('owner', 'ACTIVE'), idle), PSU.playButtons(st('owner', 'ABSENT'), idle),
            PSU.playButtons(st('owner', 'ACTIVE'), up), PSU.playButtons(st('owner', 'RUNNING'), idle),
            PSU.playButtons(st('signed', 'ACTIVE'), idle), PSU.playButtons(st('signed', 'ACTIVE'), up),
            PSU.playButtons(st(undefined, 'ACTIVE'), idle), PSU.playButtons(st('owner', 'ACTIVE', 'closed'), idle),
            PSU.playButtons(st('owner', 'JOURNAL_PENDING'), idle), PSU.playButtons(st('owner', 'CORRUPT'), idle),
            PSU.playButtons(st('owner', 'ACTIVE'), {{ok: false}}), PSU.playButtons(null, idle), PSU.playButtons(st('owner', 'ACTIVE'), undefined)
          ]));""")
        on, off = {"start": True, "stop": False}, {"start": False, "stop": False}
        self.assertEqual(out, [on, on, {"start": False, "stop": True}, off,        # owner: idle / no bundle / running / Incus up
                               off, {"start": False, "stop": True},                 # signed: never starts, can stop
                               off, off, off, off, off, off, off])

    def test_workloads_card_helpers(self):
        out = self.run_node(f"""
          const PSU = require({json.dumps(str(JS))});
          const up = {{ok: true, data: {{state: 'running', enabled: true, policy_ok: true, violations: [], note: '',
                      instances: [{{name: 'ubuntudev', status: 'Running', ips: ['192.168.178.144'], autostart: true}}]}}}};
          const st = (mode, gate) => ({{release_gate: gate || 'canary', approval_mode: mode}});
          const at = (state) => ({{ok: true, data: {{state: state}}}});
          console.log(JSON.stringify({{
            up: PSU.workloadsView(up),
            violation: PSU.workloadsView({{ok: true, data: {{state: 'running', policy_ok: false, violations: ['instance x: device usb']}}}}),
            states: ['stopped', 'starting', 'running', 'degraded', 'stopping', 'failed', 'orphaned', 'nonsense'].map(x => PSU.workloadsView(at(x)).cls),
            absent: PSU.workloadsView({{ok: true, data: null}}), bad: PSU.workloadsView({{ok: false}}), nothing: PSU.workloadsView(undefined),
            buttons: ['stopped', 'failed', 'starting', 'running', 'degraded', 'stopping', 'orphaned'].map(x => PSU.workloadsButtons(st('owner'), at(x))),
            other: [PSU.workloadsButtons(st('signed'), at('stopped')), PSU.workloadsButtons(st(undefined), at('running')),
                    PSU.workloadsButtons(st('owner', 'closed'), at('stopped')), PSU.workloadsButtons(st('owner'), {{ok: true, data: null}}),
                    PSU.workloadsButtons(st('owner'), {{ok: false}}), PSU.workloadsButtons(null, at('stopped')), PSU.workloadsButtons(st('owner'), undefined)]
          }}));""")
        self.assertEqual((out["up"]["text"], out["up"]["cls"], out["up"]["available"]), ("Running", "ok", True))
        self.assertEqual(out["up"]["instances"][0]["name"], "ubuntudev")
        self.assertEqual((out["violation"]["text"], out["violation"]["cls"]), ("Running, outside the policy", "crit"))
        self.assertEqual(out["states"], ["muted", "warn", "ok", "warn", "warn", "crit", "crit", "muted"])
        self.assertEqual((out["absent"]["available"], out["bad"]["cls"], out["nothing"]["cls"]), (False, "warn", "warn"))
        on_start, on_stop, off = {"start": True, "stop": False}, {"start": False, "stop": True}, {"start": False, "stop": False}
        # start only from a stopped (or failed: the controller says why) daemon; stop while anything of it may run
        self.assertEqual(out["buttons"], [on_start, on_start, on_stop, on_stop, on_stop, off, on_stop])
        self.assertEqual(out["other"], [off] * 7)

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
