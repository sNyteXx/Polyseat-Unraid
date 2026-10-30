# 🚧 WIP — DO NOT USE — EXPERIMENTAL UNRAID PORT

> **DANGER: NOT INSTALLABLE OR SUPPORTED.** Do not run Polyseat host preparation, Incus initialization, network bridging, or GPU passthrough changes on a production Unraid server. This branch is a feasibility scaffold only. It has not been validated on Unraid hardware. GOW must remain installed until an independently verified migration.

## Goal
Native Unraid plugin lifecycle for Polyseat without replacing Unraid's Docker, libvirt, network bridge, or NVIDIA host driver. This is **not** a production-ready implementation.

## Gates
1. Read-only preflight on Unraid 7.3.x: kernel features, cgroup v2, namespaces, NVIDIA, mount filesystems, existing bridge and occupied ports.
2. Build Incus userspace in isolated CI and verify host kernel requirements; do not assume systemd or apt/pacman/dnf.
3. Demonstrate isolated Incus startup and shutdown without touching br0, Docker or libvirt.
4. Validate NVIDIA device mapping and NVENC with one headless seat, no Steam.
5. Validate Sunshine/Moonlight, then a second seat, input separation, disk and memory isolation.
6. Test shared library filesystem semantics; ZFS is not an automatic substitute for Btrfs/XFS reflinks.
7. Explicit operator-approved migration and rollback, then optional GOW removal.

## Risks
- Upstream host installer assumes supported distributions and systemd; **never run it on Unraid**.
- Incus bridge and iptables/nftables rules may collide with Unraid networking.
- NVIDIA GPU must not be concurrently passed through to a VM.
- Steam/Proton/Once Human and anti-cheat remain unverified; no promises of compatibility.
- Host boot persistence, UID mappings, input devices and kernel modules need dedicated tests.

## Design constraints
Keep all plugin-owned files under an explicit namespace, store persistent state on a user-selected pool, avoid host package mutation, do not download or execute remote installers, and fail closed on unsupported features. Provide dry-run, backup, rollback and uninstall that never deletes seats or game data by default.
