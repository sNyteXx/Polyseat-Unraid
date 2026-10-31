# WIP — First native CPU-only Unraid canary (preparation, not permission to start)

The owner chose production Tower for the first **small, supervised compatibility test** after Ubuntu/CI evidence.
This document defines that test; it does not authorize a write, installation, daemon start or reboot.
The repository controller remains `closed`, the ordinary package builder remains `closed|test-host`,
and `polyseat-unraid.plg.disabled` remains inert. A separately assembled, hash-bound canary kit is not a release.

## 1. One deliberately small test

The first test proves only:

1. The signed native Incus/LXC userspace passes the recorded Tower ABI check.
2. The controller starts a fresh daemon with aggregate CPU/RAM/PID limits.
3. It imports the exact bundled, pinned BusyBox image, creates **one unprivileged disk-only container**,
   starts it, executes a fixed command, stops/restarts it, then removes the test instance and stops the daemon.
4. The lifetime supervisor and emergency stop work and the recorded host state is unchanged afterward.

There is **no GPU, PCI/USB passthrough, VM, NIC, bridge, external image download, game, host share or user workload**.
There is no automatic boot start and no open-ended access to the Incus CLI. The controller owns the fixed test sequence.

**Not in this first production test:** plugin-manager installation/boot integration, persistent ZFS storage, updates,
rollback fault injection, reboot, multi-seat streaming or gaming. Update/recovery/rollback tests stay on disposable CI.
A later test must separately establish ZFS persistence and full plugin lifecycle behaviour.

## 2. Why the first data directory is in RAM

Read-only Tower inspection on 2026-10-30 established:

| Path | Owner / mode | Consequence |
|---|---|---|
| `/`, `/var`, `/var/lib` | root:root, 0755 | suitable protected parent chain |
| `/mnt/cache`, `/mnt/cache/appdata` | uid 99 / gid 100, 0777 | not a protected ancestor chain for privileged runtime state |
| `/var/lib/polyseat-unraid-canary` | absent | reserved, fresh test-only directory |

The canary uses **exactly `/var/lib/polyseat-unraid-canary`** on the existing Unraid RAM-root.
It does not chmod existing shares, create a dataset, change quotas or add a host mount.
Pre-existing Incus state is refused before start rather than trusted or automatically deleted.
No existing seat/game/configuration can be imported into this test.

This is intentionally **not a ZFS or persistence test**. RAM-root storage has no separate hard disk quota:
fixed authenticated inputs and a fixed command sequence bound expected growth, while the supervisor monitors
a 256 MiB data threshold and a 16 MiB log threshold. These are abort thresholds, not filesystem quotas.
Preparation/unpacking outside the workload cgroup also consumes host RAM and is included in preflight headroom.

## 3. Technical gates before the first test

The corresponding CI output, source commit, origin run, signatures and hashes belong in the generated kit/evidence.
A green generic workflow alone does not satisfy this table.

| Gate | Required evidence |
|---|---|
| Inputs | Exact Incus commit and annotated release tag, explicit checksum-pinned dependency overlay, pinned C sources, Go toolchain, pinned Ubuntu BusyBox DEB and binary hashes |
| Inventory and security | Runtime SBOM for **both** shipped Go binaries, host ABI closure, working OSV and govulncheck reports, individual disposition of remaining findings; scanner errors fail |
| Provenance | Origin CI run and commit, exact archive/controller/manifest hashes; local-vs-CI comparison reports real differences instead of claiming unequal builds identical |
| Signed kit | Exact source-derived canary controller, runtime, BusyBox image, plan and evidence file set; external pinned signer fingerprint; revocation checked; no live approval included |
| Fresh state | Protected parent chain, private root socket, fresh one-shot Incus database; no startup of an old instance database |
| Fixed policy | Sole default project/profile, dir pool under the private test data directory, one allowed unprivileged container; no raw settings, extra devices or network/metrics/BGP/DNS listeners |
| Resource boundary | Aggregate 512 MiB/no swap, 0.1 CPU, E-cores 24–27, 256 PIDs; real Incus and LXC processes stay under the boundary |
| Lifetime | Independent bounded supervisor active before daemon startup; wall/monotonic lifetime; emergency cgroup kill available without a responsive Incus API |
| Exact Linux rehearsal | Real **canary-gate** lifecycle, containment, expiry/teardown and termination after loss of the supervisor on a disposable GitHub-hosted Linux runner, not just a mock or the broader test-host controller |
| Host state | Before/after kernel modules, network links/addresses, TCP **and UDP** listeners, firewall rules (excluding counters), selected sysctls, mounts and top-level cgroups; unexpected persistent changes fail |
| Live read-only preflight | Actual Tower resources, tools, fresh paths, VM/Docker state and absence of conflicting maintenance jobs |

### What is not a circular prerequisite

Success of the first native container on Unraid, interaction with `cgroup2-unraid`, Unraid RAM-root/idmapping behaviour
and target-kernel behaviour are **test objectives**, not facts that can be proven before that first test.
They remain explicit risks and abort conditions. Full PLG/boot/ZFS behaviour is outside this first test.

### What the local build means

UbuntuDev is for fast compiler, dependency, scanner and offline-test feedback using the same pinned build core.
Local-dev artifacts remain labelled local-dev and are not silently relabelled as approved CI artifacts.
A same-source local build may have different ELF bytes because of toolchain paths and build flags.
Those differences must be recorded honestly; signing always binds the **exact chosen candidate bytes**.

## 4. Resource and host guardrails

| Resource | Limit / condition |
|---|---|
| Workload memory | `memory.max=536870912`, `memory.swap.max=0` |
| Workload CPU | `cpu.max=100000 1000000`, cpuset 24–27, nice 19 |
| Workload tasks | `pids.max=256` |
| Supervisor | Separate small bounded cgroup, outside the workload OOM domain; exact values in rendered controller |
| RAM-root data / logs | Monitored abort thresholds 256 MiB / 16 MiB, not claimed as hard quotas |
| Approval lifetime | At most 1800 seconds; issued only for the actual owner-approved window |
| Before start | MemAvailable at least 6 GiB, 5-minute load at most 8, no conflicting maintenance job |
| During test | Abort if MemAvailable below 4 GiB, 1-minute load above 14, unexpected host/service/kernel change |

The host's shared cgroup controllers are inspected, never enabled/reconfigured by the canary.
LXC's root-container default can otherwise escape the daemon's current cgroup: daemon inheritance by itself is insufficient.
The confined launcher therefore needs its reviewed private namespaces and a real CI containment check.
Private namespace mounts must not become host-global mounts.

The absence of AppArmor/SELinux on Tower is not hidden: user namespaces, seccomp, capabilities and cgroups reduce exposure
but this is still experimental root-operated software. A malicious host root can bypass a root-owned controller;
the policy is not advertised as a security boundary against an already compromised administrator.

## 5. Exact first-write scope

The final Go report must use the kit's actual hashes, signer fingerprints and commands, not placeholders.
Preparation may build, inspect and sign files on the development machine; Tower remains read-only until that report is approved.

| Area | Allowed first-test effect | Explicitly excluded |
|---|---|---|
| Flash plugin directory | Exact hash-addressed bundle, signed manifests, pinned public signers, private configuration/transaction state and current approval | Changes to `config/go`, Docker/libvirt settings, other plugins or license |
| RAM runtime/controller | Exact manifest-owned files under Polyseat runtime/controller paths; no general plugin auto-install | Broad library replacement, system package update, autostart |
| Private test data | Fresh `/var/lib/polyseat-unraid-canary` only | `/mnt/user`, cache/appdata shares, existing Incus databases, games or seats |
| Runtime bookkeeping | Private pid/log/socket files, reviewed plugin-only cgroups, empty `/var/lib/polyseat-lxc/rootfs` mountpoint, exact managed subuid/subgid block if required | Existing user mappings, host namespace mounts, foreign cgroups |
| Processes | One daemon, fixed BusyBox instance, lifetime supervisor, controller helpers | Docker/libvirt/GOW control, VMs, GPU/NVMe/USB or host kernel-module changes |

The final package/plan file set determines the exact installation/removal mechanics.
The first test does **not** require installing an auto-starting `.plg`, running `upgradepkg` broadly, or exposing the WebUI.
The controller and its matching `schema.json` may run from the verified package in a private temporary directory,
without installing any WebUI or event hook. The minimum sequence is `stage ... --approve`,
`activate SHA --approve`, `verify`, then `canary-smoke --approve`. A separate `start` is unnecessary:
the fixed smoke action includes daemon startup and teardown. Activation already runs each native binary with
`--version` under a 15-second command timeout (`incus`, `incusd` and the bundled `unsquashfs -version`); it is the
first native execution and must be included in the approved action list even though it does not start a daemon.
This short version probe is outside the daemon cgroup.
Do not set `ACCEPT_NO_LSM=yes` on Tower merely because the Ubuntu CI fixture needs it; the normal fail-closed
LSM default remains in effect and the live read-only preflight must confirm the actual target state.

### Daemon helper programs (added after run 1)

Incus 7.5.1 refuses to start unless `ip rsync setfattr tar unsquashfs xz` resolve on its PATH
(`cmd/incusd/main_daemon.go`, `neededPrograms`). Unraid 7.3.3 ships all of them except `unsquashfs`.
The bundle therefore carries exactly one pinned helper, `payload/tools/unsquashfs` (squashfs-tools 4.7.5, signed upstream
tag, GPL-2.0-or-later; `payload/share/licenses/squashfs-tools/COPYING`). It links only compression libraries that
Unraid provides and is covered by the same manifest hashes, ABI gate, SBOM, OSV lookup and artifact signature as Incus.
Nothing is installed globally: only the daemon's own PATH is `deny-tools:<slot>/payload/tools:/usr/sbin:/usr/bin:/sbin:/bin`.
Before any canary state exists the controller performs the same lookup with that PATH and, on the canary, requires
the bundled copy; a host copy never stands in for the reviewed binary. The build fails if the controller list and
the pinned Incus source drift apart.

Incus probes every storage and VM driver at start. On Tower that would run the host's own `btrfs version` and
`lvm version` and could reach `zfs`/`zpool`/`qemu-system-x86_64`, which the CI runner does not have. The daemon PATH
therefore starts with private stubs (`CANARY_DENIED_TOOLS`: `modprobe insmod rmmod btrfs lvm zfs zpool
qemu-system-x86_64`, exit 126), so those drivers report unsupported and no host storage/VM tool runs; the QEMU probe
already stops at the denied `modprobe vhost_vsock`, so `/dev/vsock` is never opened. No stub shadows a program Incus
requires (enforced by a test).

The canary health timeout is a fixed 60 s (`CANARY_HEALTH_TIMEOUT`), identical in the CI rehearsal and on Tower and
never read from user settings; the CI daemon became healthy after about 4 s.

## 6. Signing and approval are different

The canary-only artifact identity signs the exact runtime and test-kit manifests.
A **separate approval identity** signs a short-lived action list bound to the normalized Tower DMI UUID hash and exact archive SHA-256.

The approval namespace is `polyseat-unraid-canary-approval-v1`; identity is `polyseat-unraid-canary-approver`.
Approval JSON contains exactly `schema`, `host_sha256`, `archive_sha256`, `issued_at`, `expires_at`, `actions`.
Only the controller's narrow canary actions may be approved. Boot, service enablement, update and rollback are not part of this run.
Emergency stop/uninstall/recover remains possible after expiry; expiry cannot prevent cleanup.

Private keys never go into Git, CI artifacts, the kit, tool output or Tower.
A software canary-only key on the owner's development machine is weaker than hardware/separate-user custody:
a build process compromised under the same UID could read it. That tradeoff and the public fingerprints must be explicitly
accepted in the final Go. Test-generated CI keys are never production signers. See [signing-key-policy.md](signing-key-policy.md).

## 7. Abort and return path

Abort on any failed signature, revocation, ABI/policy/path/resource check; an existing daemon/database; failed health;
unexpected container behaviour; loss of the supervisor; expiry; or a new kernel/VM/Docker/network anomaly.
Do not automatically weaken a limit, broaden a capability, touch another service or retry on production.

Normal stop terminates the single instance and daemon. Emergency stop kills only the validated canary workload cgroup.
Cleanup removes only recorded plugin-owned files and empty owned cgroup/mountpoint directories; configuration/data are
kept unless the exact test-owned removal is separately listed in the approved plan. Compare host state afterward.
A failed cleanup is a reported blocker, not permission for a recursive delete or a server reboot.

A Tower reboot is outside the canary scope and always needs a separate owner decision.

## 8. Operational owner decisions immediately before the test

- Confirm the window and an independent recovery path (physical console or equivalent); SSH alone does not survive a host hang.
- Accept the exact candidate, public signer fingerprints, action list, resource limits and experimental-host risk.
- Confirm no concurrent mover, backup, parity/rebuild or auto-update job will conflict.
- Recheck the relevant backups without implying that they cover every disk or have been restore-tested.

Read-only inventory found flash/appdata backups and libvirt configuration copies. The running Ubuntu VM copy is only
crash-consistent. GamingVM's raw passed-through NVMe and the raw CachyOS disk were **not** covered by those copies.
Docker ZFS layer snapshots are not themselves a backup of application data. Restorability was not tested.
This does not require backing up all array data merely to prepare a tiny test; it does require an honest risk decision.
The canary never intentionally opens or modifies those VM disks.

## 8a. Run 1 (2026-10-31) and what changed

Run 1 (commit `76775f3`, CI 41189281490) staged, activated and verified on Tower and the native `--version` probes passed,
but `canary-smoke` stopped fail-closed: `incusd` exited with `exec: "unsquashfs": executable file not found in $PATH`.
Uninstall succeeded and the host diff showed only plugin-owned residue. The CI runner had installed squashfs-tools,
so no rehearsal could see the gap. Changes for run 2:

- bundled, pinned `unsquashfs` and the pre-state program check above;
- the CI rehearsal runs with a Tower-like PATH built from the recorded Tower inventory
  (`unraid/abi/unraid-7.3.3-x86_64.tools`), reproduces the run-1 failure on purpose, proves the controller refuses a
  missing program before creating state, runs the exact Tower order (`canary-smoke` starts the daemon) and records a
  complete per-process exec inventory that fails on any host program Unraid lacks;
- `cgroup2-unraid` removes emptied top-level groups on its own; a group that is already gone after `cgroup.kill`
  counts as removed (the kernel only removes empty groups), and a separate leak check fails if any bundled binary or
  supervisor/witness/launcher process survives;
- run-1 residue on Tower (four flash trust/token files, RAM evidence, the retry-blocking
  `/var/lib/polyseat-unraid-canary`) is never removed automatically; the run-2 Go report lists its exact-path restoration.

## 9. Handoff / Go report

The generated candidate must include:

- Exact repository commit, CI run, archive/package/controller/BusyBox hashes, pinned signer fingerprints and expiry.
- Results of offline tests, generic lifecycle CI, exact canary rehearsal and security triage.
- Exact numbered first-write commands, affected paths, measured budgets, stop and rollback commands.
- Fresh read-only Tower findings and the remaining owner-only decisions in section 8.

**Stop before the first Tower write.** The next action after a completed technical preparation is the owner's approval
of that concrete run, not another speculative implementation phase or an unannounced production test.
