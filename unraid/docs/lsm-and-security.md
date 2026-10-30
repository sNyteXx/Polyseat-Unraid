# 🚧 WIP — DO NOT USE — LSM (AppArmor/SELinux) behaviour and security assessment

## What failed in CI and why

Run `41111483448` (head `a3466c4`) built everything, passed the ABI/bundle
checks, installed the bundle through `polyseatctl`, started a healthy `incusd`
and failed at `incus start` of the first BusyBox container:

    Failed to set LXC config: lxc.apparmor.profile=incus-polyseat-ci-cpu_...

Cause (verified in `internal/server/sys/apparmor.go` of the pinned Incus
commit): Incus enables AppArmor only if **all** of these hold: it was not
disabled by `INCUS_SECURITY_APPARMOR=false`, `/sys/kernel/security/apparmor`
is a directory, and `apparmor_parser` is in `PATH`. The Ubuntu runner satisfies
all three, so Incus asks liblxc for `lxc.apparmor.*` keys. The bundled liblxc is
**deliberately built without AppArmor** (`-Dapparmor=false`) because Unraid ships
no `libapparmor`; it rejects those keys.

## Unraid versus the Ubuntu runner

| | Ubuntu CI runner | Unraid 7.3.3 (read-only inventory) |
|---|---|---|
| `/sys/kernel/security` | present, AppArmor active | **absent** |
| `apparmor_parser` | installed | not installed |
| SELinux | off | off (no `/sys/fs/selinux`) |
| What Incus does by itself | enables AppArmor | **disables AppArmor** ("lack of kernel support") |
| Result with the bundled liblxc | start fails | no `lxc.apparmor.*` key is ever set |

So on the inspected Unraid the failure cannot occur, and nothing is lost
relative to what the kernel offers: there is no LSM to confine containers.

## What the controller does now (no blanket disabling)

1. The bundle manifest declares `built_without` (default `apparmor`,
   `selinux`); the manifest is signed, so the claim cannot be altered.
2. Before starting the daemon the controller probes the host. If a declared-
   unsupported LSM is **active** (AppArmor: `/sys/kernel/security/apparmor`;
   SELinux: `/sys/fs/selinux/enforce`) it **refuses to start** with an
   explanation. Running containers without the confinement an operator
   expects from the host would be a silent security downgrade.
3. Only `ACCEPT_NO_LSM="yes"` (exact string) in the settings overrides that. The
   acceptance is logged as a warning at every start.
4. The daemon always gets `INCUS_SECURITY_APPARMOR=false`: the bundled liblxc
   cannot use AppArmor, so Incus must not ask for it. SELinux is never
   switched on (`INCUS_SECURITY_SELINUX` is not set).
5. CI: the end-to-end first asserts that the controller **refuses** on the
   AppArmor-active runner, then sets `ACCEPT_NO_LSM="yes"`, i.e. it emulates the
   target's LSM-less state explicitly and visibly. The legacy smoke script sets
   `INCUS_SECURITY_APPARMOR=false` for the same reason; it is CI-only.

## Security assessment

Lost compared with an AppArmor-enabled Incus on Ubuntu: the per-container
AppArmor profile (mount restrictions, `/proc`/`/sys` write limits, ptrace and
signal rules, `mac_admin` isolation). That is **defence in depth**, not the
primary boundary.

Remaining controls on Unraid (all verified present or configured by Incus/LXC):

- user namespaces with a subordinate ID range (unprivileged containers; needs the
  managed `/etc/subuid` block) — the main boundary;
- seccomp (kernel support, `libseccomp` 2.6.1; liblxc built with seccomp);
- capability bounding set, cgroup v2 resource and device controls, masked
  `/proc`/`/sys` paths;
- no management socket on the network (health check fails on any
  `core.https_address`), no bridge or firewall changes, root-only socket.

Residual risk: a kernel or runtime escape from an unprivileged container is not
additionally blunted by an LSM. Do not run untrusted workloads; containers hold
game clients under the operator's control. Privileged containers must stay
disabled (no subuid block means Incus would only allow privileged ones: the
controller therefore always adds the block before starting).

## Still unproven

Behaviour of the real Unraid kernel with Incus' seccomp policy and idmapped
mounts, and interplay with `cgroup2-unraid`, can only be shown on a
disposable Unraid test system (see the runbook).
