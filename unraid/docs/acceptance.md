# 🚧 WIP — DO NOT USE — Acceptance matrix

Green source CI is **not** Unraid certification. A gate can be marked complete
only with recorded evidence from the relevant disposable test environment.

| Gate | Requirement | Status |
|---|---|---|
| A | Read-only host diagnostics on actual Unraid release | **Done once, read-only, on one 7.3.3 host** ([inventory](host-inventory-unraid-7.3.3.md)); a disposable test system is still needed for any mutating evidence |
| B1 | Immutable Incus/raft/cowsql source pins, Ubuntu build proof | Incus **7.5.1** compiled and passed the no-GPU CLI smoke ([proof](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41091221184)); **not** Unraid runtime evidence |
| B2 | Complete runtime bundle, SBOM, signature/provenance and ABI test | Offline ABI closure check against real Unraid 7.3.3 sonames/symbol versions, reproducible bundle builder and signature verification are implemented and tested; liblxc/cowsql runtime closure, runtime SBOM, release signing key and vulnerability audit **TODO** |
| C | Non-systemd service lifecycle, boot persistence, UID mapping | `polyseatctl` start/stop/restart/boot/health with a **real incusd 7.5.1 + LXC 6.0.6, unprivileged CPU-only container, no systemd** passes 13 end-to-end steps on a disposable Ubuntu 24.04 runner ([run](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41124288366)); `cgroup2-unraid` interplay, idmapped mounts on ZFS/XFS and real Unraid boot **TODO** |
| D | Network isolation without Docker, libvirt or bridge changes | Offline plan rejects `br0`, NAT, host and bridged modes; real reachable isolation **TODO** |
| E | One seat with verified host GPU ownership, NVIDIA NVENC and input | Read-only `gpu-preflight` (sysfs, `/proc/driver/nvidia`, `/proc/locks`, libvirt files) run on Tower 2026-10-31: **allowed** (dGPU on `nvidia`, both owning VMs off, no holder, no hand-over lock); LXC `nvidia` hook bundled; physical GPU/NVENC in a container **TODO (M2, owner GO)** |
| F | Two seats and Moonlight soak test | TODO |
| G | Shared library and per-seat storage on selected filesystem | TODO |
| H | Backup, stage, upgrade, rollback and uninstall without data loss | Verified staging, journal, atomic switch, health-check rollback, recovery, manual rollback and exact-path uninstall pass in 170+ sandbox tests **and** the real-incusd end-to-end on Ubuntu (state and user-data comparison after uninstall). **Not run on Unraid**; power loss and vfat/ZFS flash behaviour **TODO** |
| I | Steam/Proton/Once Human and anti-cheat | Unverified; no compatibility guarantee |
| J | Production CPU-only canary on the owner's Tower | **No-Go**: preconditions P1–P9 in [production-canary-plan.md](production-canary-plan.md) open (signing, confinement, canary gate, profile policy, triage); no host change made |

Order of the next live steps: [mvp-roadmap.md](mvp-roadmap.md).

There is no installable `.plg` (only a non-installable template) until B2–H have passed. Production deployment
or GOW migration requires a separate, explicit operator decision.

**All status items that mention offline tests are simulator evidence, not
completion of an acceptance gate.** No test has touched the running gaming VM.

The [7.5.1 proof](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41097086718)
**passed a real no-GPU container lifecycle** on disposable Ubuntu 24.04:

- Launched a dedicated Incus daemon without systemd and with a private socket.
- Imported a local BusyBox image, started, executed, stopped, restarted and
  deleted the isolated container.
- Created no managed Incus bridge and touched no Unraid/GPU/VM hardware.

This demonstrates Ubuntu runner behavior, **not** Unraid compatibility.
