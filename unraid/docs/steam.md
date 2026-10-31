# Steam in the Unraid seat (first step, 2026-10-31)

Upstream Polyseat installs Steam in every seat (`stepSteam` in `internal/seat/provision.go`) and runs it with
gamescope, Proton, a shared library and Big Picture over Moonlight. The upstream daemon does not run on Unraid yet
(roadmap: needs a managed bridge, systemd-run, a udev rule). This step brings Steam into the Unraid seat the same
way and proves that it runs on Tower's GPU, up to its sign-in window. Nobody signs in.

## What changed

- **Seat image** (`unraid/seat-image`):
  - `multilib` from the same pinned Arch archive date; upstream's `stepSteam` packages (`steam lib32-libglvnd lib32-vulkan-icd-loader ttf-liberation zenity`, checked by a test against `provision.go`);
  - upstream's NVIDIA driver assumptions (`--assume-installed vulkan-driver lib32-vulkan-driver`);
  - `lib32-mesa` as the software path for the rehearsals; `mesa-utils`, `lib32-mesa-utils` and `vulkan-tools` for the checks;
  - Xwayland (lazy) and an `en_US.UTF-8` locale;
  - a DHCPv4-only `eth0` network file. networkd and resolved stay masked; only the LAN self-test unmasks them in its own running copy.
  - The image is 424 MB compressed and 1.8 GB unpacked. Packing now streams `docker export` straight into xz, so the rootfs never lands on disk; the output is byte-identical to before.
- **Self-test** `polyseat-selftest steam` (GPU) and `steam-cpu` (rehearsal) add:
  - the injected 32 bit NVIDIA libraries (GPU mode);
  - LAN by DHCP with a default route and DNS;
  - 64 and 32 bit GL by NVIDIA (or Mesa in the rehearsal);
  - Vulkan on the NVIDIA device (GPU mode);
  - Steam's bootstrap and self-update, a stable `steamwebhelper` with a Steam window, and a screenshot;
  - Steam fully stopped afterwards.
- **Controller** `steam-smoke` (operation 4, its own approval action, bound to the seat image):
  - the M2 GPU path plus `compat32` and `display`. In libnvidia-container 1.20.1, `display` only adds
    `/dev/nvidia-modeset`, which NVIDIA's Vulkan presentation needs (Tower run 11). Upstream seats get `all`;
  - exactly one NIC: `nictype=bridged parent=br0 hwaddr=00:16:3e:50:53:01`, a veth in the host's existing br0 for the test only (owner decision 2026-10-31). No Incus-managed network, no firewall or routing change, no port forward; Sunshine's web UI is localhost-only and UPnP off;
  - the host's Vulkan manifest, validated strictly, pushed into the seat as upstream does;
  - budget 4 CPUs on cores 24–27, **8 GiB**, 4096 pids;
  - before start: GPU preflight, br0 is a bridge, the MAC is free, the token has enough time left;
  - after stop: GPU released and the veth gone.
- **Host-veth MAC** (after Tower run 9, owner decision): a pinned Incus patch gives the host end of the veth an `fe:` MAC, as libvirt's VM taps have, so br0 never adopts it. See [tower-run9-steam.md](tower-run9-steam.md).
- **nft stub**: Incus 7.5.1 only knows nftables. Without `nft` (Tower) a bridged NIC's stop fails before it removes its veth. The rehearsal showed the veth left in the bridge.
  - The canary daemon's private `deny-tools` (on the exec-capable `/var/lib/polyseat-lxc`, self-tested; `/run` is noexec) now hold an `nft` that answers exactly `--terse --json -nn list ruleset` with an empty ruleset and refuses everything else.
  - A real nft would load `nf_tables` and create a test table at daemon start.

## Budget, measured

The local rehearsal measured the non-reclaimable (anon + tmpfs) peak at 7.0 GB:
- the RAM rootfs, 1.8 GB;
- Steam's own update, 2.6 GB on disk;
- Steam, sway, Xwayland and Sunshine with software rendering.

The originally proposed 6 GiB would OOM. 8 GiB leaves Tower about 10 GiB, and the watchdog still stops the test below
4 GiB MemAvailable.

## Where each part is tested

| Part | Where |
|---|---|
| Allow-list, policy guard (NIC, GPU, limits, volatile keys), token, LAN precheck, Vulkan manifest validation, nft stub | unit tests (`test_desktop_canary.py`) |
| Real incusd/LXC with the controller's NIC on a fixture br0 (dnsmasq + NAT), deny-tools, `file push`, Steam from Valve to its sign-in window, veth removed | `unraid/scripts/local-steam-rehearsal.sh` (~2 min) and CI job `steam-rehearsal` |
| NVIDIA: compat32 injection, 32 bit GL on the RTX, Vulkan, NVENC, the real br0 and LAN router | Tower only (`steam-smoke`) |

## Not yet

- gamescope, Proton and Big Picture;
- a persistent Steam library (RAM only by owner decision for now);
- Moonlight pairing and input (M3);
- the upstream daemon itself.
