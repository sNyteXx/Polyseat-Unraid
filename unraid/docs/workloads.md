# Workloads: a permanent Incus on Tower for the owner's own containers

Owner decision 2026-11-01: the owner's dev/services VM ("UbuntuServer" = UbuntuDev: about 30 docker containers and a
Tailscale subnet router) moves into an Incus **system container** on Tower. That needs an Incus daemon that lives as long as
the array does. Play sessions keep their own, session-scoped daemon exactly as before ([plugin-owner-mode.md](plugin-owner-mode.md)).

Owner-mode builds only (`POLYSEAT_APPROVAL_MODE="owner"`, canary gate). A signed build refuses every `workloads-*` verb, and
nothing here changes its behaviour. The migration itself (rsync of the VM into the container, cutover, rollback) is the
operator's, following `ubuntudev-migration/PLAN.md`; the controller only runs the daemon and keeps it inside a policy.

## How it differs from play

| | play session | workloads |
|---|---|---|
| Started by | `play-start` (WebUI Start); **never** at boot | `workloads-start`; at array start only if `WORKLOADS_ENABLED=yes` |
| Lives | until `PLAY_SESSION_HOURS` are over or Stop | until Stop or array stop |
| Incus state | `/mnt/cache/polyseat/incus` (pool `default`) | `/mnt/cache/polyseat/workloads/incus` (pool `workloads`, `dir` driver) |
| cgroup | `polyseat-unraid-canary` (+ watchdog group, memory floor, `memory.oom.group`, `cgroup.kill` on expiry) | `polyseat-unraid-workloads`: a cpuset and an optional memory cap. No oom group, swap/pids limit, watchdog, expiry or memory floor |
| Lock | the controller lock, held for the whole session | `$RUN_DIR/workloads.lock`, held by its supervisor; never waits behind a session |
| Instances | upstream's seats (policy: `play_seat_guard`) | only `WORKLOADS_INSTANCES`, in the shape below |
| Daemon env | deny-tools (`modprobe zfs ...` stubs, read-only `nft`), PATH starts with them | the same stubs, in a directory of its own |

Same signed runtime slot (`/usr/local/polyseat-unraid/current`), same `canary_launch` (private mount/pid/cgroup namespaces, the
tmpfs `switch_root` that lets LXC pivot on Unraid's rootfs). Two daemons run side by side in different mount namespaces.

## Files and paths (what is per instance, what is shared)

| | play | workloads |
|---|---|---|
| `INCUS_DIR` | `/mnt/cache/polyseat/incus/incus` | `/mnt/cache/polyseat/workloads/incus` |
| record / clean stop | `.../incus/persist.json`, `clean-stop.json` | `.../workloads/workloads.json`, `clean-stop.json` (`.used.json` after it is consumed) |
| cgroup | `/sys/fs/cgroup/polyseat-unraid-canary`, `-watchdog` | `/sys/fs/cgroup/polyseat-unraid-workloads` (+ `daemon` leaf) |
| pid file | `$RUN_DIR/incusd.pid` | `$RUN_DIR/workloads-incusd.pid` |
| client config (`INCUS_CONF`) | `$RUN_DIR/client` | `$RUN_DIR/workloads-client` |
| daemon log | `/var/log/polyseat-unraid/incusd.log`, `.out` | `workloads-incusd.log`, `.out` (copy-truncated at 8 MiB: `/var/log` is a 128 MiB tmpfs) |
| LXC state | `/var/lib/polyseat-lxc/{root,hooks,deny-tools,hook-log}` | `/var/lib/polyseat-lxc-workloads/{root,hooks,deny-tools}` |
| lock / stop / status | controller lock, `polyseatd-stop`, `polyseatd-status.json` | `workloads.lock`, `workloads-stop`, `workloads-status.json` |
| **shared** | the runtime slot and its binaries; `/etc/subuid`; `/var/lib/polyseat-lxc/rootfs`, liblxc's compiled-in mount point (an empty directory each daemon uses inside its own mount namespace). A play stop used to remove it: it no longer does while `$RUN_DIR/workloads.claim` exists (taken under `lxc-shared.lock`) |

`$RUN_DIR` is `/var/run/polyseat-unraid`. The workspace is inside `cache/polyseat`, which the Fix Common Problems "New
Permissions" job already skips, and which `persist_mount_check` requires to be mounted with a quota and 8 GiB free. The
quota is 150 GB today; the owner proposed 350 GB for this (not changed by this work).

Coexistence is enforced where it could bite: the shared leak check skips the other daemon's cgroup (and an operator's `incus`
client); the shared teardown is pointed at the workloads group only (it used to remove a watchdog group by name); `activate`,
`rollback`, `recover` and `uninstall` refuse while the workloads daemon runs or is in transition, because they replace or remove
the runtime slot it executes from (they take the workloads lock for the rest of the operation).

## Lifecycle

`polyseatctl workloads-start | workloads-stop [--wait SECONDS] | workloads-status` (root, owner mode, no token), and
`workloads-recover --approve` on the console. The WebUI has the first three on the **Workloads** card (stop behind the
typed phrase `STOP WORKLOADS`) and the settings in the **Workloads** tab.

**Start** (`workloads-start`, returns once the supervisor is launched; `--boot` also waits up to 2 minutes for the pool):
1. Takes the workloads lock, refuses over a daemon or cgroup of an earlier run (`workloads-stop` clears them).
2. The active runtime must verify on the flash (one is activated once by `play-start`, or by `activate`). After a reboot its RAM slot is rebuilt from the flash, as `boot` does for
   other builds, under the controller lock (waits up to 60 s for it; a play session has the slot already and is not waited for).
3. Prechecks without any change: tools, LSM gate, `WORKLOADS_*` settings, bridge `br0`, cgroup v2 with `cpu cpuset memory pids`
   delegated, the dataset (mounted, quota, space), the workspace record (below).
4. A detached **supervisor** (`workloads-run`, holds the lock) repeats them and then: claims the shared LXC mount point,
   prepares its LXC directory and deny-tools, **loads the kernel modules**, ensures the subuid block, **consumes the clean-stop
   record**, makes the cgroup (cpuset from `WORKLOADS_CPUS`, `memory.max` from `WORKLOADS_MEMORY_MB`, read back), launches incusd,
   waits for health (socket `0600`, `info`, no network listener, daemon inside its group), bootstraps a fresh workspace
   (`storage create workloads dir`, root disk in the default profile), and runs the policy once.
5. Instances with `boot.autostart=true` are started by Incus itself; the controller never starts, deletes or execs into one.

**Running**: every 2 s the daemon must be alive and a stop request seen; every 10 s the policy runs, the card's view
(`workloads-status.json`: state, instances with state and IPv4) is rewritten and the logs are size-capped.

**Stop** (`workloads-stop`; from the WebUI, or `--wait 600` from `event/stopping_svcs`, alongside the play stop): for each
instance `incus stop --timeout 120` (forced after that, which makes the stop unclean), the policy once more, `incus admin shutdown
--timeout 60`, wait for the daemon to exit **by itself**, then the group is removed (the kill is the last resort and the only
use of `cgroup.kill`). Only if all of it was clean is `clean-stop.json` written (root 0600; instances stopped, `autostart` kept,
because a permanent container starts again next time). Everything is logged to the controller log. The array stop waits for it.

**Clean-stop record and recovery**: a workspace is reopened only with the record of a clean stop of the same Incus version. The
record is consumed *before* the daemon starts, so a crash or failed start from there on means the next start refuses ("recorded
no clean stop"). `workloads-recover --approve` writes a record `{recovered: true}` after checking, as play's rules do, that nothing
of the last run is left: no daemon, no cgroup, no process in the group or with `INCUS_DIR` set to the workspace, nothing mounted
below it. It cannot run while the lock is held. After a power loss or crash there is no clean-stop record either, so the dev box stays down until
the owner has looked and run `workloads-recover --approve`: that is the price of never opening a workspace the controller cannot vouch for. A supervisor that died with the daemon still up leaves the card at
"Daemon without supervisor"; `workloads-stop` then runs the same stop sequence in a new supervisor (and kills what is left of a
dead daemon's containers, on purpose).

**Boot**: `event/disks_mounted` runs `polyseatctl boot`; in owner mode it verifies the bundle and, only if `WORKLOADS_ENABLED=yes`,
launches `workloads-start --boot` detached. A play session is never started there.

## Kernel modules

Before the daemon starts the controller loads exactly `nft_chain_nat nft_masq nft_nat nft_ct nft_compat overlay` with
`/sbin/modprobe -- MODULE`, only those not already in `/sys/module`, logs what it loaded, and **never unloads** them (they are shared
with the host). The deny-tools still stub `modprobe` for the daemon: a container's `linux.kernel_modules` works because Incus checks
`/sys/module/NAME` first and finds the module loaded; a module outside the list stays unloadable. `modprobe` pulls its own
dependencies as usual. `nf_tables` itself was already loaded on Tower (2026-11-01).

## Instance policy

Checked at start and every 10 s (`wl_policy_check`, read-only; `wl_policy_enforce` acts). One violation of an instance: it is stopped
(`--force`) and `boot.autostart=false` is pinned, and the violation is logged once. A global violation (server config, a second
project, a managed network, a pool other than `workloads` on the `dir` driver under the workspace, a default profile with
anything but the root disk) stops every instance. A start with a violating persisted instance is therefore a few seconds of that
instance running (Incus autostarts before any audit is possible); the previous clean stop only certifies a state that was in policy.

- instance name listed in `WORKLOADS_INSTANCES`; `type container`; profiles `["default"]`; not ephemeral;
- config (and the expanded config): `image.*`, `volatile.*`, `user.*`; `boot.autostart` true/false; `boot.autostart.priority|delay`,
  `boot.host_shutdown_timeout` numeric; `security.nesting`, `security.syscalls.intercept.mknod|setxattr` true/false;
  `security.privileged` only `false`; `limits.*` (safe characters); `linux.kernel_modules` only from the list above. Nothing else
  (no `raw.*`, `nvidia.*`, `security.idmap.*`, `environment.*`, `cloud-init.*`);
- devices: the root disk on pool `workloads` (the profile's, named `root`); **one** nic `nictype=bridged parent=br0` with `hwaddr`
  set and `name` `enp1s0` or `eth0`, no other nic key (so no `security.mac_filtering`, no IP); `unix-char /dev/net/tun`
  (exactly `source=path=/dev/net/tun`); optional disks from `/mnt/user/...` or `/mnt/cache/...` with `readonly` and an explicit
  absolute `path` (never below `/mnt/{user,cache}/polyseat`, never `..`, no `shift`); optional custom volumes of the workloads
  pool. No GPU, USB, proxy, block devices, other character devices.

The controller's `run_incus` allowlist for this daemon (`canary_command_allowed`, operation 9) is only: the read queries the policy and
status need, `storage create workloads dir source=<workspace>/storage-pools/workloads`, `profile device add default root disk path=/
pool=workloads`, `stop NAME --timeout 120`, `stop NAME --force --timeout 10`, `config set NAME boot.autostart=false`, `admin shutdown
--timeout 60`. Not start, init, delete, exec, file, device or image commands: those are the operator's (below).

**Storage pool**: `dir`, as play's. The ZFS driver would give snapshots and block volumes (the plan's `/var/lib/docker` zvol), but it needs the real
`zfs`/`zpool` in incusd's PATH (denied on purpose: the daemon would be able to create and destroy datasets under the pool's source),
datasets mounted by Incus below `cache/polyseat`, and a source dataset of its own; not clearly safe here. Docker's `overlay2` on a directory in
a ZFS dataset works on Tower (ZFS 2.4.4, kernel 6.18). Snapshots are `zfs snapshot` of `cache/polyseat` by the owner's own backup tooling.

## Settings (WebUI: Settings, Workloads)

| key | default | |
|---|---|---|
| `WORKLOADS_ENABLED` | `no` | start the daemon at array start (owner builds) |
| `WORKLOADS_INSTANCES` | empty | names separated by spaces, e.g. `ubuntudev`; empty allows nothing; changes apply to the next policy check |
| `WORKLOADS_MEMORY_MB` | `0` | `memory.max` of the whole group (daemon and containers), 0 = unlimited; next start |
| `WORKLOADS_CPUS` | empty | cpuset of the group (`0-15`, `0-7,16-19`), checked against the host's CPUs; empty = all; next start |

The memory and CPU settings are locked while the daemon runs.

## Operator commands (on Tower, as root)

Instances are created and changed with the bundled `incus` client against the workloads socket, never through the controller:

```sh
inc() { env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/var/run/polyseat-unraid \
  INCUS_DIR=/mnt/cache/polyseat/workloads/incus INCUS_CONF=/var/run/polyseat-unraid/workloads-client \
  /usr/local/polyseat-unraid/current/payload/bin/incus "$@"; }
```

**Throwaway test container** (what to run first; nothing production is touched; use a test MAC, never the VM's):

```sh
C=/usr/local/emhttp/plugins/polyseat-unraid/scripts/polyseatctl
cat /sys/class/net/br0/address                                   # note it: it must not change
$C config-set WORKLOADS_INSTANCES="ubuntudev-test"               # list it BEFORE creating it, or it is stopped as unlisted
$C workloads-start && until [ "$($C workloads-status | jq -r .state)" = running ]; do sleep 2; done
grep -E '^(nft_chain_nat|nft_masq|nft_nat|nft_ct|nft_compat|overlay) ' /proc/modules | cut -d' ' -f1   # loaded by the controller
inc init images:ubuntu/24.04 ubuntudev-test --storage workloads --config boot.autostart=false \
  --config security.nesting=true --config security.syscalls.intercept.mknod=true --config security.syscalls.intercept.setxattr=true \
  --config linux.kernel_modules=overlay,nft_ct,nft_nat                  # already loaded: Incus must not need the (stubbed) modprobe
inc config device add ubuntudev-test tun unix-char source=/dev/net/tun path=/dev/net/tun        # devices BEFORE the first start
inc config device add ubuntudev-test eth0 nic nictype=bridged parent=br0 name=enp1s0 hwaddr=52:54:00:aa:bb:01
inc start ubuntudev-test && inc list
cat /sys/class/net/br0/address                                   # unchanged?
inc exec ubuntudev-test -- sh -c 'ip -br a; ls -l /dev/net/tun; cat /proc/self/cgroup; cat /sys/fs/cgroup/cgroup.controllers'
inc exec ubuntudev-test -- sh -c 'apt-get update -qq && apt-get install -y -qq docker.io'
inc exec ubuntudev-test -- docker run --rm hello-world
inc exec ubuntudev-test -- docker run --rm --memory 64m alpine true   # a cgroup limit inside: warning or error? (see "Open points")
inc exec ubuntudev-test -- sh -c 'iptables -S | head -3; nft list ruleset | head -3'
# the policy: a forbidden change must stop it within ~10 s and log it
inc config set ubuntudev-test security.privileged=true; sleep 15; inc list -c ns; tail -3 /var/log/polyseat-unraid/polyseat-unraid.log
inc config unset ubuntudev-test security.privileged
# the lifecycle: clean stop, record, reopen
$C workloads-stop --wait 600; ls -l /mnt/cache/polyseat/workloads/clean-stop.json; $C workloads-status | jq .state
$C workloads-start                                               # reopens; clean-stop.used.json is left behind
until [ "$($C workloads-status | jq -r .state)" = running ]; do sleep 2; done; inc list
# with a play session running at the same time (WebUI Start), both daemons up: play-stop must leave workloads alone and the other way round
```

Remove the test container with `inc delete -f ubuntudev-test` (the controller never deletes) and take it out of `WORKLOADS_INSTANCES`.

**The production container** follows PLAN.md section 4 with these differences: the pool is `workloads` (no zvol/ZFS volume; if a separate
`/var/lib/docker` is wanted, a custom volume of the `dir` pool); `inc` instead of `incus --project ubuntudev` (one project, `default`); the
rsync wrapper's `ssh ... incus exec` becomes `ssh root@tower "$(declare -f inc); inc exec ubuntudev --force-noninteractive -- ..."`; the NIC
`hwaddr=52:54:00:90:c2:d9` is added **after the final sync and after the VM is shut down and its autostart disabled**
(`inc config device add ubuntudev eth0 nic nictype=bridged parent=br0 name=enp1s0 hwaddr=52:54:00:90:c2:d9`); `boot.autostart=true`,
`boot.autostart.priority=10`, `boot.host_shutdown_timeout=120` and `limits.memory` are set last. List `ubuntudev` in
`WORKLOADS_INSTANCES` first. `/dev/net/tun`, nesting and the syscall interception are set before the first start (a hot-plug into a
running container is exactly what Tower's rootfs made fail for play before the `switch_root` fix; the same fix is in this daemon, untested for it).

**Rollback** (any time before the VM is deleted): `inc stop ubuntudev`, `inc config set ubuntudev boot.autostart=false`, then
`virsh autostart UbuntuServer --enable; virsh start UbuntuServer`. To retire the daemon: `$C workloads-stop --wait 600`,
`config-set WORKLOADS_ENABLED=no`. The workspace and settings are never removed by the plugin (`uninstall` refuses while the
daemon runs).

## Open points

- **No cgroup controllers inside the containers.** As in play, the daemon's group is a leaf without delegated controllers (the
  proven way to run LXC here), so Incus does not apply an instance's `limits.*` one by one (the keys are accepted). The
  group-wide `WORKLOADS_MEMORY_MB` and `WORKLOADS_CPUS` are the limits. Docker inside the container finds no `memory`/`cpu`
  controllers: it should discard `--memory` with a warning rather than fail; the `docker run --memory 64m` line above tests it. If the 30
  containers' memory limits matter, the daemon needs a systemd-style layout (a delegated parent with the daemon in its own leaf),
  which is a separate piece of work.
- **No lxcfs in the bundle**: `/proc/meminfo`, `/proc/cpuinfo` and `/proc/uptime` inside the container show the host's.
- **uid range shared with the seats**: both daemons use the same subuid block and neither isolates its idmap; separation between a
  seat and the dev container rests on the namespaces, as between two seats.
- **Autostart is Incus's**: instances with `boot.autostart=true` start before the first policy check (seconds); see the policy section.
- Quota of `cache/polyseat` (150 GB) must be raised before the container's data (about 100-150 GB compressed) is copied.
