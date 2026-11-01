# Owner mode: playing from the Unraid WebUI (installed plugin)

Owner decisions of 2026-11-01: (1) an Unraid admin clicking in the WebUI replaces the signed per-session token, every
other protection stays; (2) a real plugin is installed on the flash; (3) after an Unraid reboot **nothing starts by
itself**, a session is started from the WebUI.

Owner mode is a property of the **package build**, not a setting: `build-plugin-package.sh VERSION OUT canary` with
`POLYSEAT_APPROVAL_MODE=owner` writes `POLYSEAT_APPROVAL_MODE="owner"` into the controller. Owner mode exists only
with the canary gate; every other combination refuses all mutations. The committed source (and any build without the
variable) keeps `"signed"`: nothing in this document applies to it, and the signed path is unchanged.

## Workflow

| Step | What happens |
|---|---|
| Install | The `.plg` (packaging is not part of this change) puts the controller, WebUI and the files below on the flash. It must **not** call `activate --approve` any more: in owner mode that needs the host-local approval that only `play-start` writes. `recover --approve` and `uninstall --approve` stay possible (emergency teardown). |
| Start | WebUI → Polyseat card → **Start** (or `polyseatctl play-start` as root). See the sequence below. It returns as soon as polyseatd-play is launched. |
| During | The card shows address, seats and the time left. Polyseat's own page (opened from the card) manages seats and pairing. |
| Stop | Card → **Stop** (typed confirmation `STOP`), or `polyseatctl play-stop`. Seats stop in upstream's order, Incus gets its clean-stop record, the host is restored. The session also ends by itself when `PLAY_SESSION_HOURS` are over. |
| Array stop / shutdown | `event/stopping_svcs` asks a running session to stop and waits up to 5 minutes for it before Unraid unmounts the pools (`play-stop --wait 300`). Only then the old emergency `stop` runs. |
| Between sessions | A finished session leaves `canary-session.json`, `canary-guardian.pid`, `canary-emergency-stop` and `play-evidence/` in RAM. `play-start` removes the records (and keeps the last evidence as `play-evidence.last`) because it holds the lock and nothing runs; it **refuses** over `play-host.json`/`play-console.json` (unrestored host), a watchdog `.reason`, a live cgroup or daemon. Incus' own gate stays: no clean-stop record, no start. |
| Reboot | RAM is gone: the approval, the runtime slot, the staged seat image and daemon vanish with it, and the host is as Unraid boots it. `event/disks_mounted` runs `polyseatctl boot`, which in owner mode only verifies and logs. **Nothing starts.** |
| Uninstall | `plugin remove` runs `uninstall --approve` (stops anything running, removes exactly the manifest-listed runtime files, the staged seat image and daemon, the subuid block). Settings, the Incus state and the game library are never removed. |

## What `play-start` does (root, owner mode, takes the controller lock)

1. Refuses if a session or Incus daemon runs, if another controller operation holds the lock, if an earlier session left
   host changes unrestored (`play-host.json`, `play-console.json`), if its evidence directory or cgroup is still there.
   It also runs the cheap prechecks of the session itself (staged daemon, password, LAN address, library dataset,
   budgets, CPU pinning syntax, GPU preflight), so a refusal reaches the admin instead of only the log.
2. Picks the runtime bundle on the flash (the highest `version` among complete, hash-named triples; an equal version with
   two different hashes is ambiguous and refused), verifies it exactly as `stage` does (archive hash, manifest, release
   signature, listing, member hashes, host compatibility) and hashes the seat image and the daemon bundle.
3. Writes the **host-local approval** `/var/run/polyseat-unraid/owner-approval.json` (root 0600).
4. `activate` if that bundle is not current; if it is current but its RAM slot is gone (after a reboot) the slot is
   rebuilt from the flash, as `boot` does in normal mode.
5. `seat-image-stage`, then `daemon-stage` (each only if the staged copy is not already the same bytes).
6. Launches `polyseatctl polyseatd-play --approve` detached (`setsid -f`, output appended to the controller log). The
   controller lock is handed to that process, so there is no window in which a second start could slip in. It returns
   at once: *"play session starting; watch the card"*.

The approval binds the host (DMI product UUID hash, as canary approvals do), the exact bundle, seat-image and daemon
SHA-256, the actions `stage activate seat-image-stage daemon-stage polyseatd-play`, and expires at
`now + PLAY_SESSION_HOURS * 3600` (setting 1..24, default 6). The signed cap of 6 hours (`PLAY_MAX_WINDOW`) is **raised
to 24 hours in owner mode only** (`PLAY_OWNER_MAX_WINDOW`), because otherwise the setting's range would be a lie and
there is no human to re-sign; the budget watchdog applies the same window. A signed build still caps at 6 hours.

## Approval in each mode

| | signed (default) | owner |
|---|---|---|
| Approval | Ed25519 token from another machine, `canary-approval.json` + `.sig` on flash, ≤ 30 min (6 h for polyseatd-play) | `owner-approval.json` written by `play-start` in the RAM run directory, ≤ 24 h |
| Checks | signature + revocation list, host, bundle/seat image by hash, action, expiry | the same without a signature: file private to root, host, bundle/seat image/daemon by hash, exact action set, expiry |
| Other signed tokens | needed | not consulted |
| `config-set` / `config-reset` | token action | no token (an admin's click); the canary whitelist of settings still applies |
| `start` / `restart` (service) | token | no action covers them: refused. A session is started with `play-start` only |
| `play-start` | refused | root only |
| `play-stop`, `play-status` (aliases of `polyseatd-stop`, `polyseatd-status`) | token-free | token-free |

## Files on the flash (`/boot/config/plugins/polyseat-unraid/`)

| File | Content |
|---|---|
| `polyseat-unraid.cfg` | settings (never removed) |
| `allowed_signers` | the release key; bundle, seat image and daemon signatures are verified against it |
| `bundles/<sha256>.tar.gz`, `.manifest.json`, `.manifest.json.sig` | runtime bundle(s) (existing layout) |
| `seat/polyseat-desktop.tar.xz`, `seat/seat.manifest.json`, `seat/seat.manifest.json.sig` | the signed seat image (namespace `polyseat-unraid-seat-image`) |
| `daemon/polyseat-daemon.tar` (+ `daemon/polyseat-daemon.tar.sig` unless the controller pins its hash) | the polyseatd bundle |
| `state/pointer.json`, `state/journal.json` | active bundle and transaction journal (existing) |

In RAM (gone at reboot): the runtime slots, `/var/lib/polyseat-unraid-seat-image`, `/var/lib/polyseat-unraid-daemon`,
`/var/run/polyseat-unraid/*` (approval, lock, status, evidence). Persistent state lives on `cache/polyseat` as before
(Incus, polyseatd credentials, Steam) and in the game library dataset.

## What is restored after a session

Unchanged from operation 8, now also for the two new host changes: `kernel.sysrq`, the tty1 keyboard, the udev rule, the
device-owner helper and the uhid module, the Incus clean-stop record and the cgroup. New in `play-host.json`:
`energy_performance_preference` of every CPU if `PLAY_CPU_PERFORMANCE=yes` (recorded, set to `performance` for the
session and written back value by value; an unreadable file or an odd value refuses the start before anything is
changed; a host without the files is skipped; a failed restore keeps `play-host.json`, so the next start refuses).
`PLAY_SEAT_CPUS` (`0-15` for all seats, or `seat1=0-7;seat2=8-15`; a seat not named is not pinned) is checked against
the CPUs the play cgroup may use (`0-27` on Tower) when saved and when a session starts; every ten seconds the loop gives
every thread of every process in a seat's `lxc.payload.<seat>` cgroup that list with `taskset` (idempotent; nothing
outside a seat payload cgroup is touched; affinity ends with the process, so there is nothing to restore).

## Still needs an owner GO

- The `.plg` itself: its install script must drop the `activate --approve` line; flash placement of the seat image and
  daemon; rendering and signing; the actual installation on Tower.
- Setting `PLAY_CPU_PERFORMANCE=yes` and CPU pinning on Tower for the first time (granted in principle on 2026-11-01).
- Lengthening `PLAY_SESSION_HOURS` beyond 6 on the real host.
- Anything not listed above: GPU, VM and br0 rules stay as in [gpu-path.md](gpu-path.md) and [steam.md](steam.md).
