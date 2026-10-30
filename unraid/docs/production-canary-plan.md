# 🚧 WIP — DO NOT USE — Minimal-invasive CPU-only canary on the production Unraid (PLAN ONLY)

**Status: target decision, not an approval.** On 2026-10-30 the owner decided that the first small CPU-only compatibility
test should run on the production Tower instead of a separate test machine. Nothing in this document authorizes any
change. The Tower stays strictly read-only until the owner approves **exactly the actions** listed in the Go/No-Go report
(see "Approval protocol"). `polyseat-unraid.plg.disabled` stays inert; there is no installable `.plg`. A canary is not a
production release.

Inventory facts below come from read-only SSH on 2026-10-30 (details in
[host-inventory-unraid-7.3.3.md](host-inventory-unraid-7.3.3.md)).

## 1. What the canary is

One signed bundle (Incus 7.5.1 + LXC 6.0.6, no AppArmor/SELinux support, no GPU, no VM driver use), one unprivileged
BusyBox container with a disk-only profile (no NIC, no device), a daemon with a private root-only socket and no network
listener, started and stopped by `polyseatctl` for a bounded window, then uninstalled with `DATA_DIR` removed by the owner.

Not part of the canary, ever: GPU, PCI/USB devices, VMs, networks or bridges, Docker, libvirt, shares, users, services.

## 2. Pre-conditions that must be closed first (all fail-closed and verifiable)

| # | Condition | Status |
|---|---|---|
| P1 | Signed bundle: a maintainer-held release key signs the manifest of the exact CI artifact; key policy in [signing-key-policy.md](signing-key-policy.md); `allowed_signers` pinned; revocation list supported and tested | policy + revocation implemented; **no release key created, nothing signed** |
| P2 | Provenance: bundle SHA-256 = CI artifact = local rebuild comparison documented; commit SHA, pins, run URL in `TEST-REPORT.md` | Local cold/warm dev bundles at `2e2c94f` have identical SHA-256 (`3e972d5b…`); independent CI/release-artifact comparison **open** (dev differs by design) |
| P3 | Runtime SBOM + dependency review: `SBOM-runtime.cdx.json`, OSV lookup, `govulncheck` binary mode, human triage of every advisory | SBOM + OSV report implemented; **triage open**, see [security-analysis.md](security-analysis.md) |
| P4 | Resource confinement: the daemon and all containers run inside a dedicated cgroup v2 subtree with `memory.max`, `cpu.max`, `pids.max`, `cpuset` on E-cores only, plus `cgroup.kill` as hard stop | **not implemented** (design only), must pass in CI |
| P5 | Gate `canary`: a third release-gate value that needs a **signed, time-limited, bundle-bound approval file** on the flash; `test-host` (disposable marker) must not be reusable on production | **not implemented** |
| P6 | Profile policy: the controller refuses any profile or instance device except `disk root` (no `gpu`, `pci`, `nic`, `usb`, `unix-*`), because Incus calls `modprobe` (e.g. `vfio-pci`) only for such devices, QEMU VMs and `linux.kernel_modules` | verified in the pinned source; **enforcement not implemented** |
| P7 | Host-state diff proven in CI: modules, netfilter, sysctls, addresses, listeners, mounts, top-level cgroups before/after a full run | **CI passed at `2e2c94f`**; nftables rules are compared without volatile packet counters, actual firewall-rule and sysctl changes still fail; Unraid-specific behaviour untested |
| P8 | Controller package built with the canary gate and signed; PLG template rendered by a separately approved release job | **not done** |
| P9 | A dry run of the whole procedure on a Linux VM/CI with identical commands | CI e2e covers the controller; the **Unraid-specific steps are untested** |

## 3. Read-only preflight (safe to run any time; already partly done)

`ssh tower 'sh -s' < unraid/scripts/preflight.sh` and `polyseatctl preflight` print facts only and write nothing on the
host. Required result before a Go: Unraid ≥ 7.2 (found 7.3.3), kernel ≥ 6.12 (6.18.54), cgroup2 unified with
`cpuset cpu io memory hugetlb pids` (found), `max_user_namespaces` > 0 (320422), seccomp (kernel interface present),
no foreign `incusd`, `/dev/net/tun` irrelevant (no NIC), pools `ONLINE`, array started, no parity check or rebuild
running, no mover/backup job running, `MemAvailable` ≥ 6 GiB, load average (5 min) ≤ 8, `cache` ≥ 20 GiB free.

## 4. Impact list (everything a canary run would touch)

**Flash `/boot/config/plugins/polyseat-unraid/`** (ZFS flash here, 12 GB free; vfat on other systems):
`polyseat-unraid.cfg`(+`.bak`), `allowed_signers`, `revoked_keys`, `bundles/<sha>.{tar.gz,manifest.json,manifest.json.sig}`
(~60 MB), `state/{pointer,journal}.json`, `pkg/*.txz` (34 KB), the canary approval file. Nothing else on the flash;
`/boot/config/go`, `domain.cfg`, `docker.cfg`, other plugins are not touched.

**RAM root (lost at reboot, counts against memory, ≈ 130 MB per slot, 2 slots ≈ 260 MB):**
`/usr/local/polyseat-unraid/`, `/usr/local/emhttp/plugins/polyseat-unraid/` (installpkg), `/usr/local/sbin/polyseatctl`
(symlink), `/var/run/polyseat-unraid/`, `/var/log/polyseat-unraid/` (≤ 1 MB + rotation; `/var/log` tmpfs is 128 MB),
`/var/lib/polyseat-lxc/rootfs` (0755, empty mount point), `/etc/subuid` and `/etc/subgid` (managed block, currently
empty files; no overlap with any other owner).

**Persistent data:** `DATA_DIR` = a new directory under `/mnt/cache/appdata/` (ZFS, no new dataset, no quota change).
Budget ≤ 1 GiB: Incus database, dir storage pool, one BusyBox container (~5 MB). The Unraid appdata backup will include it.

**Processes/sockets/cgroups:** `incusd` (+ `incusd forklxc/forkexec` helpers per container), one container init; a unix
socket in `DATA_DIR/incus` (root only), **no TCP listener** (health check fails on any `core.https_address`); a new cgroup
subtree (P4). No Docker/libvirt interaction. The `cgroup2-unraid` daemon removes empty cgroups; its effect on the subtree is
**untested** (abort criterion).

**Kernel/network:** no module is loaded by a disk-only container workload (pinned-source review; to be proven by the CI
host-state diff); no bridge, veth, route, sysctl or netfilter change; Incus only *detects* firewall tools. Incus' VM driver
sees `qemu-system-x86_64` on the host but is never used.

**Not touched:** GamingVM (CPUs 2–15, RTX 5070 Ti + NVMe + USB controller on `vfio-pci`), UbuntuServer VM, CachyOS VM,
the 32 Docker containers, libvirt, `br0`/`docker0`/`virbr0`, WireGuard/Tailscale, shares, array, pools.

## 5. Budget and caps

| Resource | Budget | Why |
|---|---|---|
| RAM | daemon+container cgroup `memory.max` 512 MiB, `memory.swap.max` 0; RAM root 260 MB | `MemAvailable` was 7.7 GiB, no swap; ARC 5.3 of 10 GiB; the 36 GiB VM could be an OOM victim |
| CPU | `cpuset` 24–27 (E-cores), `cpu.max` 100000 1000000 (10 % of one core ≈ 0.1 CPU), nice 19 | GamingVM uses 2–15 + emulator 0–1; UbuntuServer is unpinned over 0–27 |
| Disk | DATA_DIR ≤ 1 GiB, flash ≤ 200 MB | 272 GiB free on `cache`, 12 GiB on flash |
| Time | maintenance window ≤ 30 min, all commands wrapped in `timeout` | |
| Processes | `pids.max` 256 | |

## 6. Hard abort criteria (any one: stop, run the rollback, report; do not retry)

1. Before start: any preflight value out of range in §3, a snapshot/backup job running, GamingVM or another VM not in its normal state.
2. During: `MemAvailable` < 4 GiB, load (1 min) > 14, any `vfio`, `nvidia`, `zfs`, `oom`, `Out of memory`, `BUG`, `Call Trace`
   or `cgroup` error in `dmesg` that was not present before, any change in the set of running Docker containers or libvirt
   domains, any difference in `ip -br link`, `iptables-save`, listening ports, or loaded modules, pool not `ONLINE`.
3. Controller/Incus: health not reached in 60 s, journal pending, signature/revocation/ABI/LSM gate refusal, `cgroup2-unraid`
   removing a subtree directory under a running container, any unexpected exit code.
4. Owner decision, or loss of the independent access path (§8).

## 7. Backup status found by read-only verification (the flash backup alone is not enough)

| Asset | Covered? | Evidence / gap |
|---|---|---|
| Flash (`/boot`) | **Yes**, daily | dataset `backupssd/backup_unraid/flash`, 6 snapshots, newest 2026-10-30 02:12; contains `config` incl. keys and VM XML backups. Restore never tested. |
| Appdata | **Yes**, daily | `backup_unraid/cache/appdata` (308 GB), 7 snapshots, newest 2026-10-30 02:02 |
| libvirt config (`libvirt.img`, VM definitions) | **Yes**, daily copy | `system/libvirt/libvirt.img` (1 GiB) present in the 2026-10-30 event snapshot |
| UbuntuServer vdisk (300 GB, running VM) | **Crash-consistent copy** | `vm/UbuntuServer/vdisk1.img` copied 2026-10-30 02:02 while the VM runs |
| **GamingVM storage** | **NO** | the VM has no file disk: a whole NVMe (WD SN810/SN850) and a USB controller are passed through (`vfio-pci`); nothing backs up that NVMe |
| CachyOS VM (stopped) | **NO** | uses raw `/dev/sdf` (931 GB SSD) |
| Docker "snapshots" | **Not backups** | `cache/docker/<hash>@…` are the ZFS storage driver's layer snapshots; container *data* lives in appdata (covered). Image layers (45 GB) are re-pullable, not backed up |
| Array shares (~40 TB used) | **Not seen** | `backupssd` is 928 GB; parity status was not checked |
| Restorability of any of the above | **Unverified** | no restore test was performed or is possible read-only |

Optional extra safeguards before a canary (each is a host action needing approval, none done): a manual ZFS snapshot of
`cache/appdata` right before the window; confirmation of the last parity check; an image of the GamingVM NVMe if the owner
wants insurance against *any* host incident (the canary does not touch it). The owner must also confirm that the flash
backup restores to a bootable stick.

## 8. Independent access and maintenance window (to be confirmed by the owner)

Known: SSH from the workstation works. **Unknown and required before a Go:** a second access path that survives an
SSH/network hang (local console, IPMI/AMT, physical access), who is present, the length of the window, and a pause of
backup jobs and Docker auto-updates during it. Without an independent path the answer is No-Go.

## 9. Rollback

Normal: `polyseatctl stop` (graceful instance shutdown, then SIGTERM, then verified SIGKILL), `polyseatctl uninstall
--approve` (exact manifest-listed files, managed subuid block, `rmdir` of the mount point; settings and data kept),
`removepkg` of the exact package, owner deletes `DATA_DIR`. Then compare the host state with the baseline captured before.

Hang: every step runs under `timeout`; the cgroup subtree is emptied with `cgroup.kill` (P4); `polyseatctl recover --approve`
restores the previous bundle after an interrupted transaction; the controller's journal makes a second invocation safe.

Persistence: the canary only writes RAM files, the plugin directory on the flash and `DATA_DIR`. A reboot clears the RAM
part; the flash part is removed by uninstall or restored from the flash backup. **A reboot of the Tower is a last resort**
because it stops every VM and container and needs an explicit owner decision.

**Untested on Unraid:** all of the above, the Unraid plugin manager and `.plg` flow, `cgroup2-unraid` interplay,
idmapped mounts on ZFS, `installpkg` of the package, boot ordering, vfat flash behaviour, `cgroup.kill` on this kernel.

## 10. Approval protocol

1. Close P1–P9 and attach evidence to the Go/No-Go report.
2. The report lists every first write action by exact command, path and effect (see the template below).
3. **Wait for the owner's explicit approval of exactly those actions.** No earlier prompt, build permission or plan counts as
   approval. Any deviation, extra package, service change, Incus start or test container needs a new approval.
4. Discard the test or choose a safer alternative (CI-only evidence, or a separate machine) whenever a risk to the
   GamingVM/RTX 5070 Ti, Docker, libvirt, network, data or array cannot be excluded.

### Go/No-Go report template (to be issued before the very first write action)

| Section | Content |
|---|---|
| Artifact | commit SHA, run URL, bundle SHA-256, package SHA-256, signature fingerprint, SBOM, triage result |
| Evidence | CI e2e result, host-state diff, P1–P9 status with links |
| Window | date, duration, who is present, independent access path, paused jobs |
| Backups | which of §7 are fresh, extra snapshots taken (each approved) |
| Exact actions | numbered list: command, path, effect, rollback for each |
| Budget/aborts | §5/§6 values confirmed against the live preflight |
| Decision | GO / NO-GO by the owner, timestamp |

## 11. Current assessment

**No-Go today.** P1 (no signed bundle), P4, P5, P6 (not implemented), P8 and the independent-access confirmation are
open; the gaps in §7 (GamingVM NVMe, array, restore tests) are the owner's to accept or close. The next engineering steps
are P4–P6 with CI tests, then the signing ceremony, then the dry run.
