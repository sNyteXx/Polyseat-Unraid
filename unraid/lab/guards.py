"""Pure, fail-closed GPU/network planning for disposable test fixtures.

No host detection, subprocesses, network access or side effects. A successful
plan is NOT permission to install, start Incus or touch a gaming VM.
"""

from __future__ import annotations


class Blocked(ValueError):
    """A required safety fact is missing or a proposed seat is unsafe."""


def _required_bool(record: dict, field: str) -> bool:
    value = record.get(field)
    if type(value) is not bool:
        raise Blocked(f"missing/unknown boolean fact: {field}")
    return value


def propose(facts: dict, request: dict) -> dict:
    """Return a non-executable proposal, never an executable host plan.

    Caller-supplied observations are untrusted. Missing facts are blockers;
    an active VM is not itself a blocker if a DIFFERENT, verified GPU exists.
    """
    if not isinstance(facts, dict) or not isinstance(request, dict):
        raise Blocked("facts and request must be objects")
    if facts.get("source") != "offline-fixture":
        raise Blocked("only explicitly labelled offline fixtures are accepted")
    if not isinstance(facts.get("kernel"), str) or not facts["kernel"]:
        raise Blocked("kernel observation is required")
    if not isinstance(facts.get("unraid_version"), str) or not facts["unraid_version"]:
        raise Blocked("Unraid release observation is required")
    if not _required_bool(facts, "cgroup_v2"):
        raise Blocked("cgroup v2 not verified")
    _required_bool(facts, "docker_running")
    _required_bool(facts, "libvirt_running")

    seats = request.get("seats")
    if type(seats) is not int or seats not in (1, 2):
        raise Blocked("only one or two simulated seats are supported")
    if request.get("network_mode") != "isolated-no-uplink":
        raise Blocked("any bridged, LAN, NAT or host network requires a separate review")
    target = request.get("gpu_pci")
    if not isinstance(target, str) or not target:
        raise Blocked("a specific GPU PCI address is required")

    gpus = facts.get("gpus")
    vms = facts.get("vms")
    if not isinstance(gpus, list) or not gpus:
        raise Blocked("GPU inventory is missing")
    if not isinstance(vms, list):
        raise Blocked("VM inventory is missing")
    for vm in vms:
        if not isinstance(vm, dict) or not isinstance(vm.get("name"), str):
            raise Blocked("incomplete VM entry")
        _required_bool(vm, "running")
        if not isinstance(vm.get("assigned_gpus"), list):
            raise Blocked("VM GPU assignments are unknown")
        if any(not isinstance(v, str) for v in vm["assigned_gpus"]):
            raise Blocked("invalid VM GPU assignment")

    matches = [gpu for gpu in gpus if isinstance(gpu, dict) and gpu.get("pci") == target]
    if len(matches) != 1:
        raise Blocked("target GPU missing or duplicated")
    gpu = matches[0]
    if gpu.get("driver") != "nvidia":
        raise Blocked("host NVIDIA driver ownership not verified (vfio-pci is forbidden)")
    if not _required_bool(gpu, "iommu_group_exclusive"):
        raise Blocked("GPU IOMMU isolation not verified")
    if not _required_bool(gpu, "nvenc_capable"):
        raise Blocked("NVENC capability not verified")
    if any(target in vm["assigned_gpus"] for vm in vms):
        raise Blocked("GPU assigned to a VM; never detach or stop it automatically")

    return {
        "classification": "OFFLINE-PROPOSAL-ONLY",
        "installable": False,
        "host_commands": [],
        "seats": seats,
        "gpu_pci": target,
        "network_mode": "isolated-no-uplink",
        "forbidden_mutations": ["br0", "Docker", "libvirt", "VM", "vfio-pci", "GOW"],
        "remaining_gates": [
            "Unraid kernel/userspace/Incus runtime compatibility",
            "independently tested client access without host bridge mutations",
            "real NVENC, Sunshine/Moonlight and input isolation",
            "disposable-host lifecycle and rollback acceptance",
        ],
    }
