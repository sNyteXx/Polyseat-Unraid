# Upstream-first integration plan (after M3, 2026-11-01)

Owner guideline (2026-11-01): upstream [Polyseat](https://github.com/superuser404notfound/Polyseat) is the product
architecture: polyseatd, the seat lifecycle, Sunshine/Moonlight, Steam/Big Picture/gamescope, game shortcuts,
separate users and two seats. The Unraid controller (`polyseatctl`) stays only as the host adapter:
- GPU-ownership guards and safety gates;
- resource, storage and network integration;
- the plugin manager and WebUI;
- rollback.

This plan rests on a read-only gap analysis of upstream **v0.46.0** (`ed4d271`) and a read-only check of Tower. It
names no new Tower write; every Tower step needs its own gate.

## Proven on Tower

| | Evidence |
|---|---|
| Incus 7.5.1 runtime under our supervisor, unprivileged containers on the Unraid kernel | J1–J2, [tower-run5-desktop-gpu.md](tower-run5-desktop-gpu.md) |
| RTX 5070 Ti by device access while the passthrough VMs keep priority (guard and watchdog) | J3, [tower-run8-gpu.md](tower-run8-gpu.md) |
| Steam with compat32 and Vulkan on the GPU; LAN on br0 with the `fe:` host-veth patch; persistent storage on `cache/polyseat` | J4, [tower-run12-steam.md](tower-run12-steam.md), [tower-run13-14-steam-storage.md](tower-run13-14-steam-storage.md) |
| Moonlight pairing and an NVENC stream (59 fps) over the LAN only; Steam QR login persists | J5, [tower-run15-19-session.md](tower-run15-19-session.md) |
| Persistent Incus state on `cache/polyseat`, reopened only after a recorded clean stop, nothing autostarted (step 2) | J6, [tower-run20-21-persist.md](tower-run20-21-persist.md) |
| Upstream polyseatd on the LAN (`192.168.178.24:47800`), password before first start, host actions locked, no seats (step 3) | J7, [tower-run22-polyseatd.md](tower-run22-polyseatd.md) |

## Open

- input;
- games and Proton, Big Picture and gamescope;
- two seats;
- an installable `.plg`.

## Gap analysis: upstream components on Unraid

| Upstream component | On Unraid | Why / adapter |
|---|---|---|
| `polyseatd` core: `api`, `auth`, `web`, `sunshine`, `supervise`, `library`, `version` | **runs unchanged** | needs root and a persistent `state_dir`. The library's reflink pool needs FICLONE; Tower has ZFS block cloning active |
| `config` | **runs unchanged**, with Unraid values | `web_update`, `web_uninstall`, `web_lan_bridge` and `update_check` false; `uplink` br0; state and library on the cache pool |
| `seat` provisioning, lifecycle, apps, Steam/Big Picture, 0.45 game shortcuts, 0.46 screensaver | **runs unchanged** inside the seat | it is all Incus exec and file push. Adapters: the GPU admission (below), the Vulkan ICD path (Tower: `/etc/vulkan/icd.d`), the optional `libnvidia-ngx.so.1` (missing on Tower). Upstream pins the GPU by PCI address when there are two or more render nodes; Tower has two (iGPU + RTX) |
| `seat/network.go` `eth1` bridged on br0 | **runs unchanged** with our `fe:` host-veth patch | as proven in J4/J5 |
| `seat/network.go` `eth0` (managed bridge with NAT) | **adapter** | either a pre-created Incus bridge with NAT (dnsmasq is on Tower; nftables is not, so this needs checking) or the bridged-only path that `network.go` accepts |
| `incusx` | **adapter** | talks to the local Incus socket. Incus state must move from RAM to the cache pool (`INCUS_DIR`). Images come from images.linuxcontainers.org; alternatively it adopts existing containers |
| `hostcg` | **adapter** | Tower has no cgroup v1 mounts. Our incusd runs in its own namespaces, so this should only report |
| `prepare`, `update`, `uninstall`, `lanbridge`, `hostpkg`, `report` (partly), `seat/hostlibrary` | **do not fit** | they need a package manager, `systemd-run`, NetworkManager or a host Steam. Disable them by config; the plugin owns install, update and rollback |
| `host/install.sh`, `prepare.sh`, `distro.sh`, `update.sh`, `uninstall.sh`, units | **do not fit** | the `.plg` recreates only the needed pieces: helper files, the udev rule, the uhid module, subuid. Upstream no longer needs the broker and observer units, because polyseatd supervises both itself |
| Input helpers `broker.py`, `uhid_observer.py` (bpftrace), `device_owner.py` (from udev), `fakeudev.py` (in the seat) | **adapter, hard** | **Tower has no python3 and no bpftrace**, and our seat image has no python. Without the observer, gamepads cannot be told apart once two seats exist |
| `72-polyseat-hide.rules`, `polyseat-modules-load.conf` | **adapter** | the plugin installs the rule at boot and runs `udevadm control --reload` (eudev 251) and `modprobe uhid`. `uinput` is already loaded |
| `check-hardening.sh` | **adapter** | its sysrq and VT checks belong in our preflight |

## Input: why a picture is not yet a playable seat

How upstream does it:
- Each seat gets `/dev/uinput` and `/dev/uhid` at mode 0666. Sunshine creates its devices through them, and those devices live in the **host** kernel.
- A broker per seat finds them by the creator's cgroup, seals them on the host (root 0600, no ACL, udev re-announce) and attaches each to exactly one seat.
- A udev rule hides them from the host session.

Upstream's own hardening check calls one state an open exposure: "nobody is logged in graphically while seat devices exist". **That is Unraid's permanent state.** Tower measured read-only (2026-11-01):
- **Console:** the active VT is tty1, a text console (runlevel 3, no local GUI). A seat keyboard types into the Unraid login prompt.
- **SysRq:** `kernel.sysrq=1`, so every SysRq function is on. A client keyboard can reboot or crash the host, which means an unclean array stop.
- **Ctrl+Alt+Del:** `/etc/inittab` maps it to `/sbin/shutdown -t5 -r now`, so the host reboots.
- **Helpers:** python3 and bpftrace are missing.
- **VMs:** no VM uses evdev input.

Consequences:
- Gamepads come first, through uhid only, without `/dev/uinput`. Even then upstream's `pad-pointer` creates a small keyboard (Esc, Enter, Tab, arrows) via uinput, so gamepad-only input also needs the input policy below.
- Upstream has no switch to withhold `/dev/uinput` or to set Sunshine's `keyboard`/`mouse`/`controller`. That is a small upstream patch: an input policy of off, gamepads or all.
- Keyboard and mouse come only after a console guard: SysRq restricted, Ctrl+Alt+Del not rebooting while seats have input, and the active VT not receiving keys (`K_OFF` while seat input exists). Each of these is a host change and needs its own owner GO.

## Order

| # | Step | Gate | Owner decision needed |
|---|---|---|---|
| 1 | Merge upstream **v0.46.0** on its own branch, one reviewable merge, no `unraid/` changes. 0.44→0.46 touches only seat-internal Go, docs and packaging | local tests, CI | review |
| 2 | Persistent Incus state on `cache/polyseat` (`INCUS_DIR`, `dir` pool); the default profile carries the budgets as `limits.*` | local rehearsal, CI, Tower canary | location and quota |
| 3 | polyseatd built in CI and run under our supervisor, **without seats**: Unraid config, hostcg report-only | CI rehearsal, Tower canary (daemon up, UI on localhost/LAN, no seat) | UI exposure (LAN only) |
| 4 | GPU admission at **every** container start, so polyseatd's autostart cannot bypass the VM-priority guard (an LXC pre-start hook as host root). Our watchdog stops seats through polyseatd first, because upstream needs the broker stopped before the container | local L2 hook rehearsal, Tower canary with a VM-priority drill | none beyond the standing GPU rules |
| 5 | **One seat provisioned by polyseatd**, input off by the new policy. Pairing and apps through polyseatd. Retire our duplicates: `polyseat-session`, the seat-side units and the Sunshine config template | Tower session like M3 | provisioning source: upstream's network provisioning on Tower or our signed image adopted |
| 6 | Host pieces for input: udev rule, uhid, helpers. The **python3/bpftrace gap** is solved by bundling them into the signed runtime or by porting the helpers upstream | local and CI L2 with a fake Sunshine device | bundle or port |
| 7 | **Gamepads** (uhid only, no uinput) | Tower session: the Tab's controller drives Big Picture | GO for the udev rule and uhid at boot |
| 8 | **One playable session**: a game through Big Picture or gamescope with a gamepad | Tower session | none |
| 9 | Keyboard and mouse behind the console guard (SysRq, Ctrl+Alt+Del, VT) | Tower drill: keys never reach tty1 | GO for each host change |
| 10 | **Second seat**: the controller's single-instance constants (MAC, storage dir, CPU set) become per seat; reflink library shared; two brokers; observer required | Tower, both seats at once | GPU sharing between two seats and the VMs |
| 11 | **Plugin/WebUI**: the `.plg` installs the binary, helpers, rule and config. The Unraid page shows gates and guards and links to polyseatd's UI | CI package tests, Tower install/uninstall with rollback | `.plg` activation |

## Step 2 design: persistent Incus state (owner, 2026-11-01: `cache/polyseat/incus`, the existing 150 GB quota)

The RAM canaries never start an existing database, because Incus can start persisted instances before any API audit.
With persistent state, that rule becomes: **a persisted database starts again only after the controller recorded a
clean stop of the previous run.**

| | |
|---|---|
| Operation | `persist-smoke` (operation 6), its own token action bound to the seat image. Seat `polyseat-persist-cpu`: the M1 desktop seat (CPU, no NIC, no GPU), with `boot.autostart=false` |
| Location | `/mnt/cache/polyseat/incus` (root 0711) with `incus/` (`INCUS_DIR`, 0711), `persist.json` (Incus version) and the clean-stop record |
| Before every start | the dataset `cache/polyseat` is mounted at exactly `/mnt/cache/polyseat` with a quota and at least 8 GiB free. Its mount root is `root:root` and not group- or other-writable: the pool root above it is Unraid's (0777), but nothing can rename or replace a mounted dataset root |
| First run | creates the workspace, pool, image and seat; the image is deleted after `init`, as before |
| Later runs | need `clean-stop.json` (root 0600, every instance `Stopped` with `boot.autostart=false`) and the same Incus version (no silent up- or downgrade). The record is consumed **before** the daemon starts, so a crash from there on means inspection, not a restart. After start, the persisted seat must be found stopped; nothing starts by itself |
| Clean stop | every seat stopped and non-autostarting, then `incus admin shutdown`. The daemon must exit **by itself**; only then is the record written. A killed or hung daemon never counts as clean |
| Failure path | stop the seat, never delete it; no record, so the next run refuses |
| Budget | free space on the dataset (its quota caps the size) instead of walking the rootfs every second |
| Proof | unit tests; CI job `persist-canary` with a real ZFS pool `cache` and dataset `cache/polyseat`: run A fresh, run B reused (nothing autostarted), run C refused without a record; then Tower |
| Tower setup | `/mnt/cache/polyseat` back to `root:root 0755`. "New Permissions" had set it to `nobody:users 0777`, and it now excludes `polyseat` |

## Step 3 design: polyseatd on the LAN, no seats (owner, 2026-11-01: "muss in LAN sein")

| | |
|---|---|
| Binary | upstream `cmd/polyseatd`, built static (`CGO_ENABLED=0`, `-trimpath`) in the CI build job. The canary controller **pins its SHA-256** at package build time. `daemon-stage FILE` (its own token action) copies only that exact binary into RAM (`/var/lib/polyseat-unraid-daemon`, root 0700). The controller package itself stays script-only |
| Password | upstream lets whoever opens the interface first choose the password. On the LAN that window must never open, so `credentials.json` (argon2id, written by upstream's own `internal/auth` through `unraid/cmd/polyseat-passwd`) must exist in `/mnt/cache/polyseat/polyseatd` (root 0700/0600) before the first start. Without it nothing starts |
| Interface | `listen` is exactly the single IPv4 address of `br0` on port 47800 (TLS, upstream's login). The controller checks `/proc/net/tcp`: that one listener and no other, and `setup:false` |
| Config (written per run, RAM) | `update_check`, `web_update`, `web_uninstall` and `web_lan_bridge` all false: prepare, update, restart, uninstall and the LAN bridge answer 403. `uplink` br0; state and library on `cache/polyseat` |
| Run | `polyseatd-session` (operation 7) starts the persistent Incus (clean-stop rule from step 2) and then polyseatd, in its own child of the canary cgroup (budget, watchdog, hard stop). Its `PATH` starts with the deny-tools. The controller itself only reads Incus and shuts it down |
| While running | polyseatd reaches the Incus API directly, so every 10 s the policy guard runs and every seat must still be stopped; anything outside the policy ends the run. Status every 30 s (`polyseatd-status`); `polyseatd-stop` ends it |
| Stop | SIGTERM; polyseatd must exit by itself within 30 s. Then the Incus clean stop and `/run/polyseat` removed (plain files only) |
| Measured locally (disposable container, real runtime) | at start polyseatd changes nothing in Incus. `/api/setup` gets 409 when a password exists, a wrong login 401, `/api/state` without login 401, the five host actions 403. Its uhid observer backs off on the missing python3 (input is a later step) |
| Proof | unit tests; CI run D in `persist-canary` (fixture `br0` 10.77.0.1, every HTTP check from outside, localhost not served); then Tower |

Unchanged throughout:
- no main merge, no release, no unreviewed `.plg`;
- every Tower step needs its own gate;
- the GPU, VM and br0 rules of [gpu-path.md](gpu-path.md) and [steam.md](steam.md).

Later options outside this plan, not a current task: an UnraidServerDev/UbuntuDev container, or a CachyOS VM on the
Intel iGPU with Moonlight.
