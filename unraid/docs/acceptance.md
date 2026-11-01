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
| E | One seat with verified host GPU ownership, NVIDIA NVENC and input | Read-only `gpu-preflight` (sysfs, `/proc/driver/nvidia`, `/proc/locks`, libvirt files) run on Tower 2026-10-31: **allowed** (dGPU on `nvidia`, both owning VMs off, no holder, no hand-over lock); LXC `nvidia` hook bundled; physical GPU + NVENC in a container **PASS on Tower** (J3, run 8); input and streaming to a client: M3 |
| F | Two seats and Moonlight soak test | TODO |
| G | Shared library and per-seat storage on selected filesystem | TODO |
| H | Backup, stage, upgrade, rollback and uninstall without data loss | Verified staging, journal, atomic switch, health-check rollback, recovery, manual rollback and exact-path uninstall pass in 170+ sandbox tests **and** the real-incusd end-to-end on Ubuntu (state and user-data comparison after uninstall). **Not run on Unraid**; power loss and vfat/ZFS flash behaviour **TODO** |
| I | Steam/Proton/Once Human and anti-cheat | Unverified; no compatibility guarantee |
| J2 | Production desktop canary (M1) on Tower | **PASS 2026-10-31 (run 5)**: real Polyseat desktop (systemd, headless sway, screenshot, Sunshine wlr + libx264) in one unprivileged, NIC-less container on the Unraid kernel; start/restart/delete; host state restored ([tower-run5-desktop-gpu.md](tower-run5-desktop-gpu.md)) |
| J3 | Production GPU canary (M2) on Tower | **PASS** (run 8, 2026-10-31, commit `4a9a43e`, [tower-run8-gpu.md](tower-run8-gpu.md)): RTX 5070 Ti by device access in an unprivileged container, `nvidia-smi`, sway on GLES2/GBM, Sunshine `h264_nvenc`, twice, GPU released after each stop. Earlier runs failed closed: run 5 hook unreachable, run 6 hook exit 1 without detail, run 7 ldconfig `pivot_root` on Unraid's initramfs root |
| J4 | Steam on the GPU seat on Tower | **PASS** (run 12, [tower-run12-steam.md](tower-run12-steam.md)); with **persistent storage** on `cache/polyseat` runs 13/14 PASS: the second start reused the stored client (no update download, 16 s instead of 98 s), RAM peak 3.1–3.5 GB ([tower-run13-14-steam-storage.md](tower-run13-14-steam-storage.md)). Not covered: sign-in, games, Moonlight (M3) |
| J5 | First Moonlight stream from the GPU seat (M3) on Tower | **PASS** (run 19, 2026-11-01, commit `2357728`, [tower-run15-19-session.md](tower-run15-19-session.md)): the owner paired the Tab by PIN, Desktop streamed with `h264_nvenc` at 59 fps over the LAN only, and the owner signed in to Steam by QR code. The login persists on `cache/polyseat`. Not covered: input (disabled by design), games, two seats, the upstream daemon |
| J6 | Persistent Incus state on Tower (step 2 of the upstream plan) | **PASS** (runs 20/21, 2026-11-01, commit `1f0bcfe`, [tower-run20-21-persist.md](tower-run20-21-persist.md)): workspace on `cache/polyseat`, created once, then reopened only after a recorded clean stop; the persisted seat was found stopped (nothing autostarted), ran and stopped again. CI also proves the refusal without a clean stop. Not covered: polyseatd, GPU/Steam/NIC on the persisted seat, an Incus upgrade |
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
