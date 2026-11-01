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
| Abnormal end | The card shows **Wiederherstellung nötig** with the reason and the **Wiederherstellen** button (typed confirmation `WIEDERHERSTELLEN`, or `polyseatctl play-recover` as root). Start stays refused until it has run. See *Recovery after an abnormal end* below. |
| Between sessions | A finished session leaves `canary-session.json`, `canary-guardian.pid`, `canary-emergency-stop` and `play-evidence/` in RAM. `play-start` removes the records (and keeps the last evidence as `play-evidence.last`) because it holds the lock and nothing runs; it **refuses** over `play-host.json`/`play-console.json` (unrestored host), a watchdog `.reason`, a live cgroup or daemon, and a persistent workspace without a valid clean-stop record; each of these refusals points at **Wiederherstellen**. Incus' own gate stays: no clean-stop record, no start. |
| Reboot | RAM is gone: the approval, the runtime slot, the staged seat image and daemon vanish with it, and the host is as Unraid boots it. `event/disks_mounted` runs `polyseatctl boot`, which in owner mode only verifies and logs. **Nothing starts.** A session that ended abnormally before the reboot still has no clean-stop record (it lives on the cache dataset); the card shows `no-clean-stop` and **Wiederherstellen** records it (the RAM records and cgroups are gone, so there is no host to restore). |
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

## Memory floor: the session ends gracefully first

A play session may load the host fully on purpose, but not to the point where Unraid itself is short of memory. The
supervisor (an independent process in its own cgroup, once a second) reads `MemAvailable`:

| `MemAvailable` | What happens |
|---|---|
| ≥ 4 GiB | nothing |
| < 4 GiB (`CANARY_HOST_MIN_AVAILABLE_KB`) | The supervisor writes `play-end-reason` (`memory-floor`) and then the session's own stop request (`polyseatd-stop`). The session ends exactly as `play-stop` ends it: polyseatd, the seats in upstream's halt order, the Incus clean stop, the host restore. No `.reason` is written (it would make every `incus` call of the stop fail). The card then says *Session wegen Speichermangel beendet*, and the next start needs nothing. |
| < 2 GiB (`CANARY_HOST_HARD_AVAILABLE_KB`) at any time | Hard stop at once: `cgroup.kill`, reason `memory-floor-hard`. 2 GiB is what the host needs to run the stop itself; below it there is no time to be gentle. |
| 90 s (`CANARY_FLOOR_GRACE_SECONDS`) after the first sample below 4 GiB, session still not gone | Hard stop, reason `memory-floor-timeout`. The bound holds even if the memory has recovered meanwhile. A stop that needs longer than 90 s (two seats at 40 s + 90 s each, worst case) is cut here, which is what the single floor did before. |

The thresholds are constants in the controller, not settings. Only play sessions behave like this; every other operation
keeps the single hard floor and its `resource-policy-or-budget` reason. A hard stop leaves what a killed session leaves; the
next section recovers from it.

## Recovery after an abnormal end

A session can end without its clean stop: the process hosting it is killed, the supervisor hard-stops it (memory,
`expired`, GPU taken by a VM, `resource-policy-or-budget`), or its graceful stop fails. polyseatd, incusd and the seats are
gone, but no Incus clean-stop record exists (Incus' own gate then refuses the next start, by design), and the console
guard, the CPU energy profile and the watchdog's `.reason` may be left behind.

`polyseatctl play-status` (and `polyseatd-status`) print one JSON line with a `state`: `idle`, `busy` (the controller lock is
held and no session status exists yet: starting, or another operation), `running` (the status file plus `state`) or
`needs-recovery` with a `reason`, plus `last_end` (how the last session ended). The exit code is unchanged: 0 only while a
session runs. `needs-recovery` is a fact, never a label: the watchdog's `.reason`, a `play-host.json`/`play-console.json`
that was not restored, the status file of a session that is gone, or a persistent workspace without a valid clean-stop record.
Reasons the card knows: `memory-floor`, `memory-floor-hard`, `memory-floor-timeout`, `expired`, `gpu-ownership-changed`,
`resource-policy-or-budget`, `host-not-restored`, `session-process-gone`, `no-clean-stop`, `unknown`.

The **Wiederherstellen** button is on the Polyseat card (Tools → Polyseat → Sessions & seats) **only** while the status says
`needs-recovery`, in an owner build, with the gate open. It runs `play-recover` (root, owner approval mode only: a signed
build refuses it; refused at once if anything holds the controller lock). It does exactly what the operator did by hand, in
this order, and nothing else:

1. **Prove that nothing can run:** no `incusd` and no `polyseatd` process; no process in any `lxc.payload.*` or `lxc.monitor.*`
   cgroup or in the plugin's own cgroups; no process still executing a binary of the runtime; neither polyseat cgroup exists;
   no mount at or below the persistent data directory.
2. **Prove the host can be restored exactly:** the persistent workspace is the validated one (`persist.json`); `play-host.json`
   is a record `play_host_on` wrote (a console record without it is refused); if there is a console record, the staged
   `polyseat-console` exists and its bundle verifies, otherwise the recovery refuses and the console is not touched.
3. **Prove that no instance can autostart:** the names in `clean-stop.used.json` plus every directory under
   `storage-pools/default/containers` (all must be seat names; every one needs a plain `backup.yaml` whose own `container`
   block has exactly one `boot.autostart` that is the quoted string `"false"`, a matching `name`, and no other value in
   `expanded_config`). An instance of the last clean stop whose directory is gone is *not proven* and refuses.

All of that runs before anything changes, and every failure is logged; the card shows the first one and how many more the
log has. Only when everything passed: **restore the host** from `play-host.json` (`play_host_off`: CPU energy profile,
device-owner helper, udev rule and reload, `uhid` if the record says it was loaded, the console keyboard, sysrq), **look once
more** that nothing runs, **write `clean-stop.json`** (root 0600, the schema the controller writes, every instance
`Stopped`/`autostart "false"`, plus a `recovery` note saying what was verified; a valid record that is already there is kept),
and **clear** the watchdog's `.reason` and the session records (`canary-session.json`, the guardian and approval files,
`polyseatd.pid`, `polyseatd-status.json`, the stop request, `play-end-reason`). `play-last-end.json` keeps the reason for the
card; the evidence directory stays for the next start to rotate. The command is repeatable: a failed attempt changes nothing
before the host step, and a later one finds the same checks. `play-start` points at this button in every refusal that comes
from an abnormal end.

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
| `play-recover` | refused | root only |

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
