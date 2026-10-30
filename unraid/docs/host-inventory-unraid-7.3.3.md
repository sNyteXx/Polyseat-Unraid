# 🚧 WIP — DO NOT USE — Read-only Unraid inventory (7.3.3)

Observed over SSH on **one** real Unraid host on 2026-10-30, using only read
operations (`cat`, `ls`, `stat`, `ldconfig -p`, `lspci -nnk`, `ps`, `ip -br`,
`mount`). Nothing was installed, started, written or reconfigured. `virsh`,
`docker`, `modprobe` and files containing credentials were deliberately not
used. IP/MAC addresses, UUIDs, VM names and container names are omitted.

This is evidence for **one machine**, not a certification for Unraid in
general. The captured soname set is in
[`../abi/unraid-7.3.3-x86_64.sonames`](../abi/unraid-7.3.3-x86_64.sonames).

> The task statement assumed Unraid **7.2**. The observed host runs
> **7.3.3**. Plugin `Min`/`Max` handling therefore must be tested against
> both; this repository targets `Min="7.2.0"` in its template.

## Base system

| Item | Observed |
|---|---|
| Unraid | 7.3.3 (2026-10-06), Slackware-derived, `ID_LIKE=slackware` |
| Kernel | 6.18.54-Unraid, SMP PREEMPT_DYNAMIC, x86_64 |
| glibc | 2.44 (`/lib64/libc.so.6`, loader `/lib64/ld-linux-x86-64.so.2`) |
| Init | classic SysV/BSD `rc.d`; **no systemd** (`/sbin/init`, `/etc/rc.d/rc.*`) |
| Root FS | RAM `rootfs`/tmpfs (~40 GB), recreated every boot; `/usr` is an overlay |
| Flash | `/boot` is **ZFS** here (older/other systems use vfat: no modes, no symlinks) |
| Pools | ZFS cache pool, XFS array disks, one btrfs pool, `shfs` user shares |
| Shell tools on host | bash 5.3, PHP 8.4, `jq`, `xmllint`, `ssh-keygen`, `tar`, `xz`, `flock`, `sha256sum`, `curl`, `openssl`, `rsync` |
| **Absent on host** | **`python3`**, `nft`, `socat`, `unsquashfs`/`mksquashfs`, `readelf`, LXC, Incus |

**Consequence:** the on-host controller must be POSIX/bash (+ `jq`); the
Python simulation in `unraid/lab/` cannot run on Unraid and stays a CI-only
model.

## Kernel features for Incus 7.5.1 (requires ≥ 6.12, LXC ≥ 6.0.0)

| Requirement | Observed |
|---|---|
| Kernel ≥ 6.12 | 6.18.54 — satisfied |
| Namespaces cgroup/ipc/mnt/net/pid/time/user/uts | all present in `/proc/self/ns` |
| `max_user_namespaces` | 320422 |
| cgroups | **v2 only** (`cgroup2fs`, `nsdelegate`, `memory_recursiveprot`); root controllers `cpuset cpu io memory hugetlb pids` |
| Seccomp | kernel exposes `Seccomp:`/`Seccomp_filters:` in `/proc/self/status`; `libseccomp` 2.6.1 installed |
| LSM (AppArmor/SELinux) | **none**: no `/sys/kernel/security`, no `/sys/module/apparmor` |
| `/proc/config.gz` | absent — kernel options cannot be read directly |
| `/etc/subuid`, `/etc/subgid` | **present but empty (0 bytes)** |
| `newuidmap`/`newgidmap` | installed, setuid root (shadow 4.19.4) |
| Devices | `/dev/net/tun`, `/dev/fuse`, `/dev/kvm`, `/dev/vhost-net`, `/dev/vhost-vsock`, `/dev/uinput` present |
| Filesystems | overlay, squashfs, xfs, btrfs, zfs 2.4.4, fuse, ntfs3, vfat |
| Modules loaded | veth, bridge, tun, ipvlan, br_netfilter, nf_tables, nf_nat, vhost_net, kvm_intel, nvidia, zfs |

Implications:

- Without `/etc/subuid`/`/etc/subgid` root entries Incus only supports
  **privileged** containers. Unprivileged operation needs a plugin-managed,
  clearly delimited and exactly removable block, re-created each boot (the
  files live in RAM). This is a **host modification that needs separate
  approval** on any real host.
- No AppArmor: Incus runs without LSM confinement. Container isolation relies
  on namespaces, seccomp and capability dropping only. Documented risk.
- idmapped mounts on the chosen storage (ZFS 2.4.4 / XFS / btrfs) cannot be
  proven by reading; they require a functional unprivileged-container test on
  the disposable target. Incus falls back to slow shifting otherwise.
- No `/proc/config.gz`: kernel option checks must be functional probes.

## Unraid-specific runtime components

- `cgroup2-unraid` runs as a daemon (`rc.cgroup2unraid`). It watches
  `/sys/fs/cgroup` with inotify (`cgroup.events`) and calls `rmdir`: it
  removes **empty cgroups**. Interaction with LXC-created cgroups
  (`lxc.payload.*`, `incus.*`) is **untested** and a real risk; it must be
  exercised on a disposable Unraid system.
- Docker (29.x), containerd, libvirt 12.x/QEMU 10.x, dnsmasq (for `virbr0`) and
  `libvirtd` run on the host. Docker uses cgroup `docker/`, libvirt `machine/`.
  Incus must use its own cgroup and state tree.
- Network: `br0` (bond uplink), `docker0` plus user-defined Docker bridges,
  `virbr0`, WireGuard and Tailscale interfaces; `ip_forward=1`,
  `bridge-nf-call-iptables=1`; iptables chains `DOCKER*`, `LIBVIRT_*`,
  `WIREGUARD`. The plugin must create **no** bridge, rule or NAT. `nft` is not
  installed, so Incus managed networks (which need nftables ≥ 1.0) are
  unsupported on this host.
- GPUs: Intel iGPU on `i915` (host). The discrete NVIDIA card (both PCI
  functions) is bound to **`vfio-pci`** for a running VM; IOMMU is enabled
  (`intel_iommu=on iommu=pt`, 21 groups). Host NVENC is **not available** for
  that card. All streaming/GPU acceptance gates stay blocked; only CPU-only
  tests are in scope.
- GOW (`gow.plg`), `docker.networks`, `nvidia-driver`, `zfs.master`,
  `user.scripts` and ~40 other plugins are installed; the plugin namespace
  `polyseat-unraid` does not clash with any of them.

## Pluginmanager (verified from `/usr/local/sbin/plugin` source, Unraid 7.3.3)

`/usr/local/sbin/plugin` is a PHP script symlinked from
`dynamix.plugin.manager/scripts/plugin`.

- Commands: `install PLUGIN-FILE [forced]`, `remove PLUGIN`, `check PLUGIN`,
  `checkall`, `update PLUGIN`, `download PLUGIN-FILE [TARGET-VERSION] [forced]`,
  attribute lookup (`plugin version FILE`), plus internal `dump`, `changes`,
  `alert`, `validate`.
- `<PLUGIN>` attributes: `name`, `author`, `version` (mandatory), `pluginURL`,
  `min`, `max`, `launch`, `icon`, `support`.
- `<FILE Name= Mode= Run= Method= Min= Max=>` with `<URL>`, `<LOCAL>`,
  `<INLINE>`, `<SHA256>`/`<MD5>`. `Method` may list `install remove`.
  **An existing target file is not overwritten** unless its SHA-256/MD5 differs
  (then it is deleted and fetched again). Versioned, hash-addressed names are
  therefore mandatory.
- Successful install copies the `.plg` to `/boot/config/plugins` (re-installed
  every boot) and symlinks it in `/var/log/plugins`. `remove` moves it to
  `/boot/config/plugins-removed`.
- Boot persistence: plugins in `/boot/config/plugins/*.plg` are re-installed at
  boot; there is **no** systemd. Services start from event scripts under
  `/usr/local/emhttp/plugins/<name>/event/` (`started`, `disks_mounted`,
  `stopping_svcs`, `stopped`, ...) or `/boot/config/go`.
- Idiom: payload as `.txz` below `/boot/config/plugins/<name>/` installed by
  `installpkg` into RAM; settings in `<name>.cfg`.

## Differences Ubuntu CI ↔ Unraid

| Topic | Ubuntu 24.04 CI (green) | Unraid 7.3.3 (observed) |
|---|---|---|
| libc | glibc 2.39 | glibc **2.44** (newer; incusd needs ≤ 2.38) |
| liblxc | Ubuntu `liblxc` 5.0.x via apt | **absent**; Incus 7.5.1 requires LXC ≥ 6.0.0 → build pinned v6.0.6 |
| cowsql/raft | built from pinned source | absent → ship privately |
| Init/service | `sudo`, no systemd needed in smoke | rc.d/events/`go`, no systemd |
| LSM | AppArmor active | **none** |
| cgroups | v2 (systemd-managed) | v2 + `cgroup2-unraid` empty-cgroup remover |
| `/etc/subuid` | script adds a range | empty by default |
| Python/ShellCheck | present | **no Python**; bash/PHP/jq only |
| Storage | ext4 in `/tmp` | RAM rootfs; ZFS/XFS/btrfs pools; flash may be vfat or ZFS |
| Bridges/firewall | clean runner | `br0`, Docker, libvirt, WireGuard, Tailscale rules in place |
| GPU | none | one vfio-pci (VM-owned) discrete GPU, one host iGPU |
| Package manager | apt | Slackware `installpkg`/`.txz`, no dependency resolution |

## What this inventory does *not* prove

Running an unprivileged container, idmapped mounts, interaction with
`cgroup2-unraid`, Incus daemon behaviour under Unraid's RAM root, boot
ordering relative to array start, and anything about GPU/streaming. Those need
the disposable Unraid-compatible test system (see
[no-GPU test plan](no-gpu-test-plan.md)).
