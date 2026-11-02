# WIP — GPU path for seats on Tower (device access, no rebinding)

Status: **design + read-only guard implemented; no GPU has been given to any container yet.**
Measured strictly read-only on Tower on 2026-10-31 11:29–11:45 UTC
(evidence: `polyseat-canary-artifacts/gpu-inventory-20261031/`, kept off-repo).

## Measured facts

| Item | Value |
|---|---|
| dGPU | RTX 5070 Ti `0000:01:00.0` (`10de:2c05`), driver **`nvidia` 595.104.02 (open kernel module)**, persistence mode on, P8, 2 MiB of 16303 MiB used, 0 encoder sessions, no process holding `/dev/nvidia*` or its DRM nodes |
| Same IOMMU group 14 | `0000:01:00.1` HDMI audio, no driver |
| Nodes | `/dev/nvidia0`, `nvidiactl`, `nvidia-uvm`, `nvidia-modeset` (0666), `/dev/dri/card0` + `renderD128` (by-path 01:00.0); `nvidia_drm.modeset=Y` |
| iGPU | UHD 770 `0000:00:02.0`, `i915` (`card1`/`renderD129`); passed through by the (shut off) CachyOS VM definition |
| VMs passing the dGPU through | **GamingVM** (01:00.0 + 01:00.1 + NVMe + USB controller, `managed='yes'`, 16 GiB) and **Windows 11** (32 GiB); both shut off, neither autostarts. Only UbuntuServer (36 GiB, no hostdev) runs |
| Hand-over | Owner script `gamingvm-manager.sh start`: stops Docker GPU containers, `nvidia-smi -pm 0`, then **aborts the VM start if any process holds `/dev/nvidia0|ctl|uvm|modeset`** (`fuser`), else `virsh start` lets libvirt detach the GPU to `vfio-pci`. `stop` re-attaches to `nvidia`. Holds `flock -n /var/run/gamingvm-transition.lock`; leaves `/var/run/gamingvm-transition.unhealthy` after a failed hand-over |
| Host toolkit | `nvidia-container-cli` 1.20.1 + `libnvidia-container` (from Unraid's NVIDIA plugin), driver userspace incl. `libnvidia-encode`, `libnvcuvid`, `libEGL_nvidia`, `libnvidia-egl-gbm`, `gbm/nvidia-drm_gbm.so` |
| Docker GPU users | `tdarr` running with all of `/dev/dri`; `jellyfin-pgsql` NVIDIA env (runtime runc); `immich` created with a GPU request; Wolf-UI (GOW) exited; GOW compose autostarts from `/boot/config/go` once NVIDIA nodes exist |

## Decision

1. **No VFIO, no rebind, no module or driver change.** While the dGPU is on `nvidia`, a seat gets it by
   **device access**: Incus `gpu` device `pci=0000:01:00.0` plus `nvidia.runtime=true`, which makes the bundled LXC
   `nvidia` hook run the host's `nvidia-container-cli --user ... configure --no-cgroups` to inject exactly the host
   driver's userspace (version-matched by construction; the hook loads no modules in unprivileged mode).
2. **The VMs keep priority.** A seat never gets the GPU while GamingVM/Windows 11 runs or is being started, while
   the owner's hand-over lock is held, or after a failed hand-over (marker). `polyseatctl gpu-preflight` decides
   this from files only (sysfs, `/proc/driver/nvidia`, `/proc/locks`, libvirt's `/etc/libvirt/qemu/*.xml` and
   `/var/run/libvirt/qemu/<vm>.{xml,pid}`); it never calls libvirt, Docker or `nvidia-smi`.
3. **Simultaneous use by the Windows VM and a seat is impossible and never claimed.** Passthrough is exclusive.
4. Sharing between host consumers (a seat, Jellyfin/Tdarr, GOW/Wolf, local AI) is technically possible because they
   all just open device nodes; VRAM and NVENC sessions are then shared and must be measured, not assumed.

## Hazards found and how the plugin handles them

| # | Hazard | Handling |
|---|---|---|
| H1 | VM started **from the Unraid VM page** (not the manager) while a seat holds the GPU: libvirt's managed detach writes `unbind`, and the NVIDIA driver waits until every client closes (Incus' own source: "avoids a blocking call ... when unbinding") → libvirtd blocks | GPU-seat watchdog (M2): 1 s poll of owner-VM status files, driver binding, hand-over lock and marker; on any change the seat is force-stopped so the unbind can finish. Never tested against a real VM start without a separate GO |
| H2 | Manager start while a seat holds the GPU | Manager aborts the VM start (fail-closed, owner sees an error). Optional later integration (owner GO, flash write in the owner's hook dir): a `qemu.d` hook on `prepare begin` calling a synchronous `polyseatctl gpu-release` |
| H3 | User script **"NVIDIA power saver ArrayStart"** (runs at array start) executes `fuser -kv /dev/nvidia*` when no *Docker* process uses the GPU → would SIGKILL seat processes | No GPU seat is started automatically at array start until the owner decides (exclude Polyseat's cgroup in that script, or disable it) |
| H4 | `gamingvm-manager.sh` / "CPU Governor GamingVM" toggle persistence mode and CPU governor | Seats never change either; measurements record them |
| H5 | GOW/Wolf may start on the same GPU and bind Moonlight ports on the host IP | Seats use their own LAN IP (see [streaming-network.md](streaming-network.md)); Wolf is recorded as a GPU holder, not stopped |
| H6 | Memory: GamingVM needs 16 GiB, Windows 11 32 GiB, MemAvailable ≈ 19 GiB | `gpu-preflight` fails below 4 GiB; a RAM-rooted test seat must stay small; persistent seats move to a pool (M4) |
| H7 | `/boot/config/go` runs `chmod -R 777 /dev/dri`; NVIDIA nodes are 0666 | Host hygiene, not changed ([tower-host-hygiene.md](tower-host-hygiene.md)); inside the seat Incus creates its own nodes |

## M2 — first GPU test (needs its own owner GO)

Preconditions (all checked by `gpu-preflight`, re-run immediately before): driver `nvidia`, no IOMMU-group member on
`vfio-pci`, GamingVM and Windows 11 shut off and not autostart, hand-over lock not held, no marker, nodes present,
`nvidia-container-cli` present, bundle ships the hook, MemAvailable ≥ 4 GiB. Owner promise: no VM start during the
≈10-minute window.

Sequence (fixed, allow-listed like the CPU canary): M1 desktop container + `gpu` device `pci=0000:01:00.0` +
`nvidia.runtime=true` + `nvidia.driver.capabilities=compute,utility,video,graphics`; in-image self-test `nvidia`
profile: `nvidia-smi -L`, sway with `WLR_RENDERER=gles2`/`GBM_BACKEND=nvidia-drm`, grim screenshot, Sunshine
`encoder = nvenc` probe line; host samples VRAM, encoder sessions, power, temperature; stop → `gpu-preflight` again
must show **no holder** → delete. Abort on any watchdog trigger. Expected host diff: none outside plugin-owned RAM
paths; GPU back to idle P8 with 0 processes.

Implemented as `polyseatctl gpu-smoke --approve` (canary gate only, owner token action `gpu-smoke` bound to the
runtime bundle **and** the seat image): full `gpu-preflight` immediately before anything starts; fixed allow-list
(`gpu` device exactly `pci=0000:01:00.0 mode=0666`, `nvidia.runtime=true`, capabilities exactly
`compute,utility,video,graphics`); the independent canary supervisor re-runs a file-only ownership check every second
and **hard-stops the whole workload** (closing every NVIDIA descriptor) on a VM status file, a running owning VM, an
IOMMU-group member on `vfio-pci`, an unbound/rebound GPU, the owner's hand-over lock or failure marker; after every
stop it proves that no process of the test tree still holds an NVIDIA node; after teardown `gpu-preflight` must
report no holder. Tests: fixture-based (no GPU in CI); the first real run is the owner-approved M2 on Tower.

Not part of M2: network, Moonlight, Steam, a VM start, persistence mode changes, Docker containers.

## If exclusive switching were ever needed

It is not: the owner's manager plus libvirt `managed='yes'` already performs nvidia ↔ vfio-pci. Polyseat's only job
in a switch is to **release first and prove release** (seat stopped, no holder in Polyseat's cgroups), then let the
owner's tooling switch. Polyseat never writes `unbind`, `driver_override` or `new_id`, never reloads modules and never
starts or stops a VM. A failed hand-over leaves the marker; Polyseat then refuses GPU seats until the owner recovers.
