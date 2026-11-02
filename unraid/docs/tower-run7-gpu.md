# Tower run 7 — M2 GPU canary, 2026-10-31: hook diagnostics caught the cause (pivot_root on Unraid's initramfs)

Owner standing GO (2026-10-31): run Tower tests without a per-run question until M2 runs cleanly. These limits are
unchanged:
- CI-qualified signed kit, `gpu-preflight`, fail closed, full verified rollback;
- no reboot, no driver/VFIO/boot change, no GamingVM/Docker/network change, no persistent install.

Candidate: commit `f31a35d` (CI 41212926028, all green):
- runtime `1da16620…`; controller package `39749c25…` (file `97c7cdcd…`, identical to the CI rehearsal);
- seat image `791ce994…`, manifest `426b2898…`; token `35eef7cd…` (gpu-smoke).

| Step (UTC) | Result |
|---|---|
| pre | run-6 residue digest `55c92a90…` measured twice (equal); `gpu-preflight` PASS: GamingVM and Windows 11 shut off; transition lock free, no unhealthy marker; no GPU holder; MemAvailable 20.3 GiB; only UbuntuServer running |
| R0e 16:23 | residue removed exactly by digest; S0 baseline MemAvailable 19.9 GiB |
| W1–W7 16:24 | 11/11 hashes and signatures; stage, activate, seat stage; `gpu-preflight` PASS incl. "active bundle ships the LXC nvidia hook" |
| W8g 16:26:4x–16:27:06 | daemon healthy, import, init with `gpu` device `pci=0000:01:00.0 mode=0666`; **start failed** in the mount hook (status 1). Group memory peak 1.99 GB of 3 GiB; failure path kept the diagnostics, then deleted the instance |
| after | uninstall RC=0; GPU on `nvidia`, no holder, P8, 2 MiB, 0 encoder sessions; `/var/lib/polyseat-lxc` gone; host diff only expected plugin paths (and the owner's restic job in the process list); `/tmp` kit removed exactly; residue 24.7 MB |

## Cause (from the wrapper's evidence)

`hook.out` plus `nvidia-container-cli.log` (wrapper from run 6, first approved use):

- NVML and the driver RPC work as the container root inside its user namespace; it lists `/dev/nvidia0` (`GPU-3cf9157a…` at 01:00.0).
- It mounts every driver library and binary (from `/usr/lib64`, `/usr/bin`), the GSP firmware, `/dev/nvidiactl`, `/dev/nvidia-uvm*`, `/dev/nvidia0` and `/proc/driver/nvidia/gpus/0000:01:00.0` into the rootfs.
- Then: `nvc_ldcache.c:545] could not start /sbin/ldconfig: process confinement failed: invalid argument`
  → `ldcache error: process /sbin/ldconfig failed with error code: 1` → hook exit 1.

libnvidia-container 1.20.1 `change_rootfs()` is the only source of "process confinement failed". For a raw
syscall it reports errno, and the call that fails there is `pivot_root(".", ".")`. Tower's `/` is the kernel's
**rootfs** (initramfs, mount id 1, checked read-only). pivot_root(2) returns EINVAL when the current root is on
rootfs. Docker on Unraid avoids this with `DOCKER_RAMDISK`, and LXC with its own move+chroot fallback, which is why
M1 worked. The CLI has the global option `--no-pivot` for exactly this (MS_MOVE + chroot). The LXC hook cannot pass
it.

This is not reproducible on UbuntuDev or a GitHub runner: their root is a normal mount, and without a driver the
real CLI stops before ldconfig (L2 case D).

## Fix (next candidate)

- New shipped file `lxc-nvidia-cli`, installed by the controller as `hooks/cli/nvidia-container-cli`:
  - root:root 0755, hash-checked, checked as uid 1000000, removed exactly by uninstall;
  - it execs the real CLI as `--no-pivot "$@"` and refuses unless `POLYSEAT_NVC_REAL` is an absolute path other than itself.
- The wrapper resolves the real CLI the way the upstream hook does, exports `POLYSEAT_NVC_REAL` and puts `hooks/cli` first on the hook's PATH. The upstream hook stays verbatim (pinned `d4e1bcdc…`).
- The ldconfig child stays confined: its own mount, PID and IPC namespaces, empty capability bounding set, rlimits, seccomp.
- Tests:
  - L1: shim argv/status, fallbacks, refusals.
  - Controller: install, replace and removal.
  - L2 case B: `--no-pivot` first in the argv of the CLI as started by real incusd/LXC.
  - L2 case D: the real CLI 1.20.1 accepts the option and continues to its NVML step.

## Finding: "escaped" process in the sampler (runs 4–7) is the controller's own incus client

The sampler flags processes that run a bundled binary outside the canary cgroups. Every run since run 4 showed one
such process for a few seconds, in a numeric top-level cgroup (`/63216` … `/63335`). On Tower, elogind places each
SSH session in `/sys/fs/cgroup/<session id>`: a read-only check showed `0::/63342` = `XDG_SESSION_ID=63342`. So it
is the allowlisted, timeout-bounded `incus` CLI client that the controller runs from the operator's session, not
the daemon or a container. The sampler now also prints the binary name.
