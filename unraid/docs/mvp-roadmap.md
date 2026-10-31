# WIP — Gap matrix and MVP order after the second Tower canary (2026-10-31)

Baseline: commit `6352f46`, CI run 41194843793, Tower canary run 2 **PASS** (CPU-only BusyBox container on the
production Unraid 7.3.3 kernel, 42/42 host-state lines unchanged). That proves the native Incus 7.5.1/LXC 6.0.6
userspace and the controller's bounded start/exec/stop/teardown on Unraid — **nothing about a desktop, a GPU,
streaming, persistence or boot integration.**

Reality check that frames every row below:

- The Unraid kernel (6.18.54-Unraid) is never replaced or patched. Incus **containers share it**: everything a seat
  needs must already be in that kernel (verified so far: cgroup2 with all controllers, user namespaces, seccomp,
  `uinput`/`uhid` as loadable modules, nvidia/nvidia_drm/nvidia_uvm loaded by Unraid's NVIDIA plugin).
- Incus **VMs** would be a different isolation model (QEMU, VFIO), compete with libvirt for the same devices and are
  explicitly out of scope; seats are containers.
- A GPU is either host-owned (`nvidia`, shareable by device access) or VM-owned (`vfio-pci`, exclusive). Never both.

## Gap matrix

Legend: ✅ implemented and proven on Tower · 🟢 implemented, proven on disposable Linux/CI only · 🟡 scaffold/partial ·
🔴 missing · ⛔ deliberately excluded

| Area | State | Evidence / gap |
|---|---|---|
| Pinned Incus/LXC/raft/cowsql/unsquashfs build, ABI gate vs Unraid 7.3.3 | ✅ | run 2 on Tower |
| Signed bundle, manifest, hash/listing/ABI checks, atomic slot switch | ✅ (stage/activate path) | run 2; 340+ sandbox tests |
| Controller start/stop/health without systemd, private namespaces, cgroup limits, watchdog | ✅ (canary mode) | run 2 |
| update / rollback / recover / uninstall | 🟢 | sandbox + real incusd on Ubuntu CI; never on Unraid |
| Plugin-manager install (`.plg`), boot re-install, `disks_mounted`/`stopping_svcs` events | 🟡 | template + event scripts exist; `.plg` disabled; never installed |
| Persistent `DATA_DIR` on a pool (ZFS `cache`/`download`, btrfs `misc`) | 🔴 | canary uses RAM only; `/mnt/cache/appdata` is 0777 uid 99 → needs a root-owned dedicated directory/dataset |
| WebUI (status, settings, logs) | 🟡 | pages + PHP API + tests; settings form for GPU/network missing |
| LXC `nvidia` hook in the bundle (`nvidia.runtime`) | 🟢 *(this change)* | copied verbatim from the pinned LXC tag; controller sets `INCUS_LXC_HOOK` |
| Read-only GPU ownership guard (`gpu-preflight`) | ✅ read-only on Tower *(this change)* | 18 fixture tests; live read-only run 2026-10-31: allowed |
| Seat image (Arch desktop, offline, pinned, signed) | 🟡 in progress | branch `wip/seat-image-desktop`; upstream builds seats online only |
| CPU-only desktop (headless sway + Sunshine software encode) in a container | ✅ Tower run 5 (M1 PASS) | `seat-image-stage` + `desktop-smoke` (M1) implemented and unit-tested; CI rehearsal script ready; image build in progress; never on Tower |
| GPU seat (NVIDIA device access + NVENC) | 🟡 | `gpu-smoke` (M2) implemented with VM-priority supervisor + release proof; fixture-tested only; needs desktop image + Tower GO |
| LAN streaming (Moonlight ↔ Sunshine) | 🔴 | network plan: [streaming-network.md](streaming-network.md); needs owner IP decision + GO |
| Input (uinput/uhid broker) | 🔴 | upstream broker needs python3 + bpftrace on the host; **both absent on Tower** |
| Two seats, shared library | 🔴 | upstream sharing needs reflink (btrfs/XFS); ZFS block cloning unverified |
| polyseatd (upstream daemon, web UI :47800) on Unraid | 🔴 | needs systemd-run, managed NAT bridge, udev rule, prepare.sh refuses Unraid |
| Signing-key custody/rotation for continuous operation | 🔴 | software keys without passphrase on UbuntuDev only ([signing-key-policy.md](signing-key-policy.md)) |
| Docker, libvirt, `br0`, host firewall, kernel, modules, VFIO changes | ⛔ | never done by the plugin |

## MVP order (each step is its own owner GO on Tower)

| Step | Proves | Where first | Tower write? |
|---|---|---|---|
| M0 | CPU-only BusyBox lifecycle | done (run 2) | done |
| **M1** (run 3 failed closed at staging, [tower-run3-desktop.md](tower-run3-desktop.md); run 4 desktop ran on Tower but failed an over-strict isolation check, [tower-run4-desktop.md](tower-run4-desktop.md)) | Real Polyseat desktop (systemd, headless sway, grim screenshot, Sunshine software-encoder init) in one unprivileged, NIC-less container on the Unraid kernel; start/stop/restart/delete; host diff | disposable CI runner, then Tower | yes: RAM-only, GO needed |
| **M2** | Same container with the RTX 5070 Ti by **device access** (`gpu pci=0000:01:00.0` + `nvidia.runtime`): `nvidia-smi` inside, sway on NVIDIA GBM, Sunshine NVENC init, VRAM/encoder/power measured; GamingVM/Windows 11 guard + watchdog | Tower only (no GPU in CI) | yes: GO needed |
| M3 | One LAN session to one Moonlight client (Shield): picture, audio, input, latency/encoder load | Tower | yes: network GO (static IP, ports) |
| M4 | Persistent seat on a dedicated pool directory; `.plg` install/boot/uninstall in a maintenance window | Tower | yes: flash + pool GO |
| M5 | Second seat (Galaxy Tab S9 Ultra), resource and NVENC-session limits, shared library semantics on the chosen pool | Tower | yes |
| M6 | Steam/Proton, savegames per profile, local-AI coexistence | Tower | yes |

Smallest meaningful desktop test = **M1**: it exercises the parts most likely to break on Unraid (systemd as
container init under `cgroup2-unraid`, idmapped unpacking of a full Arch rootfs, user session/linger, wlroots,
Sunshine's capture/encode probe) without touching any device, network or persistent storage.

## Deferred: upstream Polyseat 0.45/0.46 (owner instruction 2026-10-31)

Upstream `superuser404notfound/Polyseat` is at 0.46.0 (`ed4d27198881bec3d42f1f843098475a5f58454a`; tags v0.45.0
`03c93cfb2998`, v0.46.0 `d47a9e48bd9f`); this branch carries upstream up to 0.44.0.
0.45.0 adds desktop/emulator game shortcuts directly in Moonlight; 0.46.0 disables Steam's in-game screensaver so it
does not cover cutscenes. **Integrate only after M1 and M2 have passed on Tower AND the first Moonlight stream works
end-to-end**, as a separate, reviewable step (own WIP branch/commit), with conflict check, tests/CI and documented seat
re-provisioning. Until then: no upstream merge, no main merge, no release, no Tower write for it. Newer upstream changes
are assessed separately.

