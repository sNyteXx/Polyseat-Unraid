"""polyseatctl behaviour tests inside throwaway /tmp/polyseat-ctl-* sandboxes.

Stub daemons only. Nothing here touches a real host, Incus, Docker or a GPU.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL, Sandbox

BAD_ENV = {"POLYSEAT_RELEASE_GATE": "open"}  # must be ignored: only the script constant counts


def file_entry(path, data: bytes, mode=0o755):
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode}


def member(name, data=b"x", mode=0o755, uid=0, gid=0, kind=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name)
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.mode = mode
    info.uid, info.gid = uid, gid
    info.type = kind
    info.linkname = linkname
    return info, (data if kind == tarfile.REGTYPE else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def assertOk(self, result):
        self.assertEqual(result.code, 0, result.text)


class GateTests(Base):
    def test_real_host_mutation_is_refused_while_gate_is_closed(self):
        for command in (["activate", "a" * 64, "--approve"], ["start"], ["stop"], ["uninstall", "--approve"],
                        ["boot"], ["restart"], ["rollback", "--approve"]):
            result = self.sb.ctl(*command, root=False, env=BAD_ENV)
            self.assertEqual(result.code, 2, (command, result.text))
            self.assertIn("gate is closed", result.text)

    def test_sandbox_root_must_be_a_private_tmp_directory(self):
        result = self.sb.ctl("start", env={"POLYSEAT_ROOT": "/tmp/other-root"}, root=False)
        self.assertEqual(result.code, 2)
        link = Path("/tmp") / f"polyseat-ctl-link{os.getpid()}"
        link.symlink_to(self.sb.root)
        self.addCleanup(link.unlink)
        self.assertEqual(self.sb.ctl("start", env={"POLYSEAT_ROOT": str(link)}, root=False).code, 2)
        os.chmod(self.sb.root, 0o755)
        self.assertEqual(self.sb.ctl("start").code, 2)
        os.chmod(self.sb.root, 0o700)

    def test_install_actions_need_explicit_approval(self):
        b = self.sb.make_bundle("1.0.0")
        for command in (["stage", b["archive"], b["manifest"], b["sig"]], ["activate", b["sha"]],
                        ["rollback"], ["recover"], ["uninstall"]):
            result = self.sb.ctl(*command)
            self.assertEqual(result.code, 2, (command, result.text))
            self.assertIn("--approve", result.text)
        self.assertEqual(self.sb.bundle_files(), [])

    def test_read_only_commands_work_on_a_blank_system_without_writing(self):
        self.assertEqual(self.sb.status()["state"], "ABSENT")
        self.assertEqual(self.sb.ctl("version").code, 0)
        self.assertEqual(self.sb.ctl("verify").code, 0)
        self.assertFalse((self.sb.flash / "bundles").exists())
        self.assertFalse((self.sb.root / "var").exists())

    def test_script_contains_no_recursive_or_glob_deletion(self):
        text = CTL.read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"\brm\s+(-[a-zA-Z]*[rR]|--recursive)", code))
        self.assertNotIn("-delete", code)
        self.assertNotIn("rmtree", code)
        self.assertIsNone(re.search(r"find\b[^\n]*-exec\s+rm", code))
        self.assertIn('POLYSEAT_RELEASE_GATE="closed"', text)


class InstallLifecycleTests(Base):
    def test_stage_activate_start_stop_roundtrip(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.install(b))
        self.assertEqual(self.sb.pointer(), {"current": b["sha"], "previous": None})
        self.assertEqual(self.sb.current_link(), f"slots/{b['sha']}")
        self.assertEqual(self.sb.status()["state"], "ACTIVE")
        self.assertEqual(self.sb.ctl("verify").out.strip(), f"OK {b['sha']}")
        self.assertEqual(sorted(self.sb.bundle_files()), sorted([
            f"{b['sha']}.manifest.json", f"{b['sha']}.manifest.json.sig", f"{b['sha']}.tar.gz"]))
        self.assertOk(self.sb.ctl("start"))
        self.assertEqual(self.sb.status()["state"], "RUNNING")
        self.assertOk(self.sb.ctl("health"))
        self.assertOk(self.sb.ctl("stop"))
        self.assertEqual(self.sb.status()["state"], "ACTIVE")
        self.assertIn("start 1.0.0", self.sb.events())
        self.assertIn("stop 1.0.0", self.sb.events())

    def test_start_creates_only_the_incus_directory_and_managed_subid_block(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        (self.sb.root / "etc/subuid").write_text("alice:100000:65536\n")
        (self.sb.root / "etc/subgid").write_text("alice:100000:65536\n")
        before = sorted(p.name for p in self.sb.data.iterdir())
        self.assertOk(self.sb.ctl("start"))
        self.assertEqual(sorted(p.name for p in self.sb.data.iterdir()), sorted(before + ["incus"]))
        text = (self.sb.root / "etc/subuid").read_text()
        self.assertTrue(text.startswith("alice:100000:65536\n"))
        self.assertIn("root:1000000:1000000000", text)
        self.assertEqual(oct((self.sb.data / "incus").stat().st_mode & 0o777), "0o711")

    def test_service_restart_keeps_state_and_uses_daemon_signal(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.assertOk(self.sb.ctl("start"))
        first = (self.sb.run_dir / "incusd.pid").read_text()
        self.assertOk(self.sb.ctl("restart"))
        self.assertNotEqual(first, (self.sb.run_dir / "incusd.pid").read_text())
        self.assertEqual(self.sb.events(), ["start 1.0.0", "stop 1.0.0", "start 1.0.0"])
        self.assertNotIn("shutdown-requested", " ".join(self.sb.events()))

    def test_boot_rebuilds_ram_state_from_flash_and_honours_service_setting(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.install(b))
        shutil.rmtree(self.sb.ram)           # a reboot empties the RAM root
        shutil.rmtree(self.sb.run_dir)
        self.assertOk(self.sb.ctl("boot"))
        self.assertEqual(self.sb.status()["state"], "ACTIVE")  # SERVICE=disabled: no start
        self.assertEqual(self.sb.events(), [])
        shutil.rmtree(self.sb.ram)
        self.sb.write_cfg(SERVICE="enabled")
        self.assertOk(self.sb.ctl("boot"))
        self.assertEqual(self.sb.status()["state"], "RUNNING")
        self.assertEqual(self.sb.events(), ["start 1.0.0"])

    def test_repeat_stage_and_activate_are_idempotent(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.install(b))
        self.assertOk(self.sb.stage(b))
        self.assertOk(self.sb.ctl("activate", b["sha"], "--approve"))
        self.assertEqual(self.sb.pointer(), {"current": b["sha"], "previous": None})

    def test_update_swaps_binaries_and_keeps_previous_for_rollback(self):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.ctl("start"))
        self.assertOk(self.sb.ctl("update", v2["archive"], v2["manifest"], v2["sig"], "--approve"))
        self.assertEqual(self.sb.pointer(), {"current": v2["sha"], "previous": v1["sha"]})
        self.assertEqual(self.sb.events(), ["start 1.0.0", "stop 1.0.0", "start 1.1.0"])
        self.assertEqual(sorted(self.sb.slot_dirs()), sorted([v1["sha"], v2["sha"]]))
        self.assertEqual(self.sb.status()["version"], "1.1.0")
        self.assertFalse((self.sb.state / "journal.json").exists())

    def test_third_update_prunes_the_oldest_bundle_exactly(self):
        bundles = [self.sb.make_bundle(v) for v in ("1.0.0", "1.1.0", "1.2.0")]
        self.assertOk(self.sb.install(bundles[0]))
        for b in bundles[1:]:
            self.assertOk(self.sb.install(b))
        self.assertEqual(sorted(self.sb.slot_dirs()), sorted(b["sha"] for b in bundles[1:]))
        self.assertNotIn(bundles[0]["sha"] + ".tar.gz", self.sb.bundle_files())
        self.assertEqual(len(self.sb.bundle_files()), 6)

    def test_manual_rollback_returns_to_previous_version(self):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.ctl("start"))
        self.assertOk(self.sb.install(v2))
        self.assertOk(self.sb.ctl("rollback", "--approve"))
        self.assertEqual(self.sb.pointer(), {"current": v1["sha"], "previous": None})
        self.assertEqual(self.sb.status()["state"], "RUNNING")
        self.assertEqual(self.sb.events()[-2:], ["stop 1.1.0", "start 1.0.0"])
        self.assertEqual(self.sb.slot_dirs(), [v1["sha"]])
        again = self.sb.ctl("rollback", "--approve")
        self.assertEqual(again.code, 2, again.text)


class LsmTests(Base):
    """A host LSM the bundled liblxc cannot use must never be silently skipped."""

    def fake_lsm(self, *parts):
        path = self.sb.root.joinpath("sys", *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if parts[-1] == "enforce":
            path.write_text("1")
        else:
            path.mkdir()

    def test_no_lsm_on_host_starts_and_tells_incus_apparmor_is_off(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.assertOk(self.sb.ctl("start"))
        self.assertEqual(self.sb.env_log(), ["apparmor=false selinux=None ld=None"])

    def test_active_apparmor_blocks_start_without_explicit_acceptance(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.fake_lsm("kernel", "security", "apparmor")
        result = self.sb.ctl("start")
        self.assertNotEqual(result.code, 0)
        self.assertIn("apparmor is active", result.text)
        self.assertIn("refusing to start containers", result.text)
        self.assertEqual(self.sb.events(), [])
        self.assertEqual(self.sb.status()["state"], "ACTIVE")

    def test_active_selinux_blocks_start_too(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.fake_lsm("fs", "selinux", "enforce")
        self.assertNotEqual(self.sb.ctl("start").code, 0)
        self.assertEqual(self.sb.events(), [])

    def test_explicit_acceptance_is_logged_and_allows_start(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.fake_lsm("kernel", "security", "apparmor")
        self.sb.write_cfg(ACCEPT_NO_LSM="yes")
        result = self.sb.ctl("start")
        self.assertOk(result)
        self.assertIn("WITHOUT apparmor confinement", self.sb.log_file.read_text())
        self.assertEqual(self.sb.env_log(), ["apparmor=false selinux=None ld=None"])

    def test_acceptance_value_must_be_exactly_yes(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.fake_lsm("kernel", "security", "apparmor")
        for value in ("true", "YES", "1", "yes please"):
            self.sb.write_cfg(ACCEPT_NO_LSM=value)
            self.assertNotEqual(self.sb.ctl("start").code, 0, value)

    def test_bundle_that_supports_the_lsm_is_not_blocked(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0", built_without=[])))
        self.fake_lsm("kernel", "security", "apparmor")
        self.assertOk(self.sb.ctl("start"))

    def test_manifest_without_or_with_unknown_built_without_is_rejected(self):
        d = b"data"
        good = self.sb.craft_bundle([member("payload/bin/incus", d)], [file_entry("payload/bin/incus", d)])
        manifest = json.loads(good["manifest"].read_text())
        for value in ("MISSING", ["apparmor", "apparmor"], ["tomoyo"], "apparmor", ["apparmor", "selinux", "x"]):
            broken = dict(manifest)
            if value == "MISSING":
                broken.pop("built_without")
            else:
                broken["built_without"] = value
            good["manifest"].write_text(json.dumps(broken))
            self.sb.sign(good["manifest"])
            self.assertEqual(self.sb.stage(good).code, 5, value)

    def test_preflight_reports_active_lsm(self):
        self.fake_lsm("kernel", "security", "apparmor")
        result = self.sb.ctl("preflight")
        self.assertIn("AppArmor is active", result.out)


class SettingsAndViewsTests(Base):
    def cfg(self):
        return json.loads(self.sb.ctl("config-get").out)

    def value(self, key):
        return {e["key"]: e["value"] for e in self.cfg()}[key]

    def test_config_get_reports_schema_defaults_and_user_values(self):
        self.sb.write_cfg(SERVICE="enabled", HEALTH_TIMEOUT=None, DATA_DIR=None)
        entries = {e["key"]: e for e in self.cfg()}
        self.assertEqual(entries["SERVICE"]["value"], "enabled")
        self.assertTrue(entries["SERVICE"]["user_set"])
        self.assertEqual(entries["HEALTH_TIMEOUT"]["value"], "60")
        self.assertFalse(entries["HEALTH_TIMEOUT"]["user_set"])
        self.assertEqual(entries["DATA_DIR"]["value"], "")

    def test_config_set_validates_writes_atomically_and_keeps_a_backup(self):
        self.assertOk(self.sb.ctl("config-set", "SERVICE=enabled", "HEALTH_TIMEOUT=45"))
        self.assertEqual(self.value("SERVICE"), "enabled")
        self.assertEqual(self.value("HEALTH_TIMEOUT"), "45")
        self.assertEqual(self.value("DATA_DIR"), DATA_LOGICAL_FOR_TESTS)
        cfg = self.sb.flash / "polyseat-unraid.cfg"
        self.assertEqual(oct(cfg.stat().st_mode & 0o777), "0o600")
        self.assertTrue((self.sb.flash / "polyseat-unraid.cfg.bak").exists())
        self.assertNotIn("ACCEPT_NO_LSM", cfg.read_text())     # untouched keys stay unset

    def test_config_set_rejects_bad_input_without_changing_anything(self):
        before = (self.sb.flash / "polyseat-unraid.cfg").read_text()
        for pair in ("SERVICE=maybe", "HEALTH_TIMEOUT=5", "HEALTH_TIMEOUT=99999", "HEALTH_TIMEOUT=1e3",
                     "UNKNOWN=1", "SUBID_COUNT=100", 'SERVICE=enabled"\nDATA_DIR="/mnt/x',
                     "DATA_DIR=/etc", "DATA_DIR=/mnt/user/x", "DATA_DIR=/mnt/../etc", "ACCEPT_NO_LSM=YES",
                     "lowercase=1", "SERVICE", "=x"):
            result = self.sb.ctl("config-set", pair)
            self.assertNotEqual(result.code, 0, pair)
        self.assertEqual((self.sb.flash / "polyseat-unraid.cfg").read_text(), before)

    def test_data_dir_must_exist_and_subid_range_must_fit(self):
        self.assertNotEqual(self.sb.ctl("config-set", "DATA_DIR=/mnt/cache/nope").code, 0)
        result = self.sb.ctl("config-set", "SUBID_START=4000000000", "SUBID_COUNT=1000000000")
        self.assertEqual(result.code, 1, result.text)

    def test_identity_settings_are_locked_while_the_service_runs(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.assertOk(self.sb.ctl("start"))
        for pair in ("SUBID_START=2000000", "DATA_DIR=/mnt/cache/appdata"):
            result = self.sb.ctl("config-set", pair)
            self.assertEqual(result.code, 2, pair)
        self.assertOk(self.sb.ctl("config-set", "HEALTH_TIMEOUT=30"))   # harmless keys stay editable
        self.assertEqual(self.sb.ctl("config-reset").code, 2)

    def test_config_reset_restores_defaults_and_keeps_backup(self):
        self.assertOk(self.sb.ctl("config-set", "SERVICE=enabled"))
        self.assertOk(self.sb.ctl("config-reset"))
        self.assertEqual(self.value("SERVICE"), "disabled")
        self.assertEqual(self.value("DATA_DIR"), "")
        self.assertIn("SERVICE", (self.sb.flash / "polyseat-unraid.cfg.bak").read_text())

    def test_settings_changes_are_refused_on_a_real_host_with_closed_gate(self):
        for command in (["config-set", "SERVICE=enabled"], ["config-reset"]):
            result = self.sb.ctl(*command, root=False)
            self.assertEqual(result.code, 2, command)
            self.assertIn("gate is closed", result.text)

    def test_list_shows_staged_bundles_and_pointer_roles(self):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertEqual(json.loads(self.sb.ctl("list").out), [])
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.install(v2))
        listing = {e["sha"]: e for e in json.loads(self.sb.ctl("list").out)}
        self.assertEqual(set(listing), {v1["sha"], v2["sha"]})
        self.assertTrue(listing[v2["sha"]]["current"] and not listing[v2["sha"]]["previous"])
        self.assertTrue(listing[v1["sha"]]["previous"])
        self.assertEqual(listing[v2["sha"]]["version"], "1.1.0")
        self.assertEqual(listing[v2["sha"]]["built_without"], ["apparmor", "selinux"])

    def test_metrics_without_and_with_a_running_daemon(self):
        idle = json.loads(self.sb.ctl("metrics").out)
        self.assertFalse(idle["daemon"]["running"])
        self.assertIsNone(idle["incus"])
        self.assertGreater(idle["memory"]["total_kb"], 0)
        self.assertEqual(len(idle["load"]), 3)
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.assertOk(self.sb.ctl("start"))
        busy = json.loads(self.sb.ctl("metrics").out)
        self.assertTrue(busy["daemon"]["running"])
        self.assertGreater(busy["daemon"]["rss_kb"], 0)
        self.assertEqual(busy["incus"], {"reachable": True, "total": 2, "running": 1})
        self.assertEqual(busy["data_dir"]["path"], "/mnt/cache/appdata/polyseat")
        self.assertGreater(busy["data_dir"]["free_kb"], 0)

    def test_logs_are_bounded_redacted_and_read_only(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        with self.sb.log_file.open("a") as log:
            log.write("2026 [info] token TOKEN=abc123 end\n")
        out = self.sb.ctl("logs", "controller", "5").out
        self.assertNotIn("abc123", out)
        self.assertLessEqual(len(out.splitlines()), 5)
        for bad in (["logs", "controller", "0"], ["logs", "controller", "501"], ["logs", "../etc/passwd"],
                    ["logs", "controller", "5;id"]):
            self.assertNotEqual(self.sb.ctl(*bad).code, 0, bad)


DATA_LOGICAL_FOR_TESTS = "/mnt/cache/appdata/polyseat"


class CiRealSystemTests(Base):
    def test_real_system_addressing_is_ignored_outside_github_runners(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        env = {"POLYSEAT_CI_REAL_SYSTEM": "1", "CI": "true", "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}
        self.assertOk(self.sb.ctl("start", env=env))
        self.assertIn("root:1000000", (self.sb.root / "etc/subuid").read_text())
        self.assertTrue((self.sb.root / "var/lib/polyseat-lxc/rootfs").is_dir())
        self.assertEqual(self.sb.ctl("start", env={"POLYSEAT_CI_REAL_SYSTEM": "1"}, root=False).code, 2)


class LxcRootfsMountTests(Base):
    def test_start_creates_only_the_plugin_owned_mount_point_and_uninstall_removes_it(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        parent = self.sb.root / "var/lib/polyseat-lxc"
        self.assertFalse(parent.exists())
        self.assertOk(self.sb.ctl("start"))
        self.assertEqual(sorted(p.name for p in parent.iterdir()), ["rootfs"])
        self.assertEqual(oct((parent / "rootfs").stat().st_mode & 0o777), "0o755")
        self.assertOk(self.sb.ctl("uninstall", "--approve"))
        self.assertFalse(parent.exists())

    def test_uninstall_keeps_foreign_content_in_the_parent(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.assertOk(self.sb.ctl("start"))
        keep = self.sb.root / "var/lib/polyseat-lxc/other"
        keep.write_text("not ours")
        self.assertOk(self.sb.ctl("uninstall", "--approve"))
        self.assertTrue(keep.exists())
        self.assertFalse((self.sb.root / "var/lib/polyseat-lxc/rootfs").exists())

    def test_symlinked_state_directory_is_refused(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        (self.sb.root / "var/lib").mkdir(parents=True)
        (self.sb.root / "var/lib/polyseat-lxc").symlink_to(self.sb.work)
        result = self.sb.ctl("start")
        self.assertNotEqual(result.code, 0)
        self.assertIn("symlinked", result.text)
        self.assertEqual(list(self.sb.work.glob("rootfs")), [])

    def test_manifest_must_declare_exactly_the_fixed_mount_point(self):
        d = b"data"
        good = self.sb.craft_bundle([member("payload/bin/incus", d)], [file_entry("payload/bin/incus", d)])
        manifest = json.loads(good["manifest"].read_text())
        for value in ("MISSING", "/etc", "/var/lib/polyseat-lxc/../etc", "/var/lib/polyseat-lxc/rootfs/", "rootfs", ""):
            broken = dict(manifest)
            if value == "MISSING":
                broken.pop("lxc_rootfs_mount")
            else:
                broken["lxc_rootfs_mount"] = value
            good["manifest"].write_text(json.dumps(broken))
            self.sb.sign(good["manifest"])
            self.assertEqual(self.sb.stage(good).code, 5, value)


class BootOrderingTests(Base):
    def test_plugin_reinstall_early_in_boot_never_needs_the_pool(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.install(b))
        shutil.rmtree(self.sb.ram)                      # fresh boot: RAM empty, pool not mounted
        shutil.rmtree(self.sb.data)
        self.sb.write_cfg(SERVICE="enabled")
        self.assertOk(self.sb.ctl("recover", "--approve"))
        self.assertOk(self.sb.ctl("activate", b["sha"], "--approve"))   # same bundle: no-op
        self.assertEqual(self.sb.events(), [])

    def test_update_of_a_stopped_but_enabled_service_does_not_start_it(self):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertOk(self.sb.install(v1))
        self.sb.write_cfg(SERVICE="enabled")
        shutil.rmtree(self.sb.data)                      # pool not available yet
        self.assertOk(self.sb.install(v2))
        self.assertEqual(self.sb.pointer()["current"], v2["sha"])
        self.assertEqual(self.sb.events(), [])


class FailureTests(Base):
    def test_failed_start_of_new_bundle_rolls_back_automatically(self):
        v1, bad = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("2.0.0", fail_start=True)
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.ctl("start"))
        self.assertOk(self.sb.stage(bad))
        result = self.sb.ctl("activate", bad["sha"], "--approve")
        self.assertEqual(result.code, 4, result.text)
        self.assertEqual(self.sb.pointer(), {"current": v1["sha"], "previous": None})
        self.assertEqual(self.sb.current_link(), f"slots/{v1['sha']}")
        self.assertEqual(self.sb.status()["state"], "RUNNING")
        self.assertFalse((self.sb.state / "journal.json").exists())
        self.assertEqual(self.sb.events()[-1], "start 1.0.0")

    def test_health_failure_on_open_listener_rolls_back(self):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.ctl("start"))
        (self.sb.data / "incus" / "HTTPS_ADDR").write_text(":8443\n")
        self.assertOk(self.sb.stage(v2))
        result = self.sb.ctl("activate", v2["sha"], "--approve")
        self.assertEqual(result.code, 4, result.text)
        self.assertEqual(self.sb.pointer()["current"], v1["sha"])

    def test_corrupted_archive_is_rejected_before_anything_is_written(self):
        b = self.sb.make_bundle("1.0.0")
        data = bytearray(b["archive"].read_bytes())
        data[len(data) // 2] ^= 0xFF
        bad = b["archive"].with_name("copy.tar.gz")
        bad.write_bytes(bytes(data))
        result = self.sb.ctl("stage", bad, b["manifest"], b["sig"], "--approve")
        self.assertEqual(result.code, 5, result.text)
        self.assertEqual(self.sb.bundle_files(), [])
        self.assertEqual(self.sb.slot_dirs(), [])

    def test_flash_bitrot_after_staging_blocks_activation(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.stage(b))
        path = self.sb.bundles / f"{b['sha']}.tar.gz"
        path.write_bytes(path.read_bytes()[:-8] + b"corrupt!")
        result = self.sb.ctl("activate", b["sha"], "--approve")
        self.assertEqual(result.code, 5, result.text)
        self.assertEqual(self.sb.slot_dirs(), [])
        self.assertFalse((self.sb.state / "pointer.json").exists())

    def test_tampered_manifest_and_untrusted_signer_are_rejected(self):
        b = self.sb.make_bundle("1.0.0")
        tampered = b["manifest"].with_name("tampered.json")
        manifest = json.loads(b["manifest"].read_text())
        manifest["version"] = "9.9.9"
        tampered.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        shutil.copy(b["sig"], str(tampered) + ".sig")
        self.assertEqual(self.sb.ctl("stage", b["archive"], tampered, str(tampered) + ".sig", "--approve").code, 5)
        other = self.sb.make_key("attacker")
        evil = self.sb.make_bundle("1.0.1", sign_key=other)
        result = self.sb.stage(evil)
        self.assertEqual(result.code, 5, result.text)
        self.assertEqual(self.sb.bundle_files(), [])

    def test_missing_signature_and_missing_allowed_signers_fail_closed(self):
        b = self.sb.make_bundle("1.0.0")
        empty = self.sb.work / "empty.sig"
        empty.write_text("")
        self.assertEqual(self.sb.ctl("stage", b["archive"], b["manifest"], empty, "--approve").code, 5)
        (self.sb.flash / "allowed_signers").unlink()
        self.assertEqual(self.sb.stage(b).code, 5)

    def test_missing_host_library_blocks_staging(self):
        b = self.sb.make_bundle("1.0.0", needed_libs=["libc.so.6", "libdoesnotexist.so.9"])
        result = self.sb.stage(b)
        self.assertEqual(result.code, 5, result.text)
        self.assertIn("libdoesnotexist.so.9", result.text)
        self.assertEqual(self.sb.bundle_files(), [])

    def test_too_old_host_requirements_block_staging(self):
        b = self.sb.make_bundle("1.0.0")
        (self.sb.root / "etc/unraid-version").write_text('version="7.1.0"\n')
        self.assertEqual(self.sb.stage(b).code, 5)

    def test_broken_binary_is_caught_before_the_live_system_changes(self):
        v1, bad = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("2.0.0", fail_smoke=True)
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.ctl("start"))
        self.assertOk(self.sb.stage(bad))
        result = self.sb.ctl("activate", bad["sha"], "--approve")
        self.assertEqual(result.code, 5, result.text)
        self.assertEqual(self.sb.current_link(), f"slots/{v1['sha']}")
        self.assertEqual(self.sb.status()["state"], "RUNNING")
        self.assertEqual(self.sb.slot_dirs(), [v1["sha"]])
        self.assertEqual(self.sb.events(), ["start 1.0.0"])

    def test_insufficient_flash_space_blocks_staging(self):
        b = self.sb.make_bundle("1.0.0")
        result = self.sb.ctl("stage", b["archive"], b["manifest"], b["sig"], "--approve",
                             env={"POLYSEAT_FAKE_FREE_KB": "100"})
        self.assertEqual(result.code, 6, result.text)
        self.assertEqual(self.sb.bundle_files(), [])

    def test_insufficient_ram_space_blocks_activation_without_side_effects(self):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.stage(v2))
        result = self.sb.ctl("activate", v2["sha"], "--approve", env={"POLYSEAT_FAKE_FREE_KB": "100"})
        self.assertEqual(result.code, 6, result.text)
        self.assertEqual(self.sb.slot_dirs(), [v1["sha"]])
        self.assertFalse((self.sb.state / "journal.json").exists())
        self.assertEqual(self.sb.pointer()["current"], v1["sha"])

    def test_foreign_incusd_blocks_start(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        fake = self.sb.work / "incusd"
        fake.write_text("#!/bin/bash\nsleep 30\n")
        fake.chmod(0o755)
        process = subprocess.Popen([str(fake)], start_new_session=True)

        def reap():
            os.killpg(process.pid, 9)
            process.wait()

        self.addCleanup(reap)
        time.sleep(0.2)
        result = self.sb.ctl("start")
        self.assertNotEqual(result.code, 0)
        self.assertIn("another incusd", result.text)
        self.assertEqual(self.sb.events(), [])

    def test_subid_conflicts_and_modified_blocks_are_refused(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        (self.sb.root / "etc/subuid").write_text("bob:1500000:65536\n")
        result = self.sb.ctl("start")
        self.assertNotEqual(result.code, 0)
        self.assertIn("overlaps", result.text)
        self.assertEqual((self.sb.root / "etc/subuid").read_text(), "bob:1500000:65536\n")

    def test_invalid_data_dir_choices_are_refused(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        for value in (None, "", "/mnt/user/polyseat", "/boot/polyseat", "/mnt/cache/../etc", "relative/path",
                      "/mnt/cache/does-not-exist"):
            self.sb.write_cfg(DATA_DIR=value)
            result = self.sb.ctl("start")
            self.assertNotEqual(result.code, 0, value)
        link = self.sb.root / "mnt/cache/appdata/linked"
        link.symlink_to(self.sb.data)
        self.sb.write_cfg(DATA_DIR="/mnt/cache/appdata/linked")
        self.assertNotEqual(self.sb.ctl("start").code, 0)
        self.sb.data.chmod(0o750)
        self.sb.write_cfg()
        result = self.sb.ctl("start")
        self.assertNotEqual(result.code, 0)
        self.assertIn("traversable", result.text)
        self.assertEqual(self.sb.events(), [])


class HostileArchiveTests(Base):
    def reject(self, members, files):
        b = self.sb.craft_bundle(members, files)
        result = self.sb.stage(b)
        self.assertEqual(result.code, 5, result.text)
        self.assertEqual(self.sb.bundle_files(), [])
        self.assertEqual(self.sb.slot_dirs(), [])

    def test_valid_craft_baseline_is_accepted(self):
        data = b"#!/bin/sh\n"
        b = self.sb.craft_bundle([member("payload/bin/incus", data)], [file_entry("payload/bin/incus", data)])
        self.assertOk(self.sb.stage(b))

    def test_symlink_member(self):
        self.reject([member("payload/bin/incus", kind=tarfile.SYMTYPE, linkname="/etc/passwd")],
                    [file_entry("payload/bin/incus", b"")])

    def test_hardlink_member(self):
        self.reject([member("payload/bin/incus", kind=tarfile.LNKTYPE, linkname="payload/x")],
                    [file_entry("payload/bin/incus", b"")])

    def test_directory_member(self):
        d = b"data"
        self.reject([member("payload/bin", kind=tarfile.DIRTYPE), member("payload/bin/incus", d)],
                    [file_entry("payload/bin/incus", d)])

    def test_path_traversal_and_absolute_names(self):
        d = b"data"
        for name in ("payload/../../etc/cron.d/x", "/etc/passwd", "payload//bin/x", "payload/.hidden"):
            self.reject([member(name, d)], [file_entry("payload/bin/incus", d)])

    def test_setuid_and_odd_modes(self):
        d = b"data"
        self.reject([member("payload/bin/incus", d, mode=0o4755)], [file_entry("payload/bin/incus", d)])
        self.reject([member("payload/bin/incus", d, mode=0o777)], [file_entry("payload/bin/incus", d)])

    def test_foreign_owner(self):
        d = b"data"
        self.reject([member("payload/bin/incus", d, uid=1000, gid=1000)], [file_entry("payload/bin/incus", d)])

    def test_undeclared_extra_member(self):
        d = b"data"
        self.reject([member("payload/bin/incus", d), member("payload/bin/extra", d)],
                    [file_entry("payload/bin/incus", d)])

    def test_missing_declared_member(self):
        d = b"data"
        self.reject([member("payload/bin/incus", d)],
                    [file_entry("payload/bin/incus", d), file_entry("payload/bin/incusd", d)])

    def test_size_and_checksum_mismatch(self):
        d = b"data"
        wrong_size = file_entry("payload/bin/incus", d)
        wrong_size["size"] = 5
        self.reject([member("payload/bin/incus", d)], [wrong_size])
        wrong_hash = file_entry("payload/bin/incus", b"other")
        wrong_hash["size"] = len(d)
        self.reject([member("payload/bin/incus", d)], [wrong_hash])

    def test_duplicate_members(self):
        d = b"data"
        self.reject([member("payload/bin/incus", d), member("payload/bin/incus", d)],
                    [file_entry("payload/bin/incus", d)])

    def test_malformed_manifest_fields(self):
        d = b"data"
        b = self.sb.craft_bundle([member("payload/bin/incus", d)], [file_entry("payload/bin/incus", d)])
        manifest = json.loads(b["manifest"].read_text())
        for field, value in (("needed_libs", ["../x"]), ("min_glibc", "2;rm"), ("schema", 2),
                             ("files", [{"path": "payload/x"}])):
            broken = dict(manifest)
            broken[field] = value
            b["manifest"].write_text(json.dumps(broken))
            self.sb.sign(b["manifest"])
            self.assertEqual(self.sb.stage(b).code, 5, field)


class InterruptionTests(Base):
    def prepare(self, running=True):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertOk(self.sb.install(v1))
        if running:
            self.assertOk(self.sb.ctl("start"))
        self.assertOk(self.sb.stage(v2))
        return v1, v2

    def crash(self, point, v2):
        result = self.sb.ctl("activate", v2["sha"], "--approve", env={"POLYSEAT_CRASH_AT": point})
        self.assertEqual(result.code, 99, result.text)

    def test_crash_at_each_activation_step_is_recoverable(self):
        for point in ("after_journal", "after_switch", "after_pointer", "after_start"):
            with self.subTest(point=point):
                self.tearDown_cleanup()
                self.setUp()
                v1, v2 = self.prepare()
                self.crash(point, v2)
                self.assertEqual(self.sb.status()["state"], "JOURNAL_PENDING")
                blocked = self.sb.ctl("activate", v2["sha"], "--approve")
                self.assertEqual(blocked.code, 7, blocked.text)
                self.assertEqual(self.sb.ctl("start").code, 7)
                self.assertOk(self.sb.ctl("recover", "--approve"))
                self.assertEqual(self.sb.pointer(), {"current": v1["sha"], "previous": None})
                self.assertEqual(self.sb.current_link(), f"slots/{v1['sha']}")
                self.assertEqual(self.sb.status()["state"], "RUNNING")
                self.assertFalse((self.sb.state / "journal.json").exists())
                self.assertEqual(self.sb.events()[-1], "start 1.0.0")
                self.assertOk(self.sb.ctl("activate", v2["sha"], "--approve"))
                self.assertEqual(self.sb.pointer()["current"], v2["sha"])

    def tearDown_cleanup(self):
        self.sb.cleanup()

    def test_crash_after_extraction_leaves_a_stale_slot_that_is_cleaned_exactly(self):
        v1, v2 = self.prepare(running=False)
        self.crash("after_extract", v2)
        self.assertIn(f".incoming-{v2['sha']}", self.sb.slot_dirs())
        self.assertOk(self.sb.ctl("activate", v2["sha"], "--approve"))
        self.assertNotIn(f".incoming-{v2['sha']}", self.sb.slot_dirs())
        self.assertEqual(self.sb.pointer()["current"], v2["sha"])

    def test_boot_recovers_an_interrupted_update_automatically(self):
        v1, v2 = self.prepare(running=False)
        self.sb.write_cfg(SERVICE="enabled")
        self.crash("after_switch", v2)
        shutil.rmtree(self.sb.ram)
        self.assertOk(self.sb.ctl("boot"))
        self.assertEqual(self.sb.pointer()["current"], v1["sha"])
        self.assertEqual(self.sb.status()["state"], "RUNNING")

    def test_interrupted_rollback_can_be_completed_by_recovery(self):
        v1, v2 = self.prepare(running=False)
        self.assertOk(self.sb.ctl("activate", v2["sha"], "--approve"))
        result = self.sb.ctl("rollback", "--approve", env={"POLYSEAT_CRASH_AT": "after_journal"})
        self.assertEqual(result.code, 99)
        self.assertOk(self.sb.ctl("recover", "--approve"))
        self.assertEqual(self.sb.pointer(), {"current": v1["sha"], "previous": None})

    def test_crash_in_first_install_recovers_to_absent(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.stage(b))
        result = self.sb.ctl("activate", b["sha"], "--approve", env={"POLYSEAT_CRASH_AT": "after_switch"})
        self.assertEqual(result.code, 99)
        self.assertOk(self.sb.ctl("recover", "--approve"))
        self.assertEqual(self.sb.status()["state"], "ABSENT")
        self.assertFalse((self.sb.ram / "current").exists())


class CorruptStateTests(Base):
    def installed(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.install(b))
        return b

    def test_garbage_and_unknown_pointer_fields(self):
        self.installed()
        pointer = self.sb.state / "pointer.json"
        for content in ("not json", '{"current":"zz","previous":null}', '{"current":null,"previous":null,"x":1}', "[]"):
            pointer.write_text(content)
            pointer.chmod(0o600)
            self.assertEqual(self.sb.ctl("status").code, 3, content)
            self.assertEqual(self.sb.ctl("activate", "a" * 64, "--approve").code, 3, content)

    def test_wrong_permissions_and_symlinked_state_files(self):
        b = self.installed()
        pointer = self.sb.state / "pointer.json"
        pointer.chmod(0o644)
        self.assertEqual(self.sb.ctl("status").code, 3)
        pointer.chmod(0o600)
        real = self.sb.work / "real-pointer.json"
        shutil.copy(pointer, real)
        pointer.unlink()
        pointer.symlink_to(real)
        self.assertEqual(self.sb.ctl("status").code, 3)
        pointer.unlink()
        shutil.copy(real, pointer)
        pointer.chmod(0o600)
        self.assertEqual(self.sb.status()["current"], b["sha"])

    def test_unknown_journal_phase_blocks_everything(self):
        self.installed()
        journal = self.sb.state / "journal.json"
        journal.write_text('{"phase":"wat","previous":null,"target":null}')
        journal.chmod(0o600)
        self.assertEqual(self.sb.ctl("activate", "a" * 64, "--approve").code, 3)
        self.assertEqual(self.sb.ctl("recover", "--approve").code, 3)
        self.assertEqual(self.sb.ctl("start").code, 3)

    def test_tampered_slot_is_rebuilt_from_flash_not_trusted(self):
        b = self.installed()
        victim = self.sb.ram / "slots" / b["sha"] / "payload/lib/libstub.so.1"
        victim.write_bytes(b"evil")
        shutil.rmtree(self.sb.run_dir, ignore_errors=True)
        self.assertOk(self.sb.ctl("boot"))
        self.assertEqual(victim.read_bytes(), b"stub-1.0.0")

    def test_slot_with_undeclared_file_is_not_deleted_recursively(self):
        b = self.installed()
        stray = self.sb.ram / "slots" / b["sha"] / "payload/bin/stray"
        stray.write_text("keep me")
        result = self.sb.ctl("boot")
        self.assertNotEqual(result.code, 0, result.text)
        self.assertEqual(stray.read_text(), "keep me")


class UninstallTests(Base):
    def test_uninstall_preserves_user_data_and_settings_and_is_idempotent(self):
        v1, v2 = self.sb.make_bundle("1.0.0"), self.sb.make_bundle("1.1.0")
        self.assertOk(self.sb.install(v1))
        self.assertOk(self.sb.install(v2))
        (self.sb.root / "etc/subuid").write_text("alice:100000:65536\n")
        (self.sb.root / "etc/subgid").write_text("alice:100000:65536\n")
        self.assertOk(self.sb.ctl("start"))
        (self.sb.data / "seats").mkdir()
        (self.sb.data / "seats" / "seat1.cfg").write_text("seat")
        (self.sb.data / "games").mkdir()
        (self.sb.data / "games" / "save.dat").write_bytes(os.urandom(64))
        subuid_before = "alice:100000:65536\n"
        user_before = {name: self.sb.tree_fingerprint(self.sb.data / name) for name in ("seats", "games")}
        cfg_before = (self.sb.flash / "polyseat-unraid.cfg").read_text()
        self.assertOk(self.sb.ctl("uninstall", "--approve"))
        self.assertTrue((self.sb.data / "seats" / "seat1.cfg").exists())
        self.assertTrue((self.sb.data / "games" / "save.dat").exists())
        self.assertEqual((self.sb.flash / "polyseat-unraid.cfg").read_text(), cfg_before)
        self.assertEqual((self.sb.root / "etc/subuid").read_text(), subuid_before)
        self.assertEqual((self.sb.root / "etc/subgid").read_text(), subuid_before)
        self.assertEqual(self.sb.status()["state"], "ABSENT")
        self.assertEqual(self.sb.slot_dirs(), [])
        self.assertEqual(self.sb.bundle_files(), [])
        self.assertFalse((self.sb.run_dir / "incusd.pid").exists())
        user_after = {name: self.sb.tree_fingerprint(self.sb.data / name) for name in ("seats", "games")}
        self.assertEqual(user_before, user_after)
        self.assertEqual(sorted(p.name for p in self.sb.data.iterdir()), ["games", "incus", "seats"])
        self.assertOk(self.sb.ctl("uninstall", "--approve"))

    def test_uninstall_leaves_undeclared_files_in_place(self):
        b = self.sb.make_bundle("1.0.0")
        self.assertOk(self.sb.install(b))
        stray = self.sb.ram / "slots" / b["sha"] / "payload/bin/stray"
        stray.write_text("keep me")
        self.assertOk(self.sb.ctl("uninstall", "--approve"))
        self.assertEqual(stray.read_text(), "keep me")
        self.assertFalse((self.sb.ram / "slots" / b["sha"] / "payload/bin/incus").exists())

    def test_uninstall_refuses_to_remove_a_modified_subid_block(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.assertOk(self.sb.ctl("start"))
        path = self.sb.root / "etc/subuid"
        text = path.read_text().replace("root:1000000:1000000000", "root:1000000:1000000000\nroot:2000000:1")
        path.write_text(text)
        self.assertOk(self.sb.ctl("uninstall", "--approve"))
        self.assertEqual(path.read_text(), text)


class DiagnosticsTests(Base):
    def test_diagnose_redacts_secrets_and_reports_state(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        with self.sb.log_file.open("a") as log:
            log.write("2026 [info] pairing PASSWORD=hunter2 TOKEN: s3cret\n")
        result = self.sb.ctl("diagnose")
        self.assertOk(result)
        self.assertNotIn("hunter2", result.out)
        self.assertNotIn("s3cret", result.out)
        self.assertIn("<redacted>", result.out)
        self.assertIn("state=ACTIVE", result.out)

    def test_log_is_written_below_the_plugin_log_directory_only(self):
        self.assertOk(self.sb.install(self.sb.make_bundle("1.0.0")))
        self.assertIn("activated bundle", self.sb.log_file.read_text())
        self.assertEqual(sorted(p.name for p in (self.sb.root / "var").iterdir()), ["log", "run"])


if __name__ == "__main__":
    unittest.main()
