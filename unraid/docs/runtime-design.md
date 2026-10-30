# 🚧 WIP — DO NOT USE — Runtime design

## Packaging boundary

Do **not** use upstream `polyseat-prepare` on Unraid. It expects distribution
package managers and systemd; Unraid has a different boot and plugin lifecycle.

The proposed runtime would use a pinned, verified Incus userspace bundle,
plugin-owned supervision and a user-selected persistent pool. A compiled Incus
binary is insufficient: library ABI, idmaps, LSM support, namespace features,
kernel modules, cgroups, storage and device permissions still need testing.

## Non-negotiable isolation

- No automatic managed Incus bridge, `br0` or host firewall edits.
- No Docker socket, container recreation, libvirt changes or GOW migration.
- No GPU reassignment. A GPU bound to `vfio-pci` for an exclusive Windows VM
  is not silently taken over by NVIDIA or used by Incus.
- A local-only Incus management socket with strict filesystem permissions;
  no LAN-facing admin API.
- No assumption that ZFS, Btrfs and XFS have interchangeable clone semantics.
- Persistent data in a user-selected pool; plugin config/bootstrap state
  clearly separated from seats and game libraries.

## Lifecycle and rollback

See [security-rollback-contract.md](security-rollback-contract.md) for a
proposed fail-closed state machine, scoped ownership, approvals, backup and
restore checks. `unraid/lab/lifecycle.py` now tests **pure state transitions**
without touching any files or services; real filesystem, process or host
transitions remain unimplemented.
The current disabled `.plg` is not an installer.

## Unverified

Host version/kernel compatibility, NVIDIA GPU availability and NVENC sessions,
Unraid boot persistence, isolated network, Moonlight input, Steam/Proton,
anti-cheat, memory/GPU load and safe two-seat teardown.

See [gaming-VM protection](gaming-vm-protection.md) for the hard rule that
exclusive passthrough GPUs stay owned by the VM while it is needed.
