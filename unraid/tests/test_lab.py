"""Entirely offline safety rehearsals. No Unraid host, VM or GPU is accessed."""

from __future__ import annotations

import hashlib
import io
import tarfile
import unittest
from copy import deepcopy

from unraid.lab.archive import RejectedArchive, verify
from unraid.lab.guards import Blocked, propose
from unraid.lab.lifecycle import State, TransitionDenied, transition
from unraid.scripts.make_proof_sbom import create as make_source_inventory


GPU_A = "0000:01:00.0"  # illustrative PCI ID, not a discovered device
GPU_B = "0000:02:00.0"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def fixture() -> dict:
    return {
        "source": "offline-fixture",
        "kernel": "fixture-kernel",
        "unraid_version": "fixture-7.2",
        "cgroup_v2": True,
        "docker_running": True,
        "libvirt_running": True,
        "vms": [{"name": "Gaming VM", "running": True, "assigned_gpus": [GPU_A]}],
        "gpus": [
            {"pci": GPU_A, "driver": "vfio-pci", "iommu_group_exclusive": True,
             "nvenc_capable": True},
            {"pci": GPU_B, "driver": "nvidia", "iommu_group_exclusive": True,
             "nvenc_capable": True},
        ],
    }


class GPUSafetyTests(unittest.TestCase):
    def test_active_gaming_vm_preserved_and_gpu_fails_closed(self):
        facts = fixture()
        before = deepcopy(facts)
        with self.assertRaisesRegex(Blocked, "vfio-pci"):
            propose(facts, {"gpu_pci": GPU_A, "seats": 1,
                            "network_mode": "isolated-no-uplink"})
        self.assertEqual(facts, before)

    def test_other_dedicated_gpu_can_only_generate_offline_plan(self):
        plan = propose(fixture(), {"gpu_pci": GPU_B, "seats": 2,
                                   "network_mode": "isolated-no-uplink"})
        self.assertEqual(plan["classification"], "OFFLINE-PROPOSAL-ONLY")
        self.assertFalse(plan["installable"])
        self.assertEqual(plan["host_commands"], [])
        self.assertIn("VM", plan["forbidden_mutations"])

    def test_unknown_driver_and_unknown_vm_assignments_block(self):
        facts = fixture()
        facts["gpus"][1]["driver"] = "unknown"
        with self.assertRaises(Blocked):
            propose(facts, {"gpu_pci": GPU_B, "seats": 1,
                            "network_mode": "isolated-no-uplink"})
        facts = fixture()
        del facts["vms"][0]["assigned_gpus"]
        with self.assertRaises(Blocked):
            propose(facts, {"gpu_pci": GPU_B, "seats": 1,
                            "network_mode": "isolated-no-uplink"})

    def test_no_bridge_nat_or_host_network_allowed(self):
        for mode in ("br0", "host", "nat", "macvlan", "bridged", None):
            with self.subTest(mode=mode), self.assertRaises(Blocked):
                propose(fixture(), {"gpu_pci": GPU_B, "seats": 1,
                                    "network_mode": mode})

    def test_unknown_vm_and_iommu_gate_block(self):
        facts = fixture()
        facts["vms"][0]["running"] = None
        with self.assertRaises(Blocked):
            propose(facts, {"gpu_pci": GPU_B, "seats": 1,
                            "network_mode": "isolated-no-uplink"})
        facts = fixture()
        facts["gpus"][1]["iommu_group_exclusive"] = False
        with self.assertRaises(Blocked):
            propose(facts, {"gpu_pci": GPU_B, "seats": 1,
                            "network_mode": "isolated-no-uplink"})

    def test_not_a_real_host_probe_and_seat_limit(self):
        facts = fixture()
        facts["source"] = "live-unraid"
        with self.assertRaises(Blocked):
            propose(facts, {"gpu_pci": GPU_B, "seats": 1,
                            "network_mode": "isolated-no-uplink"})
        for count in (0, 3, True):
            with self.subTest(count=count), self.assertRaises(Blocked):
                propose(fixture(), {"gpu_pci": GPU_B, "seats": count,
                                    "network_mode": "isolated-no-uplink"})


class LifecycleTests(unittest.TestCase):
    def test_first_install_failure_rolls_back_to_absent_without_data_change(self):
        original = State(external_fingerprint="game-library-hash")
        staged = transition(original, "stage", bundle=DIGEST_A)
        with self.assertRaises(TransitionDenied):
            transition(staged, "activate")
        active = transition(staged, "activate", approved=True)
        failed = transition(active, "mark_failed")
        reverted = transition(failed, "rollback", approved=True)
        self.assertEqual(reverted.phase, "ABSENT")
        self.assertEqual(reverted.external_fingerprint, original.external_fingerprint)

    def test_failed_upgrade_restores_prior_bundle(self):
        first = transition(transition(State(), "stage", bundle=DIGEST_A),
                           "activate", approved=True)
        self.assertEqual(transition(first, "stage", bundle=DIGEST_A), first)
        upgrade = transition(transition(first, "stage", bundle=DIGEST_B),
                             "activate", approved=True)
        self.assertEqual(upgrade.previous, DIGEST_A)
        reverted = transition(transition(upgrade, "mark_failed"),
                              "rollback", approved=True)
        self.assertEqual(reverted.active, DIGEST_A)

    def test_uninstall_never_deletes_external_data(self):
        s = transition(transition(State(external_fingerprint="steam-and-vm"),
                                  "stage", bundle=DIGEST_A), "activate", approved=True)
        self.assertEqual(transition(s, "uninstall", approved=True),
                         State(external_fingerprint="steam-and-vm"))

    def test_corrupt_unknown_and_unapproved_transitions_block(self):
        for state in (State(phase="UNKNOWN"), State(phase="STAGED"),
                      State(phase="ACTIVE", active="not-a-digest"),
                      State(phase="ABSENT", active=DIGEST_A)):
            with self.subTest(state=state), self.assertRaises(TransitionDenied):
                transition(state, "stage", bundle=DIGEST_B)
        with self.assertRaises(TransitionDenied):
            transition(State(), "uninstall")
        with self.assertRaises(TransitionDenied):
            transition(State(), "rollback", approved=True)


def sample_archive(name="payload/bin/incus", mode=0o755, kind=tarfile.REGTYPE,
                   content=b"incus-offline-proof") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.mode = mode
        info.uid = 0
        info.gid = 0
        info.type = kind
        info.size = len(content) if kind == tarfile.REGTYPE else 0
        info.linkname = "payload/bin/other" if kind == tarfile.SYMTYPE else ""
        tar.addfile(info, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    return stream.getvalue()


def manifest(name="payload/bin/incus", content=b"incus-offline-proof") -> dict:
    return {"files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(),
                       "size": len(content), "mode": 0o755}]}


class ArchiveTests(unittest.TestCase):
    def check(self, blob, entries):
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            verify(tar, entries)

    def test_safe_declared_file(self):
        self.check(sample_archive(), manifest())

    def test_traversal_absolute_and_special_entries_rejected(self):
        for name in ("payload/../../etc/passwd", "/etc/passwd", "payload/bin/../incus",
                     "payload//incus", "payload/.hidden", "outside/incus"):
            with self.subTest(name=name), self.assertRaises(RejectedArchive):
                self.check(sample_archive(name=name), manifest(name=name))
        with self.assertRaises(RejectedArchive):
            self.check(sample_archive(kind=tarfile.SYMTYPE), manifest())
        with self.assertRaises(RejectedArchive):
            self.check(sample_archive(kind=tarfile.LNKTYPE), manifest())

    def test_unexpected_mode_and_sha_block(self):
        with self.assertRaises(RejectedArchive):
            self.check(sample_archive(mode=0o4755), manifest())
        with self.assertRaises(RejectedArchive):
            self.check(sample_archive(), manifest(content=b"tampered"))

    def test_unknown_file_and_duplicate_manifest_block(self):
        with self.assertRaises(RejectedArchive):
            self.check(sample_archive(name="payload/bin/unknown"), manifest())
        duplicate = manifest()
        duplicate["files"].append(deepcopy(duplicate["files"][0]))
        with self.assertRaises(RejectedArchive):
            self.check(sample_archive(), duplicate)


class SourceInventoryTests(unittest.TestCase):
    def test_build_source_inventory_is_explicitly_not_runtime_certification(self):
        s = make_source_inventory(
            "github.com/lxc/incus/v7\nexample.org/one v1.2.3\n"
            "example.org/two v0.0.1 => example.org/replaced v2.0.0\n",
            "v7.0.2", {"incus": DIGEST_A[:40], "raft": DIGEST_B[:40],
                         "cowsql": "c" * 40})
        self.assertEqual(s["bomFormat"], "CycloneDX")
        self.assertIn("source-inventory-only-not-runtime",
                      [p["value"] for p in s["metadata"]["properties"]])
        names = {component["name"] for component in s["components"]}
        self.assertIn("example.org/replaced", names)
        self.assertNotIn("example.org/two", names)

    def test_unpinned_go_replacement_rejected(self):
        with self.assertRaises(ValueError):
            make_source_inventory("example.org/one v1.0.0 => ../local\n",
                                  "v7.0.2", {"incus": "a", "raft": "b", "cowsql": "c"})


if __name__ == "__main__":
    unittest.main()
