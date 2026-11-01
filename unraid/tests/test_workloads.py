"""The permanent workloads Incus (owner builds): policy, command allowlist, workspace record, modules, cgroup, stop
sequence, the supervisor, and its coexistence with the play session's daemon.

Sandbox directories under /tmp only; no Incus daemon, cgroup, module or host path is touched. The Incus API is a set of
JSON files behind a stub `run_incus` that still goes through the controller's own command allowlist.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from unraid.tests.ctl_support import CTL, Sandbox
from unraid.tests.test_desktop_canary import source_ctl

MODULES = ["nft_chain_nat", "nft_masq", "nft_nat", "nft_ct", "nft_compat", "overlay"]
SHA = "a" * 64

# Every test starts here: a sandbox root, the workloads instance entered, a cgroup root that is a plain directory.
HEAD = r'''
D=$1; shift
POLYSEAT_RELEASE_GATE=canary POLYSEAT_APPROVAL_MODE=owner
POLYSEAT_ROOT=$D; init_paths
SCHEMA=@SCHEMA@ CTL_SELF=@CTL@  # a sourced script cannot find its neighbours by its own name
WL_CGROUP_ROOT=$D/cg WL_CGROUP=$D/cg/polyseat-unraid-workloads WL_SYS_MODULE=$D/sysmodule
wl_enter
mkdir -p "$RUN_DIR" "$LOG_DIR"; LOG_ENABLED=1
me=$(id -u) mg=$(id -g)
stat() { local out; out=$(command stat "$@") || return; out=${out//$me $mg/0 0}; printf '%s\n' "${out//$me /0 }"; }  # the controller runs as root, the tests do not
sleep() { command sleep 0.02; }
'''

# The API stub: the files in $D/api answer the queries; every command goes through the controller's own allowlist first.
API = r'''
run_incus() {
  canary_command_allowed "$@" || { echo "DENIED $*" >> "$D/calls"; return 1; }
  echo "$*" >> "$D/calls"
  case "$*" in
    'query /1.0') cat "$D/api/server.json" ;;
    'query /1.0/projects?recursion=1') cat "$D/api/projects.json" ;;
    'query /1.0/profiles?recursion=1') cat "$D/api/profiles.json" ;;
    'query /1.0/storage-pools?recursion=1') cat "$D/api/pools.json" ;;
    'query /1.0/networks?recursion=1') cat "$D/api/networks.json" ;;
    'query /1.0/instances?recursion=1') cat "$D/api/instances.json" ;;
    'query /1.0/instances?recursion=2') cat "$D/api/instances2.json" ;;
    'query /1.0/storage-pools/workloads') [[ -e $D/api/nopool ]] && return 1; echo '{}' ;;
    'query /1.0/profiles/default') [[ -e $D/api/nopool ]] && echo '{"devices":{}}' || echo '{"devices":{"root":{}}}' ;;
    'stop '*--force*) jq --arg n "$2" 'map(if .name == $n then .status = "Stopped" else . end)' "$D/api/instances.json" > "$D/api/tmp" && mv "$D/api/tmp" "$D/api/instances.json" ;;
    'stop '*) [[ -e $D/api/stubborn-$2 ]] && return 1
              jq --arg n "$2" 'map(if .name == $n then .status = "Stopped" else . end)' "$D/api/instances.json" > "$D/api/tmp" && mv "$D/api/tmp" "$D/api/instances.json" ;;
    'config set '*) jq --arg n "$3" 'map(if .name == $n then .config["boot.autostart"] = "false" else . end)' "$D/api/instances.json" > "$D/api/tmp" && mv "$D/api/tmp" "$D/api/instances.json" ;;
    'admin shutdown --timeout 60') [[ -n ${DAEMON:-} ]] && [[ ! -e $D/api/hangs ]] && { kill "$DAEMON"; wait "$DAEMON" 2>/dev/null; } ;;
    *) return 0 ;;
  esac
}
'''


def good_instance(name="ubuntudev"):
    config = {"boot.autostart": "true", "security.nesting": "true", "security.syscalls.intercept.mknod": "true",
              "security.syscalls.intercept.setxattr": "true", "limits.memory": "28GiB", "boot.autostart.priority": "10",
              "boot.host_shutdown_timeout": "120", "linux.kernel_modules": "nft_ct, overlay", "image.os": "ubuntu",
              "volatile.base_image": "abc", "volatile.idmap.base": "1000000", "user.note": "dev box"}
    devices = {"eth0": {"type": "nic", "nictype": "bridged", "parent": "br0", "hwaddr": "52:54:00:90:c2:d9", "name": "enp1s0"},
               "tun": {"type": "unix-char", "source": "/dev/net/tun", "path": "/dev/net/tun"}}
    return {"name": name, "type": "container", "status": "Running", "profiles": ["default"], "ephemeral": False,
            "config": dict(config), "expanded_config": dict(config), "devices": copy.deepcopy(devices),
            "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "workloads"}, **copy.deepcopy(devices)}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="polyseat-ctl-wl"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.dir.chmod(0o700)
        self.flash = self.dir / "boot/config/plugins/polyseat-unraid"
        self.flash.mkdir(parents=True)
        self.run_dir = self.dir / "var/run/polyseat-unraid"
        self.work = self.dir / "mnt/cache/polyseat/workloads"
        self.incus = self.work / "incus"
        (self.dir / "mnt/cache/polyseat").mkdir(parents=True)
        (self.dir / "mnt/cache/polyseat").chmod(0o755)  # the dataset root: only root may write
        (self.dir / "api").mkdir()
        self.cfg(WORKLOADS_INSTANCES="ubuntudev")
        self.set_api()

    def cfg(self, **values):
        (self.flash / "polyseat-unraid.cfg").write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))

    def set_api(self, instances=None, **docs):
        api = self.dir / "api"
        defaults = {
            "server": {"config": {}, "environment": {}}, "projects": [{"name": "default"}],
            "profiles": [{"name": "default", "config": {}, "devices": {"root": {"type": "disk", "path": "/", "pool": "workloads"}}}],
            "pools": [{"name": "workloads", "driver": "dir", "config": {"source": f"{self.incus}/storage-pools/workloads"}}],
            "networks": [{"name": "br0", "managed": False}],
            "instances": [good_instance()] if instances is None else instances}
        defaults.update(docs)
        for name, doc in defaults.items():
            (api / f"{name}.json").write_text(json.dumps(doc))
        full = [dict(i, state={"network": {"lo": {"addresses": [{"family": "inet", "scope": "local", "address": "127.0.0.1"}]},
                                           "enp1s0": {"addresses": [{"family": "inet6", "scope": "global", "address": "fd00::1"},
                                                                    {"family": "inet", "scope": "global", "address": "192.168.178.144"}]}}})
                for i in defaults["instances"]]
        (api / "instances2.json").write_text(json.dumps(full))

    def bash(self, body, *args, api=True, check=None):
        head = HEAD.replace("@SCHEMA@", str(CTL.parent.parent / "schema.json")).replace("@CTL@", str(CTL))
        res = source_ctl(head + (API if api else "") + body, str(self.dir), *args)
        if check is not None:
            self.assertEqual(res.returncode, check, res.stdout + res.stderr)
        return res

    def calls(self):
        f = self.dir / "calls"
        return f.read_text().splitlines() if f.exists() else []


class PolicyTests(Fixture):
    def violations(self, instances=None, **docs):
        self.set_api(instances, **docs)
        res = self.bash("wl_policy_check", check=0)
        return [line for line in res.stdout.splitlines() if line]

    def with_instance(self, mutate):
        inst = good_instance()
        mutate(inst)
        return self.violations([inst])

    def test_the_dev_box_as_the_owner_describes_it_is_in_policy(self):
        self.assertEqual(self.violations(), [])
        self.assertEqual(self.violations([]), [], "no instance at all is fine")

    def test_allowed_variations_stay_in_policy(self):
        def set_both(inst, key, value):
            inst["config"][key] = inst["expanded_config"][key] = value
        def dev(inst, name, value):
            inst["devices"][name] = inst["expanded_devices"][name] = value
        cases = {
            "nesting off": lambda i: set_both(i, "security.nesting", "false"),
            "autostart off": lambda i: set_both(i, "boot.autostart", "false"),
            "limits": lambda i: [set_both(i, "limits.cpu", "0-3"), set_both(i, "limits.cpu.allowance", "50%"),
                                 set_both(i, "limits.processes", "20000"), set_both(i, "limits.kernel.nofile", "1048576")],
            "every allowed module": lambda i: set_both(i, "linux.kernel_modules", ",".join(MODULES)),
            "one module": lambda i: set_both(i, "linux.kernel_modules", "overlay"),
            "setxattr only": lambda i: [i["config"].pop("security.syscalls.intercept.mknod"), i["expanded_config"].pop("security.syscalls.intercept.mknod")],
            "eth0 as the nic name": lambda i: dev(i, "eth0", dict(i["devices"]["eth0"], name="eth0")),
            "lower case mac": lambda i: dev(i, "eth0", dict(i["devices"]["eth0"], hwaddr="52:54:00:90:c2:D9")),
            "a share under /mnt/user, read-only": lambda i: dev(i, "media", {"type": "disk", "source": "/mnt/user/media", "path": "/media", "readonly": "true"}),
            "a share under /mnt/cache": lambda i: dev(i, "code", {"type": "disk", "source": "/mnt/cache/appdata/code", "path": "/srv/code"}),
            "a share in games": lambda i: dev(i, "g", {"type": "disk", "source": "/mnt/cache/polyseat-games/x", "path": "/g", "readonly": "false"}),
            "a custom volume of the workloads pool": lambda i: dev(i, "docker", {"type": "disk", "pool": "workloads", "source": "docker", "path": "/var/lib/docker"}),
            "no nic yet": lambda i: [i["devices"].pop("eth0"), i["expanded_devices"].pop("eth0")],
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.assertEqual(self.with_instance(mutate), [])

    def test_configuration_outside_the_allowlist_is_a_violation(self):
        def both(key, value):
            def mutate(inst):
                inst["config"][key] = inst["expanded_config"][key] = value
            return mutate
        bad = {
            "privileged": ("security.privileged", "true", "config security.privileged"),
            "raw lxc": ("raw.lxc", "lxc.cap.drop=", "config raw.lxc"),
            "raw idmap": ("raw.idmap", "both 0 0", "config raw.idmap"),
            "isolated idmap": ("security.idmap.isolated", "true", "config security.idmap.isolated"),
            "nvidia": ("nvidia.runtime", "true", "config nvidia.runtime"),
            "devlxd": ("security.devlxd", "false", "config security.devlxd"),
            "cloud-init": ("cloud-init.user-data", "#cloud-config", "config cloud-init.user-data"),
            "environment": ("environment.X", "1", "config environment.X"),
            "a foreign module": ("linux.kernel_modules", "nft_ct,vfio-pci", "config linux.kernel_modules"),
            "only a foreign module": ("linux.kernel_modules", "vhost_net", "config linux.kernel_modules"),
            "an empty module list": ("linux.kernel_modules", "", "config linux.kernel_modules"),
            "autostart that is not a bool": ("boot.autostart", "yes", "config boot.autostart"),
            "a limit with shell characters": ("limits.memory", "1GiB; id", "config limits.memory"),
            "a non numeric priority": ("boot.autostart.priority", "high", "config boot.autostart.priority"),
            "mknod that is not a bool": ("security.syscalls.intercept.mknod", "maybe", "config security.syscalls.intercept.mknod"),
        }
        for label, (key, value, expect) in bad.items():
            with self.subTest(label):
                lines = self.with_instance(both(key, value))
                self.assertTrue(any(f"instance ubuntudev: {expect}" in line for line in lines), lines)
        # an expanded value alone (it came from a profile) counts as well
        def only_expanded(inst):
            inst["expanded_config"]["security.privileged"] = "true"
        self.assertTrue(self.with_instance(only_expanded))

    def test_devices_outside_the_allowlist_are_a_violation(self):
        eth = good_instance()["devices"]["eth0"]
        def dev(name, value):
            def mutate(inst):
                inst["devices"][name] = inst["expanded_devices"][name] = value
            return mutate
        bad = {
            "a second nic": dev("eth1", dict(eth, hwaddr="52:54:00:90:c2:da")),
            "another bridge": dev("eth0", dict(eth, parent="br1")),
            "another nic name": dev("eth0", dict(eth, name="eth1")),
            "no hwaddr": dev("eth0", {k: v for k, v in eth.items() if k != "hwaddr"}),
            "a malformed hwaddr": dev("eth0", dict(eth, hwaddr="52:54:00:90:c2")),
            "macvlan": dev("eth0", dict(eth, nictype="macvlan")),
            "mac filtering": dev("eth0", dict(eth, **{"security.mac_filtering": "true"})),
            "an ip on the nic": dev("eth0", dict(eth, **{"ipv4.address": "192.168.178.144"})),
            "tun with a mode": dev("tun", {"type": "unix-char", "source": "/dev/net/tun", "path": "/dev/net/tun", "mode": "0666"}),
            "another character device": dev("fuse", {"type": "unix-char", "source": "/dev/fuse", "path": "/dev/fuse"}),
            "kvm": dev("kvm", {"type": "unix-char", "source": "/dev/kvm", "path": "/dev/kvm"}),
            "tun somewhere else": dev("tun", {"type": "unix-char", "source": "/dev/net/tun", "path": "/dev/tun"}),
            "a gpu": dev("gpu", {"type": "gpu", "pci": "0000:01:00.0"}),
            "a usb device": dev("usb", {"type": "usb", "vendorid": "1234"}),
            "a proxy": dev("p", {"type": "proxy", "listen": "tcp:0.0.0.0:80", "connect": "tcp:127.0.0.1:80"}),
            "a block device": dev("sda", {"type": "disk", "source": "/dev/sda", "path": "/mnt/x"}),
            "a share below /mnt/disks": dev("s", {"type": "disk", "source": "/mnt/disks/x", "path": "/x"}),
            "the whole of /mnt/user": dev("s", {"type": "disk", "source": "/mnt/user", "path": "/x"}),
            "dot dot in the source": dev("s", {"type": "disk", "source": "/mnt/user/../../etc", "path": "/x"}),
            "the Incus workspace": dev("s", {"type": "disk", "source": "/mnt/cache/polyseat/workloads/incus", "path": "/x"}),
            "the play workspace": dev("s", {"type": "disk", "source": "/mnt/cache/polyseat/incus", "path": "/x"}),
            "polyseat through the user share": dev("s", {"type": "disk", "source": "/mnt/user/polyseat/steam-seat", "path": "/x"}),
            "a share with shift": dev("s", {"type": "disk", "source": "/mnt/user/x", "path": "/x", "shift": "true"}),
            "a share onto /": dev("s", {"type": "disk", "source": "/mnt/user/x", "path": "/"}),
            "a share with dot dot in its path": dev("s", {"type": "disk", "source": "/mnt/user/x", "path": "/a/../b"}),
            "a share with a relative path": dev("s", {"type": "disk", "source": "/mnt/user/x", "path": "x"}),
            "readonly that is not a bool": dev("s", {"type": "disk", "source": "/mnt/user/x", "path": "/x", "readonly": "yes"}),
            "a volume of another pool": dev("v", {"type": "disk", "pool": "default", "source": "docker", "path": "/x"}),
            "a volume with a path as its source": dev("v", {"type": "disk", "pool": "workloads", "source": "/etc", "path": "/x"}),
            "a root that is not the root": dev("root", {"type": "disk", "source": "/mnt/user/x", "path": "/"}),
            "a root on another pool": dev("root", {"type": "disk", "path": "/", "pool": "default"}),
            "a device that is not an object": dev("x", "string"),
        }
        for label, mutate in bad.items():
            with self.subTest(label):
                lines = self.with_instance(mutate)
                self.assertTrue(any(line.startswith("instance ubuntudev: device") or "more than one nic" in line for line in lines), lines)
        # expanded devices alone (from a profile) count too
        def only_expanded(inst):
            inst["expanded_devices"]["usb"] = {"type": "usb"}
        self.assertTrue(self.with_instance(only_expanded))

    def test_the_instance_itself(self):
        def vm(inst): inst["type"] = "virtual-machine"
        def profiles(inst): inst["profiles"] = ["default", "extra"]
        def ephemeral(inst): inst["ephemeral"] = True
        for label, mutate, expect in (("a vm", vm, "not a container"), ("two profiles", profiles, "profiles"), ("ephemeral", ephemeral, "ephemeral")):
            with self.subTest(label):
                self.assertIn(f"instance ubuntudev: {expect}", self.with_instance(mutate))
        # names: only the listed ones, and the same regex everywhere
        self.assertEqual(self.violations([good_instance("other")]), ["instance other: not listed in WORKLOADS_INSTANCES"])
        self.cfg(WORKLOADS_INSTANCES="ubuntudev other")
        self.assertEqual(self.violations([good_instance("other"), good_instance("ubuntudev")]), [])
        self.cfg(WORKLOADS_INSTANCES="")
        self.assertEqual(self.violations(), ["instance ubuntudev: not listed in WORKLOADS_INSTANCES"], "an empty setting allows nothing")
        self.cfg(WORKLOADS_INSTANCES="Bad Name")
        self.assertEqual(self.violations(), ["global: WORKLOADS_INSTANCES is not usable"])

    def test_global_violations(self):
        pool = {"name": "workloads", "driver": "dir", "config": {"source": f"{self.incus}/storage-pools/workloads"}}
        root = {"type": "disk", "path": "/", "pool": "workloads"}
        cases = {
            "server config": ({"server": {"config": {"core.https_address": ":8443"}}}, "global: server config"),
            "another project": ({"projects": [{"name": "default"}, {"name": "x"}]}, "global: projects"),
            "a managed network": ({"networks": [{"name": "incusbr0", "managed": True}]}, "global: a managed network"),
            "no pool": ({"pools": []}, "global: storage pools"),
            "a second pool": ({"pools": [pool, dict(pool, name="default")]}, "global: storage pools"),
            "a zfs pool": ({"pools": [dict(pool, driver="zfs")]}, "global: storage pools"),
            "a pool elsewhere": ({"pools": [dict(pool, config={"source": "/mnt/user/x"})]}, "global: storage pools"),
            "a nic in the default profile": ({"profiles": [{"name": "default", "config": {}, "devices": {
                "root": root, "eth0": {"type": "nic", "nictype": "bridged", "parent": "br0"}}}]}, "global: profiles"),
            "profile config": ({"profiles": [{"name": "default", "config": {"security.privileged": "true"}, "devices": {"root": root}}]}, "global: profiles"),
            "another profile": ({"profiles": [{"name": "default", "config": {}, "devices": {"root": root}}, {"name": "x"}]}, "global: profiles"),
        }
        for label, (docs, expect) in cases.items():
            with self.subTest(label):
                self.assertIn(expect, self.violations(**docs))

    def test_a_check_that_cannot_run_is_an_error_not_a_pass(self):
        self.set_api()
        res = self.bash('run_incus() { return 1; }; wl_policy_check; echo "rc=$?"; wl_policy_enforce; echo "enforce=$?"', check=0)
        self.assertIn("rc=1", res.stdout)
        self.assertIn("enforce=1", res.stdout)
        (self.dir / "api/instances.json").write_text("not json")
        res = self.bash('wl_policy_enforce; echo "enforce=$?"', check=0)
        self.assertIn("enforce=1", res.stdout)

    def enforce(self, instances, **docs):
        self.set_api(instances, **docs)
        (self.dir / "calls").unlink(missing_ok=True)
        res = self.bash('wl_policy_enforce; echo "rc=$? ok=$WL_POLICY_OK"; echo "$WL_VIOLATIONS"', check=0)
        return res

    def test_an_offender_is_stopped_and_kept_from_autostarting_and_nothing_else_is_touched(self):
        bad = good_instance("ubuntudev-test")
        bad["config"]["security.privileged"] = "true"
        self.cfg(WORKLOADS_INSTANCES="ubuntudev ubuntudev-test")
        res = self.enforce([good_instance(), bad])
        self.assertIn("rc=0 ok=false", res.stdout)
        self.assertEqual([c for c in self.calls() if not c.startswith("query")],
                         ["stop ubuntudev-test --force --timeout 10", "config set ubuntudev-test boot.autostart=false"])
        self.assertIn("security.privileged", res.stdout.splitlines()[-1])
        self.assertIn("outside the policy", (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())
        # in policy again: nothing to do, the flag is clean
        res = self.enforce([good_instance()])
        self.assertIn("rc=0 ok=true", res.stdout)
        self.assertEqual([c for c in self.calls() if not c.startswith("query")], [])

    def test_an_unlisted_instance_is_stopped_and_a_stopped_one_is_only_pinned(self):
        stranger = good_instance("stranger")
        stopped = good_instance("ubuntudev-test")
        stopped["status"] = "Stopped"
        stopped["config"]["boot.autostart"] = stopped["expanded_config"]["boot.autostart"] = "true"
        stopped["devices"]["usb"] = stopped["expanded_devices"]["usb"] = {"type": "usb"}
        self.cfg(WORKLOADS_INSTANCES="ubuntudev ubuntudev-test")
        self.enforce([good_instance(), stranger, stopped])
        self.assertEqual([c for c in self.calls() if not c.startswith("query")],
                         ["stop stranger --force --timeout 10", "config set stranger boot.autostart=false",
                          "config set ubuntudev-test boot.autostart=false"])

    def test_a_global_violation_stops_everything(self):
        self.cfg(WORKLOADS_INSTANCES="ubuntudev ubuntudev-test")
        res = self.enforce([good_instance(), good_instance("ubuntudev-test")], networks=[{"name": "x", "managed": True}])
        self.assertIn("rc=0 ok=false", res.stdout)
        self.assertEqual([c for c in self.calls() if c.startswith("stop")],
                         ["stop ubuntudev --force --timeout 10", "stop ubuntudev-test --force --timeout 10"])

    def test_the_same_violation_is_logged_once(self):
        bad = good_instance()
        bad["config"]["raw.lxc"] = "x"
        self.set_api([bad])
        self.bash('wl_policy_enforce; wl_policy_enforce; wl_policy_enforce', check=0)
        log = (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text()
        self.assertEqual(log.count("workloads: outside the policy:"), 1)


class AllowlistTests(unittest.TestCase):
    INCUS = "/mnt/cache/polyseat/workloads/incus"

    def allowed(self, operation, command):
        body = r"""
          POLYSEAT_RELEASE_GATE=canary
          init_paths
          INCUS_DIR=/mnt/cache/polyseat/workloads/incus
          CANARY_OPERATION=$1
          eval "set -- $2"
          canary_command_allowed "$@"
        """
        return source_ctl(body, str(operation), command).returncode == 0

    def test_operation_9_allows_exactly_what_the_lifecycle_needs(self):
        for command in ("info", "config get core.https_address", "query /1.0", "query /1.0/projects?recursion=1",
                        "query /1.0/profiles?recursion=1", "query /1.0/instances?recursion=1", "query /1.0/instances?recursion=2",
                        "query /1.0/storage-pools?recursion=1", "query /1.0/networks?recursion=1",
                        "query /1.0/storage-pools/workloads", "query /1.0/profiles/default",
                        f"storage create workloads dir source={self.INCUS}/storage-pools/workloads",
                        "profile device add default root disk path=/ pool=workloads",
                        "admin shutdown --timeout 60", "stop ubuntudev --timeout 120", "stop ubuntudev --force --timeout 10",
                        "config set ubuntudev boot.autostart=false"):
            with self.subTest(command=command):
                self.assertTrue(self.allowed(9, command))
        for command in ("start ubuntudev", "delete ubuntudev", "delete ubuntudev --force", "init images:ubuntu/26.04 x",
                        "exec ubuntudev -- sh", "admin shutdown", "admin shutdown --timeout 600", "stop ubuntudev",
                        "stop ubuntudev --timeout 5", "stop ubuntudev --force --timeout 5", "stop ubuntudev --force",
                        "stop Bad_Name --timeout 120", "stop ubuntudev --timeout 120 --force",
                        "config set ubuntudev boot.autostart=true", "config set ubuntudev security.privileged=true",
                        "config set ubuntudev boot.autostart=false x", "config device add ubuntudev x disk",
                        "profile device add default eth0 nic nictype=bridged parent=br0 name=eth0",
                        "profile device add default root disk path=/ pool=default",
                        f"storage create default dir source={self.INCUS}/storage-pools/default",
                        "storage create workloads zfs source=cache/x", "storage delete workloads",
                        "storage create workloads dir source=/mnt/user/x", "image import /x --alias y", "image delete y",
                        "project create x", "network create x", "config set core.https_address :8443", "file push x y",
                        "query /1.0/instances/ubuntudev/state", "query /1.0/instances?recursion=3", "snapshot create ubuntudev"):
            with self.subTest(command=command):
                self.assertFalse(self.allowed(9, command))

    def test_the_two_daemons_allowlists_do_not_overlap_where_they_could_hurt(self):
        # play's operation cannot touch the workloads pool or stop with the workloads timeouts, and the other way round
        for command in ("storage create workloads dir source=/mnt/cache/polyseat/incus/incus/storage-pools/workloads",
                        "stop x --timeout 120", "query /1.0/instances?recursion=2"):
            self.assertFalse(self.allowed(8, command), command)
        for command in ("storage create default dir source=/mnt/cache/polyseat/workloads/incus/storage-pools/default",
                        "exec seat1 -- sudo -u player env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user stop polyseat-sway.service",
                        "stop seat1 --timeout 90", "query /1.0/profiles/default x"):
            self.assertFalse(self.allowed(9, command), command)
        self.assertTrue(self.allowed(8, "stop seat1 --timeout 90"))
        for operation in (1, 2, 3, 4, 5, 6, 7):
            self.assertFalse(self.allowed(operation, "stop ubuntudev --timeout 120"))
            self.assertFalse(self.allowed(operation, "query /1.0/instances?recursion=2"))
            self.assertFalse(self.allowed(operation, "config set ubuntudev boot.autostart=false"))

    def test_run_incus_has_no_watchdog_for_the_permanent_daemon_and_keeps_it_for_the_others(self):
        text = CTL.read_text()
        body = re.search(r"^run_incus\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertRegex(body, r'if \[\[ \$CANARY_OPERATION -ne 9 \]\]; then\n\s+canary_watchdog_check \|\|')
        self.assertIn('INCUS_CONF="$INCUS_CLIENT_DIR"', body)


class ModulesTests(Fixture):
    def load(self, present=(), failing=None, real=True):
        (self.dir / "sysmodule").mkdir(exist_ok=True)
        for mod in present:
            (self.dir / "sysmodule" / mod).mkdir(exist_ok=True)
        (self.dir / "modprobe").write_text('#!/bin/sh\necho "$*" >> "$MODLOG"\n[ -n "$FAILING" ] && [ "$2" = "$FAILING" ] && exit 1\nexit 0\n')
        (self.dir / "modprobe").chmod(0o755)
        play_real = "play_real() { return 0; }" if real else ""
        return self.bash(f'''
          {play_real}
          export MODLOG="$D/modlog" FAILING={failing or ""}
          WL_MODPROBE="$D/modprobe"
          wl_modules_load; echo "rc=$?"''', api=False)

    def loaded(self):
        f = self.dir / "modlog"
        return f.read_text().splitlines() if f.exists() else []

    def test_exactly_the_allowlisted_set_is_loaded_in_order_and_only_what_is_missing(self):
        res = self.load()
        self.assertIn("rc=0", res.stdout)
        self.assertEqual(self.loaded(), ["-- " + m for m in MODULES])
        self.assertIn("kernel modules loaded: " + " ".join(MODULES), (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())
        (self.dir / "modlog").unlink()
        res = self.load(present=["nft_ct", "overlay"])
        self.assertEqual(self.loaded(), ["-- nft_chain_nat", "-- nft_masq", "-- nft_nat", "-- nft_compat"])
        (self.dir / "modlog").unlink()
        res = self.load(present=MODULES)
        self.assertEqual(self.loaded(), [])
        self.assertIn("rc=0", res.stdout)

    def test_a_module_that_cannot_be_loaded_stops_the_start(self):
        res = self.load(failing="nft_nat")
        self.assertIn("rc=1", res.stdout)
        self.assertEqual(self.loaded(), ["-- nft_chain_nat", "-- nft_masq", "-- nft_nat"])
        self.assertIn("cannot load the kernel module nft_nat", res.stderr)

    def test_a_sandbox_never_runs_the_tool(self):
        res = self.load(real=False)
        self.assertIn("rc=0", res.stdout)
        self.assertEqual(self.loaded(), [])

    def test_the_set_the_tool_and_its_one_call_site_are_pinned(self):
        text = CTL.read_text()
        self.assertEqual(re.findall(r'^WL_MODULES="([^"]*)"$', text, re.M), [" ".join(MODULES)])
        self.assertEqual(re.findall(r'^WL_MODPROBE="([^"]*)"$', text, re.M), ["/sbin/modprobe"])
        code = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        calls = [line for line in code if '"$WL_MODPROBE"' in line and not line.startswith("WL_MODPROBE=")]
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0].strip(), 'if play_real; then "$WL_MODPROBE" -- "$mod" || { log error "workloads: cannot load the kernel module $mod"; return 1; }; fi')
        # never unloaded: the modules are shared with the host
        block = text[text.index("# -------------------------------------------------------------- workloads ----"):text.index("# --------------------------------------------------------- transaction ----")]
        for line in block.splitlines():
            if not line.lstrip().startswith("#"):
                self.assertNotRegex(line, r"\b(rmmod|insmod|modprobe -r)\b|WL_MODPROBE\"? +-r")
        # and the instance policy accepts no other module than those
        self.assertIn('--arg modules "$WL_MODULES"', text)


class CgroupTests(Fixture):
    def cg(self, **files):
        root = self.dir / "cg"
        root.mkdir(exist_ok=True)
        defaults = {"cgroup.controllers": "cpuset cpu io memory pids", "cgroup.subtree_control": "cpuset cpu memory pids",
                    "cpuset.cpus.effective": "0-27", "cpuset.mems.effective": "0"}
        defaults.update(files)
        for name, value in defaults.items():
            if value is None:
                (root / name).unlink(missing_ok=True)
            else:
                (root / name).write_text(value + "\n")

    def run_cg(self, body="wl_cgroup_prepare", **cfg):
        if cfg:
            self.cfg(WORKLOADS_INSTANCES="ubuntudev", **cfg)
        return self.bash(f'fs_type() {{ echo cgroup2fs; }}\n{body}; echo "rc=$?"', api=False)

    def test_the_group_is_made_with_a_cpuset_and_a_memory_value_that_read_back(self):
        self.cg()
        res = self.run_cg()
        self.assertIn("rc=0", res.stdout, res.stderr)
        group = self.dir / "cg/polyseat-unraid-workloads"
        self.assertEqual((group / "cpuset.cpus").read_text().strip(), "0-27")
        self.assertEqual((group / "cpuset.mems").read_text().strip(), "0")
        self.assertEqual((group / "memory.max").read_text().strip(), "max")
        self.assertTrue((group / "daemon").is_dir())
        # nothing that makes it a budget in the play sense: no oom group, no swap or pids limit, no cpu.max, no kill switch
        self.assertEqual(sorted(p.name for p in group.iterdir()), ["cpuset.cpus", "cpuset.mems", "daemon", "memory.max"])

    def test_the_owner_limits_become_cgroup_values(self):
        self.cg()
        res = self.run_cg(WORKLOADS_MEMORY_MB="28672", WORKLOADS_CPUS="0,1,2,3,16-19")
        self.assertIn("rc=0", res.stdout, res.stderr)
        group = self.dir / "cg/polyseat-unraid-workloads"
        self.assertEqual((group / "memory.max").read_text().strip(), str(28672 * 1048576))
        self.assertEqual((group / "cpuset.cpus").read_text().strip(), "0-3,16-19", "as the kernel prints it")

    def test_a_host_that_does_not_fit_refuses_before_anything_is_made(self):
        for label, files, cfg in (
                ("cpus the host lacks", {}, {"WORKLOADS_CPUS": "0-31"}),
                ("an unusable memory value", {}, {"WORKLOADS_MEMORY_MB": "lots"}),
                ("memory not delegated", {"cgroup.subtree_control": "cpuset cpu pids"}, {}),
                ("pids not available", {"cgroup.controllers": "cpuset cpu memory", "cgroup.subtree_control": "cpuset cpu memory"}, {}),
                ("an odd numa string", {"cpuset.mems.effective": "x"}, {}),
                ("no effective cpus", {"cpuset.cpus.effective": "none"}, {})):
            with self.subTest(label):
                shutil.rmtree(self.dir / "cg", ignore_errors=True)
                self.cg(**files)
                res = self.run_cg(**cfg)
                self.assertIn("rc=1", res.stdout)
                self.assertFalse((self.dir / "cg/polyseat-unraid-workloads").exists())
        shutil.rmtree(self.dir / "cg", ignore_errors=True)
        self.cg()
        (self.dir / "cg/polyseat-unraid-workloads").mkdir()
        res = self.run_cg()
        self.assertIn("rc=1", res.stdout)
        self.assertIn("exists already", res.stderr)

    def test_not_a_cgroup_v2_refuses(self):
        self.cg()
        res = self.bash('fs_type() { echo tmpfs; }\nwl_cgroup_prepare; echo "rc=$?"', api=False)
        self.assertIn("rc=1", res.stdout)
        self.assertFalse((self.dir / "cg/polyseat-unraid-workloads").exists())


class WorkspaceRecordTests(Fixture):
    def setUp(self):
        super().setUp()
        (self.dir / "manifest.json").write_text('{"incus_version": "7.5.1"}\n')

    def prepare(self, version="7.5.1"):
        return self.bash(f'''
          bundle_manifest() {{ printf '%s\\n' "$D/manifest.json"; }}
          jq -n --arg v {version} '{{incus_version: $v}}' > "$D/manifest.json"
          PTR_CURRENT={SHA}
          wl_state_prepare {SHA}; echo "rc=$?"''', api=False)

    def record(self, instances, mode=0o600, **extra):
        f = self.work / "clean-stop.json"
        f.write_text(json.dumps({"schema": 1, "archive_sha256": SHA, "stopped_at": 1, "instances": instances, **extra}))
        f.chmod(mode)
        return f

    def test_the_first_start_creates_the_workspace_and_later_ones_need_a_clean_stop(self):
        res = self.prepare()
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertEqual(oct(self.work.stat().st_mode & 0o777), "0o711")
        self.assertEqual(oct(self.incus.stat().st_mode & 0o777), "0o711")
        rec = json.loads((self.work / "workloads.json").read_text())
        self.assertEqual((rec["kind"], rec["incus_version"]), ("workloads", "7.5.1"))
        res = self.prepare()
        self.assertIn("rc=1", res.stdout)
        self.assertIn("recorded no clean stop", res.stderr)
        self.record([{"name": "ubuntudev", "status": "Stopped", "autostart": "true"}])
        res = self.prepare()
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertFalse((self.work / "clean-stop.json").exists(), "consumed before the daemon starts")
        self.assertTrue((self.work / "clean-stop.used.json").exists())
        self.assertIn("rc=1", self.prepare().stdout, "a crash from here on means inspection")

    def test_instances_keep_their_autostart_in_the_record(self):
        self.assertIn("rc=0", self.prepare().stdout)
        self.record([{"name": "a", "status": "Stopped", "autostart": "true"}, {"name": "b", "status": "Stopped", "autostart": "false"},
                     {"name": "c", "status": "Stopped", "autostart": ""}])
        self.assertIn("rc=0", self.prepare().stdout)

    def test_a_record_that_does_not_prove_a_clean_stop_is_refused_and_left_for_inspection(self):
        self.assertIn("rc=0", self.prepare().stdout)
        stopped = {"name": "ubuntudev", "status": "Stopped", "autostart": "true"}
        cases = {
            "an instance still running": ([dict(stopped, status="Running")], 0o600, {}),
            "an instance frozen": ([dict(stopped, status="Frozen")], 0o600, {}),
            "an odd autostart": ([dict(stopped, autostart="maybe")], 0o600, {}),
            "a bad name": ([dict(stopped, name="Not A Name")], 0o600, {}),
            "a duplicate": ([stopped, stopped], 0o600, {}),
            "world readable": ([stopped], 0o644, {}),
            "group readable": ([stopped], 0o640, {}),
            "a stray key": ([stopped], 0o600, {"rogue": 1}),
        }
        for label, (instances, mode, extra) in cases.items():
            with self.subTest(label):
                self.record(instances, mode, **extra)
                self.assertIn("rc=1", self.prepare().stdout)
                self.assertTrue((self.work / "clean-stop.json").exists())
        f = self.record([stopped])
        data = json.loads(f.read_text())
        data["schema"] = 2
        f.write_text(json.dumps(data))
        f.chmod(0o600)
        self.assertIn("rc=1", self.prepare().stdout)
        self.record([stopped])
        (self.work / "clean-stop.json").chmod(0o600)
        os.symlink(self.work / "clean-stop.json", self.work / "link.json")
        self.assertIn("rc=1", self.prepare(version="7.6.0").stdout, "no silent up- or downgrade")
        self.assertIn("rc=0", self.prepare().stdout)

    def test_a_workspace_without_its_record_or_with_a_symlinked_incus_dir_is_refused(self):
        self.assertIn("rc=0", self.prepare().stdout)
        (self.work / "workloads.json").unlink()
        self.record([])
        self.assertIn("rc=1", self.prepare().stdout)
        (self.work / "workloads.json").write_text('{"schema": 1, "kind": "persistent", "incus_version": "7.5.1"}')
        self.assertIn("rc=1", self.prepare().stdout, "play's workspace record is not ours")


class RecoveryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.work.mkdir(parents=True, mode=0o711)
        self.incus.mkdir(mode=0o711)
        (self.work / "workloads.json").write_text('{"schema": 1, "kind": "workloads", "incus_version": "7.5.1"}')
        (self.dir / "manifest.json").write_text('{"incus_version": "7.5.1"}\n')

    def recover(self, pre="", *args):
        return self.bash(f'''
          bundle_manifest() {{ printf '%s\\n' "$D/manifest.json"; }}
          persist_mount_check() {{ return 0; }}
          read_pointer() {{ PTR_CURRENT={SHA}; }}
          ensure_state_dirs() {{ :; }}
          {pre}
          cmd_workloads_recover''', *args, api=False)

    def test_it_needs_the_operators_word(self):
        res = self.recover()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--approve", res.stderr)

    def test_nothing_runs_so_the_record_is_written_and_the_next_start_may_reopen(self):
        (self.run_dir).mkdir(parents=True, exist_ok=True)
        (self.run_dir / "workloads-status.json").write_text("{}")
        res = self.recover("APPROVED=1")
        self.assertEqual(res.returncode, 0, res.stderr)
        rec = json.loads((self.work / "clean-stop.json").read_text())
        self.assertEqual((rec["recovered"], rec["instances"], rec["schema"]), (True, [], 1))
        self.assertEqual(oct((self.work / "clean-stop.json").stat().st_mode & 0o777), "0o600")
        self.assertFalse((self.run_dir / "workloads-status.json").exists(), "the failed state is cleared")
        # and the start path accepts exactly that record
        res = self.bash('''bundle_manifest() { printf '%s\\n' "$D/manifest.json"; }
          PTR_CURRENT=a; wl_record_check; echo "rc=$?"''', api=False)
        self.assertIn("rc=0", res.stdout, res.stderr)

    def test_anything_left_of_the_last_run_refuses(self):
        cases = {
            "the lock is held": ("exec 7> \"$WL_LOCKFILE\"; flock -n 7; ( cmd_workloads_recover )", "running, or is being started"),
            "the daemon is alive": ("command sleep 8 & echo $! > \"$WL_PIDFILE\"; incusd_pid() { cat \"$1\"; }", "daemon is running"),
            "the group exists": ("mkdir -p \"$WL_CGROUP\"", "still exists"),
            "a mount below the workspace": ("wl_mounts_below() { echo \"$DATA_REAL/incus/devices/x\"; }", "still mounted below"),
            "a clean-stop record exists": (": > \"$DATA_REAL/clean-stop.json\"", "nothing to recover"),
        }
        for label, (pre, expect) in cases.items():
            with self.subTest(label):
                (self.work / "clean-stop.json").unlink(missing_ok=True)
                shutil.rmtree(self.dir / "cg", ignore_errors=True)
                res = self.recover("APPROVED=1\n" + pre)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(expect, res.stderr)
                if label != "a clean-stop record exists":
                    self.assertFalse((self.work / "clean-stop.json").exists())

    def test_a_process_of_the_instance_anywhere_refuses(self):
        proc = subprocess.Popen(["sleep", "30"], env={"INCUS_DIR": str(self.incus), "PATH": os.environ["PATH"]})
        self.addCleanup(proc.kill)
        res = self.recover("APPROVED=1")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"process {proc.pid} still works on", res.stderr)
        proc.kill()
        proc.wait()
        self.assertEqual(self.recover("APPROVED=1").returncode, 0)

    def test_a_process_in_the_group_refuses_even_after_the_directory_is_gone(self):
        fake = self.dir / "proc"
        (fake / "4242").mkdir(parents=True)
        (fake / "4242/cgroup").write_text("0::/polyseat-unraid-workloads/daemon/lxc.payload.ubuntudev\n")
        (fake / "4242/environ").write_bytes(b"PATH=/bin\0")
        res = self.recover(f"APPROVED=1\nWL_PROC={fake}")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("process 4242 is still in the workloads group", res.stderr)
        (fake / "4242/cgroup").write_text("0::/polyseat-unraid-canary/daemon\n")
        self.assertEqual(self.recover(f"APPROVED=1\nWL_PROC={fake}").returncode, 0, "play's group is not ours")

    def test_a_missing_or_foreign_workspace_is_not_recovered(self):
        (self.work / "workloads.json").write_text('{"schema": 1, "kind": "persistent", "incus_version": "7.5.1"}')
        self.assertNotEqual(self.recover("APPROVED=1").returncode, 0)
        shutil.rmtree(self.work)
        res = self.recover("APPROVED=1")
        self.assertNotEqual(res.returncode, 0)


class StopSequenceTests(Fixture):
    def stop(self, instances, setup="", expect_rc=None):
        self.set_api(instances)
        self.work.mkdir(parents=True, exist_ok=True)
        res = self.bash(f'''
          PTR_CURRENT={SHA}
          command sleep 300 & DAEMON=$!
          wl_daemon_pid() {{ kill -0 "$DAEMON" 2>/dev/null && echo "$DAEMON"; }}
          canary_cgroup_stop() {{ echo cgroup-stop >> "$D/calls"; return 0; }}
          {setup}
          wl_stop_sequence; echo "rc=$?"
          kill "$DAEMON" 2>/dev/null''')
        if expect_rc is not None:
            self.assertIn(f"rc={expect_rc}", res.stdout, res.stdout + res.stderr)
        return res

    def two(self):
        self.cfg(WORKLOADS_INSTANCES="ubuntudev ubuntudev-test")
        return [good_instance(), good_instance("ubuntudev-test")]

    def test_each_instance_gets_its_own_clean_stop_then_the_daemon_then_the_group_then_the_record(self):
        self.stop(self.two(), expect_rc=0)
        calls = [c for c in self.calls() if not c.startswith("query")]
        self.assertEqual(calls, ["stop ubuntudev --timeout 120", "stop ubuntudev-test --timeout 120",
                                 "admin shutdown --timeout 60", "cgroup-stop"])
        rec = json.loads((self.work / "clean-stop.json").read_text())
        self.assertEqual([(i["name"], i["status"], i["autostart"]) for i in rec["instances"]],
                         [("ubuntudev", "Stopped", "true"), ("ubuntudev-test", "Stopped", "true")], "autostart is kept: it is a permanent container")
        self.assertEqual(oct((self.work / "clean-stop.json").stat().st_mode & 0o777), "0o600")

    def test_an_already_stopped_instance_is_not_asked_again(self):
        inst = good_instance()
        inst["status"] = "Stopped"
        self.stop([inst], expect_rc=0)
        self.assertEqual([c for c in self.calls() if c.startswith("stop")], [])

    def test_an_instance_that_does_not_stop_in_time_is_forced_and_no_record_is_written(self):
        (self.dir / "api").mkdir(exist_ok=True)
        (self.dir / "api/stubborn-ubuntudev").write_text("")
        self.stop([good_instance()], expect_rc=1)
        calls = [c for c in self.calls() if not c.startswith("query")]
        self.assertEqual(calls, ["stop ubuntudev --timeout 120", "stop ubuntudev --force --timeout 10", "admin shutdown --timeout 60", "cgroup-stop"])
        self.assertFalse((self.work / "clean-stop.json").exists())
        self.assertIn("without a clean-stop record", (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())

    def test_a_daemon_that_does_not_exit_by_itself_is_no_clean_stop(self):
        (self.dir / "api").mkdir(exist_ok=True)
        (self.dir / "api/hangs").write_text("")
        self.stop([good_instance()], expect_rc=1)
        self.assertFalse((self.work / "clean-stop.json").exists())
        self.assertIn("did not exit by itself", (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())

    def test_an_offender_at_the_end_is_pinned_and_the_record_still_counts_when_it_is_stopped(self):
        bad = good_instance("ubuntudev-test")
        bad["config"]["security.privileged"] = "true"
        self.cfg(WORKLOADS_INSTANCES="ubuntudev ubuntudev-test")
        self.stop([good_instance(), bad], expect_rc=0)
        rec = json.loads((self.work / "clean-stop.json").read_text())
        self.assertEqual([(i["name"], i["autostart"]) for i in rec["instances"]], [("ubuntudev", "true"), ("ubuntudev-test", "false")])

    def test_a_policy_that_cannot_be_evaluated_or_a_failing_group_stop_is_no_clean_stop(self):
        self.stop([good_instance()], setup='wl_policy_enforce() { return 1; }', expect_rc=1)
        self.assertFalse((self.work / "clean-stop.json").exists())
        self.stop([good_instance()], setup='canary_cgroup_stop() { return 1; }', expect_rc=1)
        self.assertFalse((self.work / "clean-stop.json").exists())

    def test_without_a_daemon_there_is_nothing_to_ask_and_nothing_to_record(self):
        self.stop([good_instance()], setup='wl_daemon_pid() { return 1; }', expect_rc=1)
        self.assertEqual([c for c in self.calls() if c != "cgroup-stop"], [])
        self.assertFalse((self.work / "clean-stop.json").exists())


class SupervisorTests(Fixture):
    """cmd_workloads_run end to end, with a fake incusd process and fake cgroup directories; the real lock, state, record,
    stop and policy code in between."""

    def setUp(self):
        super().setUp()
        slot = self.dir / "usr/local/polyseat-unraid/slots" / SHA / "payload/bin"
        slot.mkdir(parents=True)
        (slot / "incusd").write_text("#!/bin/bash\ntrap 'kill $!; exit 0' TERM\nsleep 300 & wait\n")
        (slot / "incusd").chmod(0o755)
        (self.dir / "usr/local/polyseat-unraid/current").symlink_to(f"slots/{SHA}")
        bundles = self.flash / "bundles"
        bundles.mkdir()
        (bundles / f"{SHA}.manifest.json").write_text(json.dumps({"incus_version": "7.5.1", "lxc_rootfs_mount": "/var/lib/polyseat-lxc/rootfs"}))
        state = self.flash / "state"
        state.mkdir()
        (state / "pointer.json").write_text(json.dumps({"current": SHA, "previous": None}))
        (state / "pointer.json").chmod(0o600)
        (self.dir / "etc").mkdir()
        (self.dir / "etc/subuid").write_text("")
        (self.dir / "etc/subgid").write_text("")
        (self.dir / "sysmodule").mkdir()
        for mod in MODULES:
            (self.dir / "sysmodule" / mod).mkdir()
        (self.dir / "sys/class/net/br0").mkdir(parents=True)
        (self.dir / "cg").mkdir()
        for name, value in {"cgroup.controllers": "cpuset cpu io memory pids", "cgroup.subtree_control": "cpuset cpu memory pids",
                            "cpuset.cpus.effective": "0-27", "cpuset.mems.effective": "0"}.items():
            (self.dir / "cg" / name).write_text(value + "\n")
        self.slot_bin = slot

    SETUP = r'''
      fs_type() { echo cgroup2fs; }
      persist_mount_check() { return 0; }
      daemon_programs_check() { return 0; }
      lsm_gate() { return 0; }
      canary_cgroup_stop() { echo cgroup-stop >> "$D/calls"; rm -rf "$WL_CGROUP"; [[ ! -f $D/daemon.pid ]] || kill "$(cat "$D/daemon.pid")" 2>/dev/null; return 0; }
      wl_launch() { bash "$1/payload/bin/incusd" & echo $! > "$WL_PIDFILE"; DAEMON=$!; echo $! > "$D/daemon.pid"; }
      wl_health_check() { wl_daemon_pid >/dev/null; }
      wl_log_cap() { :; }
      WL_GUARD_EVERY=1 WL_HEALTH_TIMEOUT=3
      exec 8> "$WL_LOCKFILE"; flock -n 8
    '''

    def run_supervisor(self, scenario, mode="run", timeout=60):
        body = self.SETUP + scenario + (f'\ncmd_workloads_run {mode}; echo "rc=$?"; [[ -z ${{DAEMON:-}} ]] || kill "$DAEMON" 2>/dev/null\n'
                                       '[[ ! -f $D/daemon.pid ]] || kill "$(cat "$D/daemon.pid")" 2>/dev/null; true')
        head = HEAD.replace("@SCHEMA@", str(CTL.parent.parent / "schema.json")).replace("@CTL@", str(CTL))
        script = "source <(sed '$d' \"$1\")\nshift\n" + head + API + body
        # its own session: a runaway fake daemon or watcher is killed with it instead of holding the test's pipes open
        proc = subprocess.Popen(["bash", "-c", script, "x", str(CTL), str(self.dir)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env={"PATH": "/usr/bin:/bin"}, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            out, err = proc.communicate()
            self.fail("the supervisor did not finish in time:\n" + out + err[-3000:])
        finally:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    STOP_WHEN_RUNNING = r'''
      ( until grep -q '"state":"running"' "$WL_STATUS" 2>/dev/null; do command sleep 0.05; done; : > "$WL_STOPFILE" ) &
    '''

    def test_a_start_and_a_requested_stop_leave_a_clean_workspace_and_a_clean_stop_record(self):
        (self.dir / "api/nopool").write_text("")  # a fresh workspace: the pool and the root disk are made
        res = self.run_supervisor(self.STOP_WHEN_RUNNING)
        self.assertIn("rc=0", res.stdout, res.stdout + res.stderr)
        log = (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text()
        for step in ("kernel modules loaded", "daemon healthy", "workloads: up (in policy)", "workloads: stop (stop requested)",
                     "stopping ubuntudev", "admin shutdown", "clean stop recorded"):
            self.assertIn(step, log)
        calls = [c for c in self.calls() if not c.startswith("query")]
        self.assertIn("storage create workloads dir source=" + str(self.incus) + "/storage-pools/workloads", calls)
        self.assertIn("profile device add default root disk path=/ pool=workloads", calls)
        self.assertEqual(calls[-3:], ["stop ubuntudev --timeout 120", "admin shutdown --timeout 60", "cgroup-stop"][-3:])
        rec = json.loads((self.work / "clean-stop.json").read_text())
        self.assertEqual([i["name"] for i in rec["instances"]], ["ubuntudev"])
        self.assertTrue((self.work / "workloads.json").exists())
        for gone in (self.run_dir / "workloads-status.json", self.run_dir / "workloads-stop", self.run_dir / "workloads-incusd.pid",
                     self.run_dir / "workloads.claim", self.dir / "var/lib/polyseat-lxc-workloads"):
            self.assertFalse(gone.exists(), gone)
        self.assertEqual(oct((self.work).stat().st_mode & 0o777), "0o711")
        # the workspace reopens: the record is consumed by the next start, and the pool it made is not made again
        (self.dir / "calls").unlink()
        (self.dir / "api/nopool").unlink()
        res = self.run_supervisor(self.STOP_WHEN_RUNNING)
        self.assertIn("rc=0", res.stdout, res.stdout + res.stderr)
        self.assertTrue((self.work / "clean-stop.used.json").exists())
        self.assertFalse(any(c.startswith(("storage create", "profile device add")) for c in self.calls()))

    def test_the_running_state_and_the_view_are_published_while_it_runs(self):
        snap = self.dir / "snapshot.json"
        res = self.run_supervisor(f'''
          ( until grep -q '"state":"running"' "$WL_STATUS" 2>/dev/null; do command sleep 0.05; done
            cp "$WL_STATUS" "{snap}"; (cmd_workloads_status > "$D/status-out.json"); : > "$WL_STOPFILE" ) &
        ''')
        self.assertIn("rc=0", res.stdout, res.stdout + res.stderr)
        status = json.loads(snap.read_text())
        self.assertEqual((status["state"], status["policy"]), ("running", {"ok": True, "violations": []}))
        self.assertEqual(status["instances"], [{"name": "ubuntudev", "type": "container", "status": "Running", "autostart": "true",
                                                "ips": ["192.168.178.144"]}])
        self.assertEqual(oct(snap.stat().st_mode & 0o777), "0o600")
        out = json.loads((self.dir / "status-out.json").read_text())
        self.assertEqual((out["state"], out["supervised"], out["enabled"]), ("running", True, False))

    def test_an_offender_that_appears_while_it_runs_is_stopped_and_the_daemon_keeps_running(self):
        bad = good_instance("ubuntudev-test")
        bad["config"]["security.privileged"] = "true"
        self.cfg(WORKLOADS_INSTANCES="ubuntudev ubuntudev-test")
        api = self.dir / "api"
        (self.dir / "bad.json").write_text(json.dumps([good_instance(), bad]))
        res = self.run_supervisor(f'''
          ( until grep -q '"state":"running"' "$WL_STATUS" 2>/dev/null; do command sleep 0.05; done
            cp "$D/bad.json" "$D/api/instances.json"
            until grep -q 'stop ubuntudev-test --force' "$D/calls"; do command sleep 0.05; done
            until grep -q '"ok":false' "$WL_STATUS"; do command sleep 0.05; done
            cp "$WL_STATUS" "$D/offender-status.json"; : > "$WL_STOPFILE" ) &
        ''')
        self.assertIn("rc=0", res.stdout, res.stdout + res.stderr)
        calls = self.calls()
        self.assertIn("stop ubuntudev-test --force --timeout 10", calls)
        self.assertIn("config set ubuntudev-test boot.autostart=false", calls)
        self.assertNotIn("stop ubuntudev --force --timeout 10", calls)
        view = json.loads((self.dir / "offender-status.json").read_text())
        self.assertFalse(view["policy"]["ok"])
        self.assertIn("security.privileged", " ".join(view["policy"]["violations"]))

    def test_a_start_that_never_gets_healthy_stops_what_it_started_and_needs_a_recovery(self):
        res = self.run_supervisor("wl_health_check() { return 1; }; WL_HEALTH_TIMEOUT=1")
        self.assertNotIn("rc=0", res.stdout)
        self.assertIn("did not become healthy", (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())
        self.assertIn("cgroup-stop", self.calls())
        self.assertFalse((self.run_dir / "workloads-incusd.pid").exists())
        self.assertFalse((self.run_dir / "workloads.claim").exists(), "nothing left that could use the shared mount point")
        self.assertEqual(json.loads((self.run_dir / "workloads-status.json").read_text())["state"], "failed")
        self.assertTrue((self.work / "workloads.json").exists())
        self.assertFalse((self.work / "clean-stop.json").exists())
        # the next start refuses at the precheck, before anything is touched
        res = self.bash(f'PTR_CURRENT={SHA}; wl_record_check; echo "rc=$?"', api=False)
        self.assertIn("rc=1", res.stdout)
        self.assertIn("recorded no clean stop", res.stderr)

    def test_a_precheck_that_fails_changes_nothing(self):
        res = self.run_supervisor("wl_precheck() { return 1; }")
        self.assertNotIn("rc=0", res.stdout)
        self.assertIn("before the daemon started; nothing was changed", (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())
        self.assertFalse(self.work.exists(), "no workspace, no record consumed")
        self.assertFalse((self.dir / "cg/polyseat-unraid-workloads").exists())
        self.assertFalse((self.run_dir / "workloads-incusd.pid").exists())
        self.assertEqual([c for c in self.calls() if not c.startswith("cgroup-stop")], [])

    def test_a_module_that_cannot_be_loaded_stops_before_the_record_is_consumed(self):
        (self.work).mkdir(parents=True, mode=0o711)
        self.incus.mkdir(mode=0o711)
        (self.work / "workloads.json").write_text('{"schema": 1, "kind": "workloads", "incus_version": "7.5.1"}')
        (self.work / "clean-stop.json").write_text(json.dumps({"schema": 1, "instances": []}))
        (self.work / "clean-stop.json").chmod(0o600)
        res = self.run_supervisor("wl_modules_load() { return 1; }")
        self.assertNotIn("rc=0", res.stdout)
        self.assertTrue((self.work / "clean-stop.json").exists(), "a refusal before the daemon leaves the record alone")
        self.assertFalse((self.work / "clean-stop.used.json").exists())

    def test_a_daemon_that_dies_on_its_own_leaves_its_containers_alone(self):
        res = self.run_supervisor(r'''
          ( until grep -q '"state":"running"' "$WL_STATUS" 2>/dev/null; do command sleep 0.05; done; kill "$(cat "$WL_PIDFILE")" ) &
        ''')
        self.assertNotIn("rc=0", res.stdout)
        log = (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text()
        self.assertIn("the daemon exited on its own", log)
        self.assertIn("NOT touched", log)
        self.assertNotIn("cgroup-stop", self.calls(), "the group, and with it the containers, is left to the operator")
        self.assertEqual(json.loads((self.run_dir / "workloads-status.json").read_text())["state"], "failed")
        self.assertTrue((self.run_dir / "workloads.claim").exists(), "the shared mount point is still in use")

    def test_adopting_a_stop_without_a_supervisor_runs_the_same_sequence(self):
        (self.work).mkdir(parents=True)
        res = self.run_supervisor(f'''
          command sleep 300 & DAEMON=$!; echo "$DAEMON" > "$WL_PIDFILE"
          wl_daemon_pid() {{ kill -0 "$DAEMON" 2>/dev/null && echo "$DAEMON"; }}
          mkdir -p "$WL_CGROUP"
        ''', mode="adopt")
        self.assertIn("rc=0", res.stdout, res.stdout + res.stderr)
        self.assertEqual([c for c in self.calls() if not c.startswith("query")], ["stop ubuntudev --timeout 120", "admin shutdown --timeout 60", "cgroup-stop"])
        self.assertTrue((self.work / "clean-stop.json").exists())

    def test_it_runs_only_when_started_with_the_lock(self):
        res = self.bash("cmd_workloads_run run", api=False)
        self.assertEqual(res.returncode, 2)
        self.assertIn("started by workloads-start and workloads-stop only", res.stderr)
        res = self.bash("exec 8> \"$WL_LOCKFILE\"; flock -n 8; cmd_workloads_run bogus", api=False)
        self.assertEqual(res.returncode, 2)
        res = self.bash("exec 8> \"$WL_LOCKFILE\"; POLYSEAT_APPROVAL_MODE=signed; cmd_workloads_run run", api=False)
        self.assertEqual(res.returncode, 2)
        self.assertIn("owner-mode build", res.stderr)


class CommandLineTests(Fixture):
    def test_start_refuses_while_another_operation_holds_the_workloads_lock(self):
        res = self.bash('''
          wl_runtime_ready() { echo NOT-REACHED; }
          exec 7> "$WL_LOCKFILE"; flock -n 7
          ( cmd_workloads_start )''', api=False)
        self.assertEqual(res.returncode, 7)
        self.assertIn("already running, or is being started or stopped", res.stderr)
        self.assertNotIn("NOT-REACHED", res.stdout)

    def test_start_refuses_over_a_daemon_or_a_group_of_an_earlier_run(self):
        for pre in ('mkdir -p "$WL_CGROUP"', 'command sleep 8 & echo $! > "$WL_PIDFILE"; incusd_pid() { cat "$1"; }'):
            res = self.bash(f'''wl_runtime_ready() {{ echo NOT-REACHED; }}; {pre}; ( cmd_workloads_start )''', api=False)
            self.assertEqual(res.returncode, 2, pre)
            self.assertIn("workloads-stop clears them", res.stderr)
            self.assertNotIn("NOT-REACHED", res.stdout)

    def test_start_checks_everything_before_it_launches_and_hands_the_lock_to_the_supervisor(self):
        res = self.bash(r'''
          wl_runtime_ready() { echo runtime >> "$D/order"; }
          wl_precheck() { echo precheck >> "$D/order"; }
          wl_launch_detached() { echo "launch $1 $([[ /proc/self/fd/8 -ef $WL_LOCKFILE ]] && echo with-the-lock)" >> "$D/order"; }
          touch "$WL_STOPFILE" "$WL_STATUS"
          cmd_workloads_start''', api=False, check=0)
        self.assertEqual((self.dir / "order").read_text().split("\n")[:3], ["runtime", "precheck", "launch run with-the-lock"])
        self.assertFalse((self.run_dir / "workloads-stop").exists(), "a stale stop request does not end the new run")
        self.assertIn("workloads starting", res.stdout)
        # a failed precheck launches nothing
        (self.dir / "order").unlink()
        res = self.bash(r'''
          wl_runtime_ready() { :; }
          wl_precheck() { return 1; }
          wl_launch_detached() { echo LAUNCHED >> "$D/order"; }
          cmd_workloads_start''', api=False)
        self.assertEqual(res.returncode, 2)
        self.assertFalse((self.dir / "order").exists())

    def test_only_boot_may_wait_for_the_pool(self):
        self.assertEqual(self.bash("cmd_workloads_start --bogus", api=False).returncode, 1)
        self.assertEqual(self.bash("cmd_workloads_start --boot x", api=False).returncode, 1)
        res = self.bash(r'''
          wl_runtime_ready() { :; }; wl_precheck() { :; }; wl_launch_detached() { :; }
          persist_mount_check() { echo probe >> "$D/probes"; [[ $(wc -l < "$D/probes") -ge 3 ]]; }
          cmd_workloads_start --boot''', api=False, check=0)
        self.assertEqual((self.dir / "probes").read_text().split(), ["probe"] * 3)

    def test_stop_with_nothing_running_returns_at_once_and_clears_stale_requests(self):
        res = self.bash('touch "$WL_STOPFILE" "$WL_CLAIM"; cmd_workloads_stop --wait 5', api=False, check=0)
        self.assertFalse((self.run_dir / "workloads-stop").exists())
        self.assertFalse((self.run_dir / "workloads.claim").exists())
        self.assertIn("no workloads daemon is running", (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())

    def test_stop_asks_the_supervisor_and_with_a_wait_holds_until_it_is_gone_but_not_longer_than_asked(self):
        res = self.bash(r'''
          ( exec 8> "$WL_LOCKFILE"; flock -n 8; command sleep 3 ) &
          command sleep 0.3
          cmd_workloads_stop; echo "rc=$?"; [[ -e $WL_STOPFILE ]] && echo FILE
          rm -f "$WL_STOPFILE"
          cmd_workloads_stop --wait 4; echo "bounded=$?"''', api=False)
        self.assertIn("rc=0", res.stdout)
        self.assertIn("FILE", res.stdout)
        self.assertIn("stop requested", res.stdout)
        self.assertIn("bounded=1", res.stdout)
        self.assertIn("still running after 4s", (self.dir / "var/log/polyseat-unraid/polyseat-unraid.log").read_text())
        res = self.bash(r'''
          ( exec 8> "$WL_LOCKFILE"; flock -n 8; command sleep 0.3 ) &
          command sleep 0.1
          cmd_workloads_stop --wait 60; echo "ended=$?"''', api=False)
        self.assertIn("ended=0", res.stdout)

    def test_stop_arguments_are_exactly_a_bounded_wait(self):
        for args in ("--wait", "--wait 0", "--wait 901", "--wait x", "--wait 10 y", "now", "--wait -1"):
            with self.subTest(args=args):
                self.assertEqual(self.bash(f"cmd_workloads_stop {args}", api=False).returncode, 1)

    def test_a_daemon_without_a_supervisor_is_adopted_by_a_new_one_that_holds_the_lock(self):
        res = self.bash(r'''
          mkdir -p "$WL_CGROUP"
          wl_launch_detached() { echo "launch $1 $([[ /proc/self/fd/8 -ef $WL_LOCKFILE ]] && echo with-the-lock)" >> "$D/order"; }
          cmd_workloads_stop; echo "rc=$?"
          flock -n -- "$WL_LOCKFILE" true && echo OUR-FD-IS-CLOSED''', api=False)
        self.assertEqual((self.dir / "order").read_text().split("\n")[0], "launch adopt with-the-lock")
        self.assertIn("OUR-FD-IS-CLOSED", res.stdout, "the caller's own descriptor must not keep the lock")

    def test_the_supervisor_is_launched_detached_with_the_controller_log_and_without_the_controller_lock(self):
        res = self.bash(r'''
          setsid() { printf '%s\n' "$*" >> "$D/setsid"; }
          wl_launch_detached run''', api=False, check=0)
        self.assertRegex((self.dir / "setsid").read_text(), r"^-f /bin/bash \S+/workloads-ctl/scripts/polyseatctl workloads-run run\n$")
        text = CTL.read_text()
        launch = re.search(r"^wl_launch_detached\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn("POLYSEAT_DETACHED=1 setsid -f", launch)
        self.assertIn("9>&-", launch)

    def test_status_names_what_is_there(self):
        def status(pre=""):
            res = self.bash(f'{pre}\ncmd_workloads_status', api=False, check=0)
            return json.loads(res.stdout)
        self.assertEqual(status()["state"], "stopped")
        self.assertEqual(status('echo \'{"state":"failed","note":"start failed"}\' > "$WL_STATUS"')["state"], "failed")
        self.assertEqual(status('mkdir -p "$WL_CGROUP"')["state"], "orphaned")
        shutil.rmtree(self.dir / "cg")
        # a supervisor holds the lock: what it last wrote is believed (starting if it wrote nothing yet)
        held = 'exec 7> "$WL_LOCKFILE"; flock -n 7; '
        self.assertEqual(status(held)["state"], "starting")
        self.assertEqual(status(held + 'echo \'{"state":"running","policy":{"ok":false,"violations":["x"]},"instances":[{"name":"a"}]}\' > "$WL_STATUS"')["policy"]["ok"], False)
        self.assertEqual(status(held + 'echo \'{"state":"nonsense"}\' > "$WL_STATUS"')["state"], "starting")
        self.assertEqual(status(held + 'echo \'{"state":"stopping"}\' > "$WL_STATUS"')["state"], "stopping")
        self.cfg(WORKLOADS_ENABLED="yes", WORKLOADS_INSTANCES="x")
        self.assertTrue(status()["enabled"])
        self.assertIsNone(status()["clean_stop_recorded"])
        self.work.mkdir(parents=True)
        self.assertIs(status()["clean_stop_recorded"], False)
        (self.work / "clean-stop.json").write_text("{}")
        self.assertIs(status()["clean_stop_recorded"], True)
        (self.run_dir / "workloads-status.json").write_text("not json")
        self.assertEqual(status()["state"], "stopped")


class GateAndDispatchTests(unittest.TestCase):
    def gate(self, mode, gate, cmd, *extra):
        script = ('POLYSEAT_APPROVAL_MODE=$1 POLYSEAT_RELEASE_GATE=$2 ROOT=""; id() { printf "0\\n"; }; '
                  'canary_approval_for() { echo APPROVAL-CHECKED; }; shift 2; require_mutation_allowed "$@"; echo ALLOWED')
        return source_ctl(script, mode, gate, cmd, *extra)

    def test_the_workloads_verbs_exist_in_owner_builds_only_and_need_no_token(self):
        for cmd in ("workloads-start", "workloads-stop", "workloads-status", "workloads-recover", "workloads-run"):
            with self.subTest(cmd=cmd):
                res = self.gate("owner", "canary", cmd)
                self.assertIn("ALLOWED", res.stdout)
                self.assertNotIn("APPROVAL-CHECKED", res.stdout)
                res = self.gate("signed", "canary", cmd)
                self.assertEqual(res.returncode, 2)
                self.assertNotIn("ALLOWED", res.stdout)
                self.assertIn("owner-mode build", res.stderr)
                for gate in ("closed", "test-host", "open"):
                    self.assertEqual(self.gate("owner", gate, cmd).returncode, 2, (cmd, gate))

    def test_the_workloads_settings_pass_the_owner_whitelist_with_the_schemas_values_only(self):
        script = ('POLYSEAT_APPROVAL_MODE=owner POLYSEAT_RELEASE_GATE=canary ROOT=""; id() { printf "0\\n"; }; '
                  'SCHEMA=$1; shift; require_mutation_allowed config-set "$@"; echo ALLOWED')
        schema = str(CTL.parent.parent / "schema.json")
        for good in (["WORKLOADS_ENABLED=yes"], ["WORKLOADS_INSTANCES=ubuntudev"], ["WORKLOADS_MEMORY_MB=28672"], ["WORKLOADS_CPUS=0-15"],
                     ["WORKLOADS_ENABLED=no", "WORKLOADS_INSTANCES="]):
            with self.subTest(good=good):
                self.assertIn("ALLOWED", source_ctl(script, schema, *good).stdout)
        for bad in (["WORKLOADS_ENABLED=maybe"], ["WORKLOADS_INSTANCES=Bad Name"], ["WORKLOADS_MEMORY_MB=lots"], ["WORKLOADS_CPUS=0-"],
                    ["WORKLOADS_CPUS=a;b"], ["WORKLOADS_PRIVILEGED=yes"], ["WORKLOADS_ENABLED=yes", "SERVICE=enabled"]):
            with self.subTest(bad=bad):
                res = source_ctl(script, schema, *bad)
                self.assertEqual(res.returncode, 2, res.stdout)
                self.assertNotIn("ALLOWED", res.stdout)

    def test_the_verbs_are_dispatched_before_any_lock_so_a_play_session_never_queues_them(self):
        main = CTL.read_text().split("\nmain() {", 1)[1]
        early = main[:main.index("  acquire_lock\n")]
        for line in ("workloads-status) cmd_workloads_status; return ;;", 'workloads-start) cmd_workloads_start "${args[@]}"; return ;;',
                     'workloads-stop) cmd_workloads_stop "${args[@]}"; return ;;', "workloads-recover) cmd_workloads_recover; return ;;",
                     'workloads-run) cmd_workloads_run "${args[@]}"; return ;;'):
            self.assertIn(line, early)
        late = main[main.index("  acquire_lock\n"):]
        self.assertIn('die $EX_BLOCKED "the workloads daemon needs an owner-mode canary build"', late)
        for verb in ("workloads-start", "workloads-stop", "workloads-status", "workloads-recover", "workloads-run"):
            self.assertIn(verb, main.split("require_mutation_allowed", 1)[0])

    def test_a_controller_without_the_owner_canary_build_refuses_every_verb_on_the_command_line(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        for verb in ("workloads-start", "workloads-stop", "workloads-status", "workloads-recover", "workloads-run"):
            with self.subTest(verb=verb):
                res = sb.ctl(verb, "--approve")
                self.assertEqual(res.code, 2, res.text)
                self.assertIn("owner-mode canary build", res.text)
        self.assertEqual(sb.ctl("workloads-status").code, 2)


class LockAndRuntimeTests(Fixture):
    def guard(self, pre=""):
        return self.bash(f'{pre}\n( wl_runtime_change_guard; echo FREE )', api=False)

    def test_a_runtime_change_is_refused_while_the_daemon_runs_or_changes_state(self):
        self.assertIn("FREE", self.guard().stdout)
        res = self.guard('exec 7> "$WL_LOCKFILE"; flock -n 7')
        self.assertEqual(res.returncode, 7)
        self.assertIn("stop it first (workloads-stop)", res.stderr)
        res = self.guard('mkdir -p "$WL_CGROUP"')
        self.assertEqual(res.returncode, 2)
        self.assertIn("workloads processes are still there", res.stderr)
        res = self.guard('command sleep 8 & echo $! > "$WL_PIDFILE"; incusd_pid() { cat "$1"; }')
        self.assertEqual(res.returncode, 2)

    def test_the_guard_holds_the_workloads_lock_for_the_rest_of_the_operation(self):
        res = self.bash(r'''
          ( wl_runtime_change_guard
            flock -n -- "$WL_LOCKFILE" true && echo STARTABLE || echo WORKLOADS-START-WAITS )''', api=False)
        self.assertIn("WORKLOADS-START-WAITS", res.stdout)

    def test_activation_rollback_recovery_and_uninstall_each_take_the_guard_before_they_change_the_slot(self):
        text = CTL.read_text()
        for name, anchor in (("cmd_activate", "materialize_slot"), ("cmd_rollback", "write_journal rolling-back"),
                             ("cmd_recover", "restore_previous"), ("cmd_uninstall", "svc_stop full")):
            body = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.M | re.S).group(0)
            self.assertIn("wl_runtime_change_guard", body, name)
            self.assertLess(body.index("wl_runtime_change_guard"), body.index(anchor), name)

    def test_the_guards_lock_never_leaks_into_a_daemon_the_same_process_starts(self):
        # activate holds descriptor 8 (the workloads lock) and may start the daemon afterwards (restore_previous, svc_start):
        # a daemon that inherited it would keep every later activation refused for as long as it lives
        text = CTL.read_text()
        start = re.search(r"^svc_start\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn('> "$LOG_DIR/incusd.out" 2>&1 < /dev/null 9>&- 8>&-', start)
        self.assertIn('canary_launch "$slot_real" > "$LOG_DIR/incusd.out" 2>&1 < /dev/null', start)  # ... which closes 9 and 8 itself
        launch = re.search(r"^canary_launch\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn('9>&- 8>&-', launch)
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        first, second = sb.make_bundle("1.0.0"), sb.make_bundle("1.1.0")
        self.assertEqual(sb.install(first).code, 0)
        self.assertEqual(sb.ctl("start").code, 0, "a service that outlives the activation")
        self.addCleanup(sb.ctl, "stop")
        self.assertEqual(sb.stage(second).code, 0)
        self.assertEqual(sb.ctl("activate", second["sha"], "--approve").code, 0)
        self.assertEqual(sb.ctl("activate", first["sha"], "--approve").code, 0, "the first activation left no lock behind")

    def test_activation_on_the_command_line_is_refused_over_a_running_workloads_daemon(self):
        sb = Sandbox()
        self.addCleanup(sb.cleanup)
        first, second = sb.make_bundle("1.0.0"), sb.make_bundle("1.1.0")
        self.assertEqual(sb.install(first).code, 0)
        self.assertEqual(sb.stage(second).code, 0)
        sb.run_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(sb.run_dir / "workloads.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = sb.ctl("activate", second["sha"], "--approve")
        self.assertNotEqual(res.code, 0)
        self.assertIn("workloads", res.text)
        self.assertEqual(sb.pointer()["current"], first["sha"])
        self.assertEqual(sb.ctl("rollback", "--approve").code != 0, True)
        self.assertEqual(sb.ctl("uninstall", "--approve").code, 7, "nothing removed while it runs")
        self.assertEqual(sb.pointer()["current"], first["sha"])
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(sb.ctl("activate", second["sha"], "--approve").code, 0)
        self.assertEqual(sb.pointer()["current"], second["sha"])

    def test_the_slot_is_rebuilt_after_a_reboot_under_the_controller_lock_and_only_then(self):
        res = self.bash(r'''
          verify_bundle_files() { return 0; }; check_host_compat() { return 0; }; bundle_manifest() { echo m; }
          ensure_state_dirs() { :; }; require_no_journal() { :; }
          read_pointer() { PTR_CURRENT=aaaa; }
          materialize_slot() { echo "materialize $1" >> "$D/order"; }
          switch_current() { echo "switch $1" >> "$D/order"; }
          verify_slot() { return 1; }
          wl_runtime_ready; echo "rc=$?"''', api=False)
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertEqual((self.dir / "order").read_text().split("\n")[:2], ["materialize aaaa", "switch aaaa"])
        (self.dir / "order").unlink()
        # a play session holds the lock and has the slot: nothing is rebuilt, nothing waits
        (self.dir / "usr/local/polyseat-unraid").mkdir(parents=True)
        (self.dir / "usr/local/polyseat-unraid/current").symlink_to("slots/" + "a" * 64)
        res = self.bash(rf'''
          verify_bundle_files() {{ return 0; }}; check_host_compat() {{ return 0; }}; bundle_manifest() {{ echo m; }}
          ensure_state_dirs() {{ :; }}; require_no_journal() {{ :; }}
          read_pointer() {{ PTR_CURRENT={SHA}; }}
          materialize_slot() {{ echo MATERIALIZED >> "$D/order"; }}
          verify_slot() {{ return 0; }}
          exec 7> "$LOCKFILE"; flock -n 7
          wl_runtime_ready; echo "rc=$?"''', api=False)
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertFalse((self.dir / "order").exists())

    def test_a_bundle_that_does_not_verify_or_does_not_exist_is_not_used(self):
        for pre, expect in (("read_pointer() { PTR_CURRENT=\"\"; }", "no runtime bundle is active"),
                            ("verify_bundle_files() { return 1; }", "failed verification")):
            res = self.bash(f'''
              verify_bundle_files() {{ return 0; }}; check_host_compat() {{ return 0; }}; bundle_manifest() {{ echo m; }}
              ensure_state_dirs() {{ :; }}; require_no_journal() {{ :; }}
              read_pointer() {{ PTR_CURRENT=aaaa; }}
              {pre}
              wl_runtime_ready; echo "rc=$?"''', api=False)
            self.assertIn("rc=1", res.stdout)
            self.assertIn(expect, res.stderr)


class SettingsTests(Fixture):
    def config_set(self, pre, *pairs):
        return self.bash(f'''
          {pre}
          cmd_config_set {" ".join(pairs)}; echo "rc=$?"''', api=False)

    def test_the_memory_and_cpu_settings_cannot_change_while_the_daemon_runs_but_the_list_and_the_flag_can(self):
        running = "wl_running() { return 0; }"
        for pair in ("WORKLOADS_MEMORY_MB=1024", "WORKLOADS_CPUS=0-7"):
            res = self.config_set(running, pair)
            self.assertNotIn("rc=0", res.stdout, pair)
            self.assertIn("cannot change while the workloads daemon runs", res.stderr)
        for pair in ("WORKLOADS_INSTANCES=ubuntudev", "WORKLOADS_ENABLED=yes"):
            res = self.config_set(running, pair)
            self.assertIn("rc=0", res.stdout, res.stderr)
        # not while play runs: a play session does not lock the workloads settings, and the other way round
        res = self.config_set("wl_running() { return 1; }; svc_pid() { echo 1; }", "WORKLOADS_MEMORY_MB=2048")
        self.assertIn("rc=0", res.stdout, res.stderr)
        res = self.config_set("wl_running() { return 0; }; svc_pid() { return 1; }", "PLAY_MEMORY_MB=2048")
        self.assertIn("rc=0", res.stdout, res.stderr)
        res = self.config_set("wl_running() { return 1; }; svc_pid() { echo 1; }", "PLAY_MEMORY_MB=4096")
        self.assertNotIn("rc=0", res.stdout)

    def test_a_reset_is_refused_while_the_daemon_runs_because_an_empty_list_would_stop_every_instance(self):
        res = self.bash('wl_running() { return 0; }; cmd_config_reset; echo "rc=$?"', api=False)
        self.assertNotIn("rc=0", res.stdout)
        self.assertIn("stop the workloads daemon before resetting settings", res.stderr)
        self.cfg(WORKLOADS_INSTANCES="ubuntudev")
        res = self.bash('wl_running() { return 1; }; svc_pid() { return 1; }; cmd_config_reset; echo "rc=$?"', api=False)
        self.assertIn("rc=0", res.stdout, res.stderr)

    def test_cpus_the_host_does_not_have_are_refused_when_saved(self):
        res = self.config_set("wl_running() { return 1; }", "WORKLOADS_CPUS=0-31")
        self.assertNotIn("rc=0", res.stdout)
        self.assertIn("names CPUs this host does not have", res.stderr)
        res = self.config_set("wl_running() { return 1; }", "WORKLOADS_CPUS=0-7,16-19")
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertIn('WORKLOADS_CPUS="0-7,16-19"', (self.flash / "polyseat-unraid.cfg").read_text())

    def test_the_schema_entries(self):
        schema = json.loads((CTL.parent.parent / "schema.json").read_text())
        self.assertEqual(schema["WORKLOADS_ENABLED"]["default"], "no", "the daemon never starts by itself unless the owner says so")
        self.assertEqual(schema["WORKLOADS_ENABLED"]["values"], ["yes", "no"])
        self.assertEqual(schema["WORKLOADS_INSTANCES"]["default"], "")
        self.assertEqual(schema["WORKLOADS_MEMORY_MB"]["default"], "0", "0 = unlimited")
        self.assertEqual(schema["WORKLOADS_CPUS"]["default"], "")
        for key in ("WORKLOADS_ENABLED", "WORKLOADS_INSTANCES", "WORKLOADS_MEMORY_MB", "WORKLOADS_CPUS"):
            self.assertEqual(schema[key]["tab"], "workloads")
        self.assertTrue(schema["WORKLOADS_MEMORY_MB"]["locked_while_running"] and schema["WORKLOADS_CPUS"]["locked_while_running"])
        self.assertFalse(schema["WORKLOADS_INSTANCES"].get("locked_while_running") or schema["WORKLOADS_ENABLED"].get("locked_while_running"))

    def test_the_instance_list_is_read_strictly(self):
        for value, expect in (("ubuntudev", ["ubuntudev"]), ("a b c", ["a", "b", "c"]), ("", [])):
            self.cfg(WORKLOADS_INSTANCES=value)
            res = self.bash("wl_instances_setting; echo rc=$?", api=False)
            self.assertEqual(res.stdout.split(), expect + ["rc=0"], value)
        for value in ("a a", "A", "a  b", "a;b", " a", "a,b"):
            self.cfg(WORKLOADS_INSTANCES=value)
            self.assertIn("rc=1", self.bash("wl_instances_setting; echo rc=$?", api=False).stdout, value)


class BootTests(Fixture):
    def boot(self, enabled):
        if enabled is not None:
            self.cfg(WORKLOADS_ENABLED=enabled)
        (self.flash / "state").mkdir(exist_ok=True)
        return self.bash(r'''
          ensure_state_dirs() { :; }
          read_journal() { JR_PHASE=""; }
          read_pointer() { PTR_CURRENT=aaaa; }
          verify_bundle_files() { return 0; }
          workloads_boot() { echo WORKLOADS-BOOT >> "$D/order"; }
          cmd_boot_owner; echo "rc=$?"''', api=False)

    def test_boot_starts_the_workloads_daemon_only_when_enabled_and_never_a_play_session(self):
        res = self.boot("yes")
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertEqual((self.dir / "order").read_text().split(), ["WORKLOADS-BOOT"])
        (self.dir / "order").unlink()
        for enabled in ("no", None):
            self.boot(enabled)
            self.assertFalse((self.dir / "order").exists(), enabled)
        body = re.search(r"^cmd_boot_owner\(\) \{.*?^\}", CTL.read_text(), re.M | re.S).group(0)
        for forbidden in ("svc_start", "play-start", "cmd_play_start", "setsid", "cmd_activate", "polyseatd-play", "workloads-start"):
            self.assertNotIn(forbidden, body)

    def test_the_boot_start_is_detached_and_asks_the_command_to_wait_for_the_pool(self):
        res = self.bash(r'''
          setsid() { printf '%s\n' "$*" >> "$D/setsid"; }
          workloads_boot; echo "rc=$?"''', api=False)
        self.assertIn("rc=0", res.stdout, res.stderr)
        self.assertRegex((self.dir / "setsid").read_text(), r"^-f \S+polyseatctl workloads-start --boot\n$")
        text = CTL.read_text()
        boot = re.search(r"^workloads_boot\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn("9>&-", boot)  # the controller lock stays with boot, not with the daemon's supervisor
        self.assertNotIn("workloads-run", boot)
        base = CTL.parent.parent / "event"
        mounted = (base / "disks_mounted").read_text()
        self.assertNotIn("workloads", "\n".join(l for l in mounted.splitlines() if not l.lstrip().startswith("#")))

    def test_the_stop_event_stops_the_workloads_daemon_alongside_the_play_session_and_waits_for_it(self):
        base = CTL.parent.parent / "event"
        stopping = [l for l in (base / "stopping_svcs").read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertIn('"$CTL" workloads-stop --wait 600 > /dev/null 2>&1 &', stopping)
        self.assertIn('wait "$workloads"', stopping)
        self.assertLess(stopping.index('"$CTL" workloads-stop --wait 600 > /dev/null 2>&1 &'), stopping.index('"$CTL" play-stop --wait 300 > /dev/null 2>&1'))
        self.assertLess(stopping.index('"$CTL" stop > /dev/null 2>&1'), stopping.index('wait "$workloads"'))


class CoexistenceTests(Fixture):
    """The play session's daemon keeps running, unharmed, next to the workloads one."""

    def test_every_per_instance_path_differs_and_only_the_deliberately_shared_ones_are_equal(self):
        body = r'''
          POLYSEAT_ROOT=$D; init_paths
          for v in CANARY_CGROUP PIDFILE INCUS_CLIENT_DIR INCUSD_LOG CANARY_DENY_DIR LXC_HOOK_DIR INCUS_DIR DATA_REAL RUN_DIR LOG_DIR SLOTS CURRENT; do
            printf 'play %s %s\n' "$v" "${!v}"
          done
          LXC_HOOK_DIR=$LXC_STATE_PARENT/hooks
          wl_enter
          for v in CANARY_CGROUP PIDFILE INCUS_CLIENT_DIR INCUSD_LOG CANARY_DENY_DIR LXC_HOOK_DIR INCUS_DIR DATA_REAL RUN_DIR LOG_DIR SLOTS CURRENT; do
            printf 'wl %s %s\n' "$v" "${!v}"
          done
          printf 'play LOCK %s\nwl LOCK %s\n' "$LOCKFILE" "$WL_LOCKFILE"
          printf 'play LXC %s\nwl LXC %s\n' "$LXC_STATE_PARENT" "$WL_LXC_STATE"
        '''
        out = source_ctl("D=$1; shift\n" + body, str(self.dir)).stdout
        values = {"play": {}, "wl": {}}
        for line in out.splitlines():
            who, name, value = line.split(" ", 2)
            values[who][name] = value
        shared = {"RUN_DIR", "LOG_DIR", "SLOTS", "CURRENT"}  # the runtime slot and the directories both live in
        for name, value in values["play"].items():
            if name in shared:
                self.assertEqual(value, values["wl"][name], name)
            else:
                self.assertNotEqual(value, values["wl"][name], name)
        self.assertNotEqual(values["play"]["INCUS_DIR"], values["wl"]["INCUS_DIR"])
        self.assertTrue(values["wl"]["INCUS_DIR"].endswith("/mnt/cache/polyseat/workloads/incus"))
        self.assertTrue(values["play"]["INCUS_DIR"].endswith("/var/lib/polyseat-unraid-canary/incus") or values["play"]["INCUS_DIR"] == "")

    def fake_proc(self, entries):
        proc = self.dir / "proc"
        shutil.rmtree(proc, ignore_errors=True)
        for pid, (exe, cgroup) in entries.items():
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "cgroup").write_text(cgroup + "\n")
            (proc / str(pid) / "cmdline").write_bytes(b"x\0")
            if exe:
                (proc / str(pid) / "exe").symlink_to(exe)
        return proc

    def leak(self, entries, who, environ=None):
        proc = self.fake_proc(entries)
        for pid, text in (environ or {}).items():
            (proc / str(pid) / "environ").write_bytes(text.encode())
        # play's process never calls wl_enter: it has play's group, the default CANARY_OTHER_CGROUP and operation 8
        enter = "wl_enter" if who == "wl" else "CANARY_OPERATION=8 CANARY_CGROUP=/sys/fs/cgroup/polyseat-unraid-canary CANARY_OTHER_CGROUP=polyseat-unraid-workloads"
        res = self.bash(f'{enter}; CANARY_PROC={proc}; canary_leak_check; echo "rc=$?"', api=False)
        return res

    def test_the_leak_check_of_one_daemon_ignores_the_other_ones_processes_and_still_finds_its_own(self):
        slots = f"{self.dir}/usr/local/polyseat-unraid/slots/{SHA}/payload/bin"
        wl = {"1001": (f"{slots}/incusd", "0::/polyseat-unraid-workloads/daemon/lxc.monitor.ubuntudev")}
        play = {"1002": (f"{slots}/incusd", "0::/polyseat-unraid-canary/daemon")}
        play_watchdog = {"1003": (None, "0::/polyseat-unraid-canary-watchdog")}
        # play stopping: the workloads daemon's processes are not play's leftovers
        self.assertIn("rc=0", self.leak(wl, "play").stdout)
        # workloads stopping: play's group and watchdog are not ours
        self.assertIn("rc=0", self.leak(play, "wl").stdout)
        self.assertIn("rc=0", self.leak(play_watchdog, "wl").stdout)
        # a leftover of our own is a leak
        self.assertIn("rc=1", self.leak(play, "play").stdout)
        self.assertIn("rc=1", self.leak(wl, "wl").stdout)
        # outside both groups it is a leak for either
        stray = {"1004": (f"{slots}/incusd", "0::/")}
        self.assertIn("rc=1", self.leak(stray, "play").stdout)
        self.assertIn("rc=1", self.leak(stray, "wl").stdout)
        # the operator's client against the permanent daemon is no leftover; for play it still is
        client = {"1005": (f"{slots}/incus", "0::/user.slice/session-4.scope")}
        self.assertIn("rc=0", self.leak(client, "wl").stdout)
        self.assertIn("rc=1", self.leak(client, "play").stdout, "a client of no known daemon is still a leak for play")
        # ... and for play only while it talks to the workloads workspace (an operator's shell during a play stop)
        workspace = f"INCUS_DIR={self.dir}/mnt/cache/polyseat/workloads/incus\0PATH=/bin\0"
        self.assertIn("rc=0", self.leak(client, "play", {1005: workspace}).stdout)
        other = f"INCUS_DIR={self.dir}/mnt/cache/polyseat/incus/incus\0"
        self.assertIn("rc=1", self.leak(client, "play", {1005: other}).stdout)

    def test_the_workloads_teardown_never_touches_play_s_groups_even_while_a_play_session_runs(self):
        for name in ("polyseat-unraid-canary", "polyseat-unraid-canary-watchdog", "polyseat-unraid-workloads"):
            (self.dir / "cg" / name / "daemon").mkdir(parents=True)
        res = self.bash(r'''
          fs_type() { echo cgroup2fs; }
          kill_files=0
          canary_cgroup_stop; echo "rc=$?"
          canary_cgroup_remove "$D/cg/polyseat-unraid-canary" && echo PLAY-GROUP-REMOVED || echo PLAY-GROUP-REFUSED
          canary_cgroup_remove "$D/cg/polyseat-unraid-canary-watchdog" && echo WATCHDOG-REMOVED || echo WATCHDOG-REFUSED''', api=False)
        self.assertIn("PLAY-GROUP-REFUSED", res.stdout)
        self.assertIn("WATCHDOG-REFUSED", res.stdout, "the shared remover only accepts this process's own groups")
        for name in ("polyseat-unraid-canary", "polyseat-unraid-canary-watchdog"):
            self.assertEqual(sorted(p.name for p in (self.dir / "cg" / name).iterdir()), ["daemon"], f"{name} was not written to (no cgroup.kill)")
        self.assertTrue((self.dir / "cg/polyseat-unraid-workloads/cgroup.kill").exists(), "its own group is the one that gets the kill")

    def test_a_play_stop_leaves_the_shared_lxc_mount_point_while_the_workloads_claim_stands(self):
        res = self.bash(r'''
          wl_enter
          LXC_STATE_PARENT=$D/var/lib/polyseat-lxc
          mkdir -p "$LXC_STATE_PARENT/rootfs" "$LXC_STATE_PARENT/root"
          lxc_mount_real() { printf '%s\n' "$1"; }
          LXC_STATE_PARENT=$D/var/lib/polyseat-lxc CANARY_DENY_DIR=$D/var/lib/polyseat-lxc/deny-tools
          : > "$WL_CLAIM"
          remove_lxc_rootfs_mount
          [[ -d $LXC_STATE_PARENT/rootfs ]] && echo ROOTFS-KEPT
          [[ -d $LXC_STATE_PARENT/root ]] || echo PLAY-ROOT-GONE
          rm -f "$WL_CLAIM"
          remove_lxc_rootfs_mount
          [[ -d $LXC_STATE_PARENT/rootfs ]] || echo ROOTFS-REMOVED-WITHOUT-CLAIM''', api=False, check=0)
        self.assertEqual(res.stdout.split(), ["ROOTFS-KEPT", "PLAY-ROOT-GONE", "ROOTFS-REMOVED-WITHOUT-CLAIM"])

    def test_claim_and_release_use_the_one_lock_a_play_stop_takes(self):
        res = self.bash(r'''
          lxc_mount_real() { printf '%s\n' "$D$1"; }
          echo '{"lxc_rootfs_mount": "/var/lib/polyseat-lxc/rootfs"}' > "$D/m.json"
          wl_claim "$D/m.json"; echo "claim=$?"
          [[ -e $WL_CLAIM && -d $D/var/lib/polyseat-lxc/rootfs ]] && echo CLAIMED
          [[ -e $WL_SHARED_LOCK ]] && echo LOCKFILE
          wl_claim_release
          [[ -e $WL_CLAIM ]] || echo RELEASED''', api=False, check=0)
        self.assertEqual(res.stdout.split(), ["claim=0", "CLAIMED", "LOCKFILE", "RELEASED"])

    def test_the_workloads_state_directory_is_made_and_removed_exactly(self):
        res = self.bash(r'''
          lxc_mount_real() { printf '%s\n' "$D$1"; }
          wl_lxc_prepare; echo "prepare=$?"
          CANARY_DENY_DIR=$WL_LXC_STATE/deny-tools
          canary_deny_tools_prepare; echo "deny=$?"
          ls "$D$WL_LXC_STATE" | tr '\n' ' '; echo
          ls "$D$WL_LXC_STATE/deny-tools" | tr '\n' ' '; echo
          printf 'keep\n' > "$D$WL_LXC_STATE/hooks/foreign"
          wl_lxc_remove
          [[ -d $D$WL_LXC_STATE ]] && echo KEPT-BECAUSE-NOT-EMPTY
          rm "$D$WL_LXC_STATE/hooks/foreign"; wl_lxc_remove
          [[ -e $D$WL_LXC_STATE ]] || echo GONE''', api=False, check=0)
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0:2], ["prepare=0", "deny=0"], res.stderr)
        self.assertEqual(lines[2].split(), ["deny-tools", "hooks", "root"])
        self.assertEqual(lines[3].split(), sorted(["btrfs", "insmod", "lvm", "modprobe", "nft", "qemu-system-x86_64", "rmmod", "zfs", "zpool"]))
        self.assertEqual(lines[4:], ["KEPT-BECAUSE-NOT-EMPTY", "GONE"])
        # the play side was never touched
        self.assertFalse((self.dir / "var/lib/polyseat-lxc").exists())

    def test_the_launcher_serves_both_daemons_and_the_permanent_one_has_no_watchdog_no_nice_and_no_inherited_lock(self):
        text = CTL.read_text()
        body = re.search(r"^canary_launch\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn('[[ $CANARY_OPERATION -eq 9 ]] && switch_root=$(lxc_mount_real "$WL_LXC_STATE/root")', body)
        self.assertIn("if [[ $CANARY_OPERATION -ne 9 ]]; then\n    [[ ! -e $RUN_DIR/canary-emergency-stop ]] && canary_watchdog_check || return 1\n  fi", body)
        self.assertIn("if [[ $CANARY_OPERATION -ne 8 && $CANARY_OPERATION -ne 9 ]]; then renice", body)
        self.assertIn('INCUS_CONF="$INCUS_CLIENT_DIR"', body)
        self.assertIn('--logfile "$INCUSD_LOG" 9>&- 8>&-', body)
        # play's own settings are unchanged where they were: the same switch for operation 8
        self.assertIn('[[ $CANARY_OPERATION -eq 8 ]] && switch_root=$(lxc_mount_real "$LXC_STATE_PARENT/root")', body)
        # the supervisor starts it detached from its own descriptors and its output goes to the workloads files
        launch = re.search(r"^wl_launch\(\) \{.*?^\}", text, re.M | re.S).group(0)
        self.assertIn('canary_launch "$1" >> "$WL_OUT" 2>&1 < /dev/null', launch)

    def test_the_workloads_supervisor_never_touches_play_files_or_the_play_cgroup(self):
        text = CTL.read_text()
        start = text.index("# -------------------------------------------------------------- workloads ----")
        end = text.index("# --------------------------------------------------------- transaction ----")
        block = text[start:end]
        code = "\n".join(l for l in block.splitlines() if not l.lstrip().startswith("#"))
        # the one mention of the watchdog group points it at the daemon's own group (see the test above)
        self.assertEqual(re.findall(r"^\s*CANARY_WATCHDOG_CGROUP.*$", code, re.M), ["  CANARY_WATCHDOG_CGROUP=$WL_CGROUP"])
        code = code.replace("CANARY_WATCHDOG_CGROUP=$WL_CGROUP", "")
        for play_only in ("CANARY_SESSION", "canary_watchdog", "canary_guardian", "play_host", "polyseatd", "owner-approval",
                          "cgroup.kill", "memory.oom.group", "memory.swap.max", "pids.max", "canary-emergency-stop", "GPU_", "gpu_"):
            self.assertNotIn(play_only, code, play_only)
        # the group is killed in two places only: a stop sequence that is over (its last step) and a start that failed
        stoppers = set()
        for m in re.finditer(r"^(\w+)\(\)[^\n]*\{.*?^\}", block, re.M | re.S):
            body = "\n".join(l for l in m.group(0).splitlines()[1:] if not l.lstrip().startswith("#"))
            if "canary_cgroup_stop" in body:
                stoppers.add(m.group(1))
        self.assertEqual(sorted(stoppers), ["wl_run_exit", "wl_stop_sequence"])


if __name__ == "__main__":
    unittest.main()
