# 🚧 WIP — DO NOT USE — Runtime design

## Packaging decision
Do **not** run upstream polyseat-prepare on Unraid. Upstream assumes systemd and distribution package managers. Unraid is Slackware-based and has a different boot lifecycle.

Proposed architecture: pinned, checksum-verified Incus userspace bundle; plugin-owned launcher and supervisor; persistent state on a dedicated pool; no global changes to Docker or libvirt. An Incus binary alone is not sufficient: test dependencies, idmaps, device permissions, network namespace support, and confinement.

## Runtime gates
- Verify kernel namespaces, cgroup v2, overlay/storage drivers, idmapped mounts and required modules.
- Check unprivileged userns policy and subuid/subgid before any changes.
- Never initialize Incus with a default managed bridge or modify br0 automatically.
- Use an isolated test network with explicitly reviewed routes and firewall rules.
- GPU device mapping must use host NVIDIA driver ABI and preserve exclusive VM passthrough rules.
- Seat storage must not assume ZFS has Btrfs/XFS reflink behavior.
- Prohibit automatic daemon start until full rollback test succeeds.

## Upgrade and rollback
Store a manifest with pinned versions, checksums, paths and config schema. Upgrade by staging and validating before swapping. Roll back binary and config together. Preserve seat data by default. Never uninstall or alter GOW without explicit migration approval.

## Known unverified
Incus on Unraid 7.3.3, NVIDIA 595.104.02 inside Incus, NVENC concurrent sessions, Moonlight input, Steam/Proton anti-cheat, 2-seat memory and GPU load.
