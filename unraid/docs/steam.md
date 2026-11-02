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

## Persistent storage (owner, 2026-10-31: "on the cache")

| | |
|---|---|
| Dataset | `cache/polyseat` on the ZFS cache pool, `quota=150G`, `recordsize=1M`, compression and atime inherited, mounted at `/mnt/cache/polyseat` |
| Seat directory | `/mnt/cache/polyseat/steam-seat`, owner `1001000:1001000` (the seat's player, uid 1000 in the container), mode 0700 |
| In the seat | that directory only, as `~/.local/share/Steam`: login, client and library survive a restart |
| Unraid | outside `appdata`, so not in the owner's backup set (`backupssd/backup_unraid/cache/{appdata,cloud}`). Without a share config Unraid treats it as cache-only and does not export it, like `kernel-build` |
| Controller | before start: the directory is exactly this owner and mode, on exactly `cache/polyseat` (`findmnt`), the dataset has a quota, at least 8 GiB free. Exactly one `disk` device. Uninstall never touches it |
| Setup (one-time, 2026-10-31) | `zfs create -o quota=150G -o recordsize=1M cache/polyseat`; `install -d -o 1001000 -g 1001000 -m 0700 /mnt/cache/polyseat/steam-seat` |
| Grow / remove | `zfs set quota=300G cache/polyseat` / `zfs destroy cache/polyseat` (removes everything, Steam login included) |
| New Permissions | Fix Common Problems' weekly "New Permissions" resets every top-level folder on every disk and pool to `nobody:users` 777 unless it is excluded. `polyseat` is excluded (owner, 2026-11-01). Without that, the controller refuses the storage (Tower run 17) |

## M3: one Moonlight session on the LAN (owner, 2026-10-31)

`polyseatctl steam-session --approve` (operation 5, its own token action `steam-session`, bound to the seat image)
starts the same seat as `steam-smoke`, with the same NIC, storage, budget and checks, and then a session instead of
the self-test.

| | |
|---|---|
| Length | until the operator's `steam-session-stop`, at the latest 240 s before the token expires (token: 30 min) |
| LAN | the seat's veth in br0 only while the session runs; DHCP with the MAC as client id, so the router keeps one address for it |
| Sunshine | stream ports 47984–48010 in the seat, LAN only: no port forward, UPnP off. Web UI localhost only, with random credentials for this session in a root-only file in the seat |
| Pairing | Moonlight (Shield, Tab) adds the seat by its IP and shows a PIN. The owner relays it; `steam-session-pin NNNN` hands it to Sunshine, but only if exactly one device is waiting |
| Input | none. Sunshine would create its virtual devices with uinput on the HOST, where a keyboard reaches Unraid's text console. Steam signs in by QR code with the Steam Mobile app |
| While running | every 2 s: VM priority on the GPU (`gpu_guard_quick`), cgroup budgets, a waiting PIN; every 30 s a status (`steam-session-status`: IP, encoder, paired clients, waiting pairings, Steam, signed-in accounts) |
| End | `polyseat-session stop` (Steam, Sunshine, sway, credentials), then the usual teardown: seat stop, GPU released, veth gone, delete, daemon stop |

First stream: Tower run 19 ([tower-run15-19-session.md](tower-run15-19-session.md)).

`steam-session-pin`, `-status` and `-stop` need no token and do not wait for the controller lock, which the running
session holds. They only read or write root-private files in the run directory; the session reads them, so they never
reach Incus or the host.

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
| Session: operation 5 allow-list, PIN/stop hand-over, token-free operator commands | unit tests (`SteamSessionTests`) |
| Session plumbing on the fixture bridge: start (LAN, Sunshine with session credentials, Steam), PIN path to Sunshine's API, stop | `local-steam-rehearsal.sh` and CI job `steam-rehearsal` |
| Moonlight pairing and streaming from a real client | Tower only (`steam-session`) |

## Not yet

- gamescope, Proton and Big Picture;
- input from the client (needs upstream's input broker and a host udev rule);
- the upstream daemon itself.
