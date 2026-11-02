"""Fail-closed CPU-only canary policy tests; no Incus service or host cgroups touched."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL


ROOT = {"type": "disk", "path": "/", "pool": "default"}


class CanaryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="polyseat-canary-policy-")
        self.addCleanup(self.workspace.cleanup)
        self.dir = Path(self.workspace.name)
        self.profiles = [{"name": "default", "config": {}, "devices": {"root": ROOT}}]
        self.projects = [{"name": "default", "config": {}}]
        self.server = {"config": {}, "environment": {"server_clustered": False}}
        self.networks = [{"name": "lo", "managed": False}]
        self.instances = [{
            "type": "container", "name": "polyseat-canary-cpu", "profiles": ["default"],
            "config": {"image.os": "BusyBox", "volatile.uuid": "a"},
            "expanded_config": {"image.os": "BusyBox", "volatile.uuid": "a"},
            "devices": {}, "expanded_devices": {"root": ROOT},
        }]
        self.pools = [{"name": "default", "driver": "dir",
                       "config": {"source": "/var/lib/polyseat-unraid-canary/incus/storage-pools/default"}}]

    def audit(self, *, server=None, projects=None, profiles=None, instances=None, pools=None, networks=None):
        for name, value in (
            ("server", self.server if server is None else server),
            ("projects", self.projects if projects is None else projects),
            ("profiles", self.profiles if profiles is None else profiles),
            ("instances", self.instances if instances is None else instances),
            ("pools", self.pools if pools is None else pools),
            ("networks", self.networks if networks is None else networks),
        ):
            (self.dir / f"{name}.json").write_text(json.dumps(value))
        # Source the functions, without evaluating the controller's main().
        # Override only the read-only Incus query transport: no daemon is started.
        script = r'''
          source <(sed '$d' "$1")
          POLYSEAT_RELEASE_GATE=canary
          INCUS_DIR=/var/lib/polyseat-unraid-canary/incus
          # run_incus is invoked with query URI only; the test workspace is
          # exported into a variable instead of being placed on the host.
          run_incus() {
            case "$2" in
              '/1.0') jq . "$TEST_DIR/server.json" ;;
              '/1.0/projects?recursion=1') jq . "$TEST_DIR/projects.json" ;;
              '/1.0/profiles?recursion=1') jq . "$TEST_DIR/profiles.json" ;;
              '/1.0/instances?recursion=1') jq . "$TEST_DIR/instances.json" ;;
              '/1.0/storage-pools?recursion=1') jq . "$TEST_DIR/pools.json" ;;
              '/1.0/networks?recursion=1') jq . "$TEST_DIR/networks.json" ;;
              *) return 99 ;;
            esac
          }
          canary_profile_guard
        '''
        return subprocess.run(
            ["bash", "-c", script, "canary-policy-test", str(CTL)],
            env={"PATH": "/usr/bin:/bin", "TEST_DIR": str(self.dir)},
            capture_output=True, text=True, check=False,
        )

    def test_disk_only_container_passes(self):
        result = self.audit()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_fresh_state_passes(self):
        result = self.audit(profiles=[{"name": "default", "config": {}, "devices": {}}],
                            instances=[], pools=[])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_non_root_devices_are_refused(self):
        for typ in ("nic", "gpu", "pci", "usb", "unix-char", "unix-block", "proxy", "disk"):
            with self.subTest(type=typ):
                instances = copy.deepcopy(self.instances)
                instances[0]["expanded_devices"]["extra"] = {"type": typ}
                self.assertNotEqual(self.audit(instances=instances).returncode, 0)

    def test_profile_devices_are_checked_even_without_instances(self):
        profiles = copy.deepcopy(self.profiles)
        profiles[0]["devices"]["eth0"] = {"type": "nic", "nictype": "bridged"}
        self.assertNotEqual(self.audit(profiles=profiles, instances=[]).returncode, 0)

    def test_extra_project_cannot_hide_instances_or_devices(self):
        projects = copy.deepcopy(self.projects)
        projects.append({"name": "bypass", "config": {"features.profiles": "true"}})
        self.assertNotEqual(self.audit(projects=projects).returncode, 0)
        self.assertNotEqual(self.audit(projects=[projects[1]]).returncode, 0)

    def test_project_api_must_be_accessible_and_well_formed(self):
        self.assertNotEqual(self.audit(projects=[]).returncode, 0)
        self.assertNotEqual(self.audit(projects={"unexpected": True}).returncode, 0)
        self.assertNotEqual(self.audit(projects=[{"name": "default", "config": []}]).returncode, 0)

    def test_config_kernel_modules_raw_settings_and_privileged_are_refused(self):
        for key, val in (("linux.kernel_modules", "vfio-pci"), ("raw.lxc", "lxc.mount.auto=proc"),
                         ("security.privileged", "true"), ("security.nesting", "true")):
            with self.subTest(config=key):
                instances = copy.deepcopy(self.instances)
                instances[0]["config"][key] = val
                self.assertNotEqual(self.audit(instances=instances).returncode, 0)

    def test_virtual_machine_and_unknown_container_are_refused(self):
        instances = copy.deepcopy(self.instances)
        instances[0]["type"] = "virtual-machine"
        self.assertNotEqual(self.audit(instances=instances).returncode, 0)
        instances[0]["type"] = "container"
        instances[0]["name"] = "unexpected"
        self.assertNotEqual(self.audit(instances=instances).returncode, 0)

    def test_external_pool_and_mount_device_are_refused(self):
        self.assertNotEqual(self.audit(pools=[{"name": "default", "driver": "zfs"}]).returncode, 0)
        instances = copy.deepcopy(self.instances)
        instances[0]["expanded_devices"]["root"]["source"] = "/mnt/user"
        self.assertNotEqual(self.audit(instances=instances).returncode, 0)

    def test_storage_source_cannot_point_to_host_files_or_missing_config(self):
        for source in ("/", "/mnt/user", "/var/lib/polyseat-unraid-canary/incus/storage-pools/default/../foreign", None):
            with self.subTest(source=source):
                pools = copy.deepcopy(self.pools)
                pools[0]["config"]["source"] = source
                self.assertNotEqual(self.audit(pools=pools).returncode, 0)
        pools = copy.deepcopy(self.pools)
        pools[0]["config"]["volatile.initial_source"] = "/mnt/user"
        self.assertNotEqual(self.audit(pools=pools).returncode, 0)

    def test_server_listeners_cluster_settings_and_unknown_config_are_refused(self):
        for key in ("core.https_address", "core.metrics_address", "core.bgp_address", "core.dns_address",
                    "cluster.https_address", "images.remote_cache_expiry", "future.setting"):
            with self.subTest(key=key):
                server = copy.deepcopy(self.server)
                server["config"][key] = "0.0.0.0:1234"
                self.assertNotEqual(self.audit(server=server).returncode, 0)
        self.assertNotEqual(self.audit(server={"environment": {"server_clustered": False}}).returncode, 0)
        self.assertNotEqual(self.audit(server={"config": {}, "environment": {"server_clustered": True}}).returncode, 0)

    def test_managed_network_and_project_mutation_are_refused(self):
        self.assertNotEqual(self.audit(networks=[{"name": "incusbr0", "managed": True}]).returncode, 0)
        self.assertNotEqual(self.audit(projects=[{"name": "default", "config": {"restricted": "false"}}]).returncode, 0)
        self.assertEqual(self.audit(projects=[{"name": "default", "config": {"features.images": "true"}}]).returncode, 0)

    def test_canary_explicit_unprivileged_and_no_autostart_are_allowed(self):
        instances = copy.deepcopy(self.instances)
        for key in ("boot.autostart", "security.privileged", "security.nesting"):
            instances[0]["config"][key] = "false"
            instances[0]["expanded_config"][key] = "false"
        self.assertEqual(self.audit(instances=instances).returncode, 0)
        instances[0]["config"]["boot.autostart"] = "true"
        self.assertNotEqual(self.audit(instances=instances).returncode, 0)

    def test_unknown_volatile_settings_are_not_an_escape_hatch(self):
        instances = copy.deepcopy(self.instances)
        instances[0]["config"]["volatile.future.raw"] = "unsafe"
        self.assertNotEqual(self.audit(instances=instances).returncode, 0)

    def test_malformed_query_or_missing_data_fail_closed(self):
        self.assertNotEqual(self.audit(profiles={"type": "sync", "status_code": 200, "metadata": []}).returncode, 0)
        self.assertNotEqual(self.audit(instances={"unexpected": True}).returncode, 0)
        self.assertNotEqual(self.audit(profiles=[{"name": "default", "config": {}}], pools={}).returncode, 0)

    def test_cgroup_limits_are_pinned_and_only_canary_uses_them(self):
        text = CTL.read_text()
        for contract in (
            'POLYSEAT_RELEASE_GATE="closed"',
            'CANARY_CGROUP="/sys/fs/cgroup/polyseat-unraid-canary"',
            'CANARY_CPUSET="24-27"',
            'CANARY_CPU_MAX="100000 1000000"',
            'CANARY_MEMORY_MAX=536870912',
            'CANARY_PIDS_MAX=256',
            'printf \'0\\n\' > "$CANARY_CGROUP/memory.swap.max"',
            'printf \'1\\n\' > "$CANARY_CGROUP/cgroup.kill"',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, text)


class CanaryApprovalTests(unittest.TestCase):
    """Use disposable Ed25519 keys, never any real release/approval signing key."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="polyseat-canary-approval-")
        self.addCleanup(self.workspace.cleanup)
        self.dir = Path(self.workspace.name)
        self.flash = self.dir / "boot/config/plugins/polyseat-unraid"
        self.flash.mkdir(parents=True)
        self.key = self.dir / "canary-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)],
                       check=True)
        public = self.key.with_suffix(".pub").read_text().split()
        (self.flash / "canary_allowed_signers").write_text(
            f'polyseat-unraid-canary-approver namespaces="polyseat-unraid-canary-approval-v1" '
            f'{public[0]} {public[1]}\n')
        self.archive = self.dir / "bundle.tar.gz"
        self.archive.write_bytes(b"canary-fixture")
        self.sha = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        now = int(time.time())
        self.token = {
            "schema": 1, "host_sha256": "c" * 64, "archive_sha256": self.sha,
            "issued_at": now - 10, "expires_at": now + 900,
            "actions": ["stage", "activate", "start", "config-set"],
        }

    def sign(self, *, use_namespace="polyseat-unraid-canary-approval-v1"):
        dest = self.flash / "canary-approval.json"
        dest.write_text(json.dumps(self.token, separators=(",", ":")))
        # ssh-keygen asks interactively before replacing .sig on SSH PTYs.
        Path(str(dest) + ".sig").unlink(missing_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-n", use_namespace,
                        "-f", str(self.key), str(dest)], check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)
        os.chmod(dest, 0o600)
        os.chmod(Path(str(dest) + ".sig"), 0o600)

    def check(self, command="activate", *args):
        shell = r'''
          source <(sed '$d' "$1")
          POLYSEAT_ROOT="$2"
          POLYSEAT_RELEASE_GATE=canary
          init_paths
          SCHEMA="$(dirname -- "$1")/../schema.json"   # init_paths cannot find it from a sourced copy
          canary_host_sha256() { printf '%s\n' 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'; }
          canary_approval_for "${@:3}"
        '''
        return subprocess.run(
            ["bash", "-c", shell, "canary-approval-test", str(CTL), str(self.dir),
             command, *(str(x) for x in args)],
            env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False,
        )

    def test_signed_time_limited_artifact_bound_approval_passes(self):
        self.sign()
        self.assertEqual(self.check("stage", self.archive).returncode, 0)
        self.assertEqual(self.check("activate", self.sha).returncode, 0)

    def test_signer_is_independent_and_namespace_specific(self):
        self.sign(use_namespace="polyseat-unraid-bundle")
        self.assertEqual(self.check("activate", self.sha).returncode, 2)

    def test_missing_or_corrupted_signature_blocks(self):
        self.assertEqual(self.check("activate", self.sha).returncode, 2)
        self.sign()
        (self.flash / "canary-approval.json").write_text("{}")
        self.assertEqual(self.check("activate", self.sha).returncode, 2)

    def test_wrong_host_or_bundle_blocks_even_with_valid_signature(self):
        self.token["host_sha256"] = "0" * 64
        self.sign()
        self.assertEqual(self.check("activate", self.sha).returncode, 2)
        self.token["host_sha256"] = "c" * 64
        self.sign()
        self.assertEqual(self.check("activate", "e" * 64).returncode, 2)

    def test_expired_future_dated_and_oversized_windows_block(self):
        now = int(time.time())
        for issued, expires in ((now - 1000, now - 10), (now + 10, now + 100),
                                (now - 30, now + 1800)):
            with self.subTest(issued=issued, expires=expires):
                self.token["issued_at"] = issued
                self.token["expires_at"] = expires
                self.sign()
                self.assertEqual(self.check("activate", self.sha).returncode, 2)

    def test_unapproved_commands_and_update_are_refused(self):
        self.sign()
        self.assertEqual(self.check("restart").returncode, 2)
        self.assertEqual(self.check("update", self.archive).returncode, 2)
        self.assertEqual(self.check("config-set", "SERVICE=enabled").returncode, 2)
        self.assertEqual(self.check("config-set", "DATA_DIR=/mnt/user/bad").returncode, 2)
        self.assertEqual(self.check("config-set", "DATA_DIR=/mnt/cache/appdata/polyseat-canary").returncode, 2)
        self.assertEqual(self.check("config-set", "DATA_DIR=/var/lib/polyseat-unraid-canary").returncode, 0)

    def test_only_the_owners_play_settings_may_be_added_and_only_with_valid_values(self):
        self.sign()
        for setting in ("PLAY_CPU_PERCENT=0", "PLAY_CPU_PERCENT=50", "PLAY_MEMORY_MB=8192",
                        "PLAY_IDLE_STOP_MINUTES=0", "PLAY_IDLE_STOP_MINUTES=30", "LIBRARY_PATH=/mnt/download/polyseat-games"):
            with self.subTest(setting=setting):
                self.assertEqual(self.check("config-set", setting).returncode, 0)
        self.assertEqual(self.check("config-set", "SERVICE=disabled", "PLAY_CPU_PERCENT=25").returncode, 0)
        for setting in ("PLAY_CPU_PERCENT=101", "PLAY_CPU_PERCENT=-1", "PLAY_CPU_PERCENT=", "PLAY_CPU_PERCENT=1.5",
                        "PLAY_MEMORY_MB=4194305", "PLAY_MEMORY_MB=abc",
                        "PLAY_IDLE_STOP_MINUTES=1441", "PLAY_IDLE_STOP_MINUTES=", "PLAY_IDLE_STOP_MINUTES=1.5", "LIBRARY_PATH=/etc", "LIBRARY_PATH=/mnt/../etc",
                        "LIBRARY_PATH=/mnt/a/", "PLAY_PIDS_MAX=1", "PLAY_CPU_MAX=max", "PLAY_MEMORY_MAX=max"):
            with self.subTest(setting=setting):
                self.assertEqual(self.check("config-set", setting).returncode, 2)
        self.assertEqual(self.check("config-set", "PLAY_CPU_PERCENT=25", "HEALTH_TIMEOUT=300").returncode, 2)

    def test_restart_and_boot_cannot_be_authorized(self):
        for action in ("restart", "boot", "update", "rollback"):
            with self.subTest(action=action):
                self.token["actions"] = [action]
                self.sign()
                self.assertEqual(self.check(action).returncode, 2)

    def test_configuration_cannot_weaken_limits_or_mapping(self):
        self.sign()
        for setting in ("HEALTH_TIMEOUT=300", "SUBID_START=0", "SUBID_COUNT=4294967295"):
            self.assertEqual(self.check("config-set", setting).returncode, 2)

    def test_canary_smoke_action_is_signed_and_fixed_bundle_bound(self):
        self.token["actions"] = ["canary-smoke"]
        self.sign()
        state = self.flash / "state"
        state.mkdir()
        pointer = state / "pointer.json"
        pointer.write_text(json.dumps({"current": self.sha, "previous": None}))
        pointer.chmod(0o600)
        self.assertEqual(self.check("canary-smoke").returncode, 0)
        pointer.write_text(json.dumps({"current": "f" * 64, "previous": None}))
        self.assertEqual(self.check("canary-smoke").returncode, 2)


if __name__ == "__main__":
    unittest.main()
