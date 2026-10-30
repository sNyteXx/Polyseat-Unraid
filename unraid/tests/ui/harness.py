#!/usr/bin/env python3
"""Local UI harness (dev tool, not run by CI): renders the real .page files with PHP in Docker inside a
fake Unraid chrome (4 themes), and serves canned API responses on 127.0.0.1 only."""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EMHTTP = ROOT / "unraid" / "plugin" / "src" / "emhttp"
HERE = Path(__file__).resolve().parent

THEMES = {  # values observed in Unraid's palette files; only the variables this UI inherits
    "black": dict(bg="#1d1b1b", fg="#f2f2f2", mild="#262626", border="#404040", alt="#808080", link="#486dba", bar="#404040"),
    "white": dict(bg="#f2f2f2", fg="#1d1b1b", mild="#f7f9f9", border="#d3d3d3", alt="#909090", link="#486dba", bar="#808080"),
    "azure": dict(bg="#e8e8e8", fg="#606e7f", mild="#edeaef", border="#d3d3d3", alt="#909090", link="#486dba", bar="#606e7f"),
    "gray": dict(bg="#000000", fg="#606e7f", mild="#121510", border="#404040", alt="#606e7f", link="#486dba", bar="#404040"),
}
STATES = {
    "running": ({"state": "RUNNING", "release_gate": "closed", "version": "7.5.1-polyseat.ci", "current": "a" * 64, "previous": "b" * 64,
                 "controller": "0.1.0-wip", "data_dir": "/mnt/cache/appdata/polyseat"},
                {"load": [1.9, 1.2, 0.8], "cpus": 8, "memory": {"total_kb": 33554432, "available_kb": 9437184},
                 "daemon": {"running": True, "pid": 4242, "rss_kb": 183000, "cpu_seconds": 12.5},
                 "data_dir": {"path": "/mnt/cache/appdata/polyseat", "fstype": "zfs", "total_kb": 480000000, "free_kb": 300000000},
                 "incus": {"reachable": True, "total": 3, "running": 2}}),
    "stopped": ({"state": "ACTIVE", "release_gate": "closed", "version": "7.5.1-polyseat.ci", "current": "a" * 64, "previous": "",
                 "controller": "0.1.0-wip", "data_dir": "/mnt/cache/appdata/polyseat"},
                {"load": [0.3, 0.2, 0.2], "cpus": 8, "memory": {"total_kb": 33554432, "available_kb": 25165824},
                 "daemon": {"running": False, "pid": None, "rss_kb": None, "cpu_seconds": None},
                 "data_dir": {"path": "/mnt/cache/appdata/polyseat", "fstype": "zfs", "total_kb": 480000000, "free_kb": 460000000}, "incus": None}),
    "stress": ({"state": "JOURNAL_PENDING", "release_gate": "test-host", "version": "7.5.1-polyseat.ci", "current": "a" * 64, "previous": "b" * 64,
                "controller": "0.1.0-wip", "data_dir": "/mnt/cache/appdata/polyseat"},
               {"load": [7.8, 6.1, 5.0], "cpus": 8, "memory": {"total_kb": 33554432, "available_kb": 1572864},
                "daemon": {"running": False, "pid": None, "rss_kb": None, "cpu_seconds": None},
                "data_dir": {"path": "/mnt/cache/appdata/polyseat", "fstype": "zfs", "total_kb": 480000000, "free_kb": 20000000}, "incus": None}),
}
BUNDLES = [{"sha": "a" * 64, "version": "7.5.1-polyseat.ci", "incus_version": "7.5.1", "built_without": ["apparmor", "selinux"],
            "current": True, "previous": False, "has_archive": True, "has_signature": True},
           {"sha": "b" * 64, "version": "7.5.0-polyseat.ci", "incus_version": "7.5.0", "built_without": ["apparmor", "selinux"],
            "current": False, "previous": True, "has_archive": True, "has_signature": True}]
LOG = "\n".join(["2026-10-30T19:16:36Z [info] staged bundle aaaaaaaaaaaa", "2026-10-30T19:16:37Z [info] activated bundle",
                 "2026-10-30T19:17:02Z [warn] apparmor is active; ACCEPT_NO_LSM=yes",
                 "2026-10-30T19:17:05Z [error] health: management socket missing", "2026-10-30T19:17:09Z [info] service started and healthy"])

chrome = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>:root{{--text-color:{fg};--background-color:{bg};--mild-background-color:{mild};--border-color:{border};--alt-text-color:{alt};
--link-text-color:{link};--usage-bar-background-color:{bar};--green-500:#17bf0b;--green-800:#4f8a10;--orange-500:#ff8c2f;--orange-800:#f15a2c;
--red-500:#ff3300;--red-700:#de1100}}
body{{margin:0;background:var(--background-color);color:var(--text-color);font:13px/1.4 "Clear Sans",Arial,sans-serif}}
html{{font-size:62.5%}} body{{font-size:1.3rem}}
header{{background:var(--mild-background-color);padding:10px 16px;border-bottom:1px solid var(--border-color);font-weight:600}}
main{{padding:12px 16px}} h1.t{{font-size:1.8rem;margin:0 0 10px}}
table.dash{{width:{tile_w}px;max-width:100%;border-collapse:collapse}} table.dash td{{padding:8px 10px;border:1px solid var(--border-color);background:var(--mild-background-color)}}
.tile-header{{display:flex;justify-content:space-between;align-items:center;gap:8px}} .tile-header-left{{display:flex;gap:10px;align-items:center}}
.tile-header-main{{margin:0;font-size:1.6rem}} .f32{{font-size:3.2rem}} .control{{margin-left:6px}}
button,input,select,table{{font-family:inherit}} input[type=text],select{{height:30px}} button{{height:30px;background:#444;color:#fff;border:0}}
</style><link rel="stylesheet" href="/plugins/polyseat-unraid/css/psu.css"></head><body><header>Unraid 7.3 &mdash; {title} ({theme})</header>
<main><h1 class="t">{title}</h1>{body}</main></body></html>"""


def render_php(page: str, theme: str, workdir: Path) -> str:
    (workdir / "ctl.sh").write_text('#!/bin/sh\ncase "$1" in\n config-get) cat /h/config.json;;\n status) cat /h/status.json;;\n *) echo "{}";;\nesac\n')
    (workdir / "ctl.sh").chmod(0o755)
    cfg = subprocess.run([str(ROOT / "unraid/plugin/src/emhttp/scripts/polyseatctl"), "config-get"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "POLYSEAT_ROOT": "/tmp/polyseat-ctl-harnessnone"}).stdout
    if not cfg.strip():
        schema = json.loads((EMHTTP / "schema.json").read_text())
        cfg = json.dumps([{"key": k, "type": v["type"], "tab": v["tab"], "label": v["label"], "values": v.get("values"), "min": v.get("min"),
                           "max": v.get("max"), "locked_while_running": v.get("locked_while_running", False), "default": v["default"],
                           "value": "/mnt/cache/appdata/polyseat" if k == "DATA_DIR" else v["default"], "user_set": k == "DATA_DIR"} for k, v in schema.items()])
    (workdir / "config.json").write_text(cfg)
    (workdir / "status.json").write_text(json.dumps(STATES["running"][0]))
    result = subprocess.run(["docker", "run", "--rm", "-v", f"{EMHTTP}:/usr/local/emhttp/plugins/polyseat-unraid:ro", "-v", f"{HERE}:/r:ro",
                             "-v", f"{workdir}:/h:ro", "php:8.4-cli", "php", "/r/render.php", page, theme], capture_output=True, text=True)
    if result.returncode != 0:
        return f"<pre>{result.stderr}</pre>"
    return result.stdout


class Handler(http.server.BaseHTTPRequestHandler):
    workdir: Path
    cache: dict = {}

    def log_message(self, *args):  # quiet
        pass

    def send(self, code, ctype, body, extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send(423, "application/json", json.dumps({"ok": False, "error": "locked", "detail": "The release gate is closed: changes are disabled in this WIP build."}))

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        cookie = self.headers.get("Cookie", "")
        state = next((c.split("=")[1] for c in cookie.split("; ") if c.startswith("psu_state=")), "running")
        if url.path.startswith("/plugins/polyseat-unraid/include/api.php"):
            status, metrics = STATES.get(state, STATES["running"])
            action = q.get("action", [""])[0]
            data = {"status": status, "metrics": metrics, "list": BUNDLES if state != "empty" else [], "logs": LOG, "diag": "== diagnostics (redacted)\nstate=" + status["state"] + "\n"}.get(action)
            return self.send(200, "application/json", json.dumps({"ok": True, "data": data}))
        if url.path.startswith("/plugins/polyseat-unraid/"):
            rel = url.path[len("/plugins/polyseat-unraid/"):]
            f = (EMHTTP / rel).resolve()
            if EMHTTP.resolve() in f.parents and f.is_file():
                ctype = "text/css" if f.suffix == ".css" else "application/javascript" if f.suffix == ".js" else "image/svg+xml"
                return self.send(200, ctype, f.read_bytes())
        if url.path.startswith("/page/"):
            name = url.path.split("/")[2]
            theme = q.get("theme", ["black"])[0]
            state = q.get("state", ["running"])[0]
            files = {"tile": ("PolyseatTile.page", "Dashboard"), "tool": ("PolyseatTool.page", "Polyseat"), "settings": ("PolyseatSettings.page", "Polyseat Settings")}
            page, title = files[name]
            key = (page, theme)
            if key not in self.cache:
                self.cache[key] = render_php(page, theme, self.workdir)
            body = self.cache[key]
            if name == "tile":
                body = f'<table class="dash">{body}</table>'
            p = THEMES[theme]
            html = chrome.format(title=title, theme=theme, body=body, tile_w=q.get("w", ["440"])[0], **p)
            return self.send(200, "text/html", html, {"Set-Cookie": f"psu_state={state}; Path=/"})
        self.send(404, "text/plain", "not found")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    with tempfile.TemporaryDirectory(prefix="psu-harness-") as tmp:
        Handler.workdir = Path(tmp)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        print(f"harness on http://127.0.0.1:{port}/page/tool?theme=black", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
