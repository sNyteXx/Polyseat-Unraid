# Local-first test matrix for the GPU (M2) hook path

Goal: diagnose and fix the LXC nvidia mount hook in seconds on UbuntuDev instead of a push/CI/Tower round trip per
micro-fix. GitHub CI and an owner GO remain mandatory before every Tower run; this matrix only shortens the loop.

## Levels (measured on UbuntuDev, 2026-10-31)

| Level | Command | Time | Needs | Proves |
|---|---|---|---|---|
| L0 lint | `bash -n …` + ShellCheck 0.9 (`koalaman/shellcheck-alpine:v0.9.0`, `--network none`) | ~2 s | Docker | syntax and lint of controller, wrapper, rehearsal |
| L1 hook unit | `python3 -m unittest unraid.tests.test_lxc_nvidia_hook` | 1.8 s, 11 tests | nothing | wrapper passes arguments unchanged and adds a writable `--debug` file; preserves exit status 0/1/2/126/137 and records it; captures stdout+stderr; caps at 256 KiB **without** SIGPIPE; filters the environment (no secrets); falls back to the plain hook without a usable log dir; upstream hook (pinned fixture `d4e1bcdc…` = `LXC_NVIDIA_HOOK_SHA256`): exit 0 for `void`/empty/unset `NVIDIA_VISIBLE_DEVICES`, exit 1 for wrong hook type, missing CLI, or **not in a user namespace** (CLI never reached) |
| L1 controller | `python3 -m unittest unraid.tests.test_desktop_canary.LxcHookPathTests` | ~5 s | nothing | wrapper + verbatim hook materialized in a 0755 chain with their hashes, replaced when changed, removed exactly by uninstall; the daemon is never pointed into the 0700 slot |
| L2 rehearsal | `POLYSEAT_L2_BUNDLE=<sha>.tar.gz python3 -m unittest unraid.tests.test_lxc_nvidia_hook` (or `unraid/scripts/local-hook-rehearsal.sh BUNDLE OUT CLI_DIR`) | 6.2 s total (3–4 s inside the container; first image build ~1 min, cached) | Docker (privileged, disposable, `--network none`, 2 GiB / 4096 pids) | the **real pinned incusd 7.5.1 + liblxc 6.0.6** start an unprivileged container with `nvidia.runtime=true`; the controller's own `lxc_hook_prepare` builds the hook directory; cases below |

L2 cases (all PASS on UbuntuDev; the same script runs in CI job `desktop-canary`):

| Case | Setup | Result |
|---|---|---|
| A | hook directory 0700 | `Script exited with status 126` → **Tower run 5 reproduced locally** |
| B | stub CLI exits 0 | container starts; recorded context: `uid_map 0 1000000`, own user/mnt/pid/cgroup/net namespaces, `LXC_HOOK_VERSION=1`, `LXC_HOOK_TYPE=mount`, `LXC_LOG_LEVEL=WARN`, `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics`; CLI argv (through the shim) `--no-pivot --debug=/var/lib/polyseat-lxc/hook-log/nvidia-container-cli.log --user configure --no-cgroups --ldconfig=@<ldconfig> --compute --utility --video --graphics /var/lib/polyseat-lxc/rootfs` (+ `--device=<GPU UUID>` with a GPU device) |
| C | stub CLI prints an error, exits 1 | LXC logs only `Script exited with status 1` (**exactly Tower run 6**); the wrapper log holds the CLI's text and status 1, still present after the instance is deleted |
| D | **NVIDIA's real `nvidia-container-cli` 1.20.1** (Tower's version; `.deb` sha256-pinned from nvidia.github.io, extracted, never installed) | reached as container root ("using unprivileged user 0:0"), starts its driver RPC service, fails with `initialization error: load library failed: libnvidia-ml.so.1` (no driver here); the error is captured and its debug log opens at the writable path (1.20.1 `assert`s that it can) |

## What only Tower with the RTX can reproduce

Tower run 7 got through NVML, device listing and every driver mount. It then failed in the CLI's
ldconfig step: `pivot_root` returns EINVAL on Unraid's initramfs root (`/` is rootfs). The CLI now runs with
`--no-pivot` through the shim (see [tower-run7-gpu.md](tower-run7-gpu.md)). Neither UbuntuDev nor a GitHub runner
has a rootfs root, and without a driver the real CLI stops before ldconfig, so that step remains Tower-only.

The step after case D: the real CLI with a **real NVIDIA driver** — loading NVML (`libnvidia-ml`) and the driver RPC
service against `/dev/nvidia*`, resolving the driver's userspace via Unraid's `/etc/ld.so.cache` (Unraid's NVIDIA
plugin layout under `/usr/lib64` and `/usr/lib`), bind-mounting those libraries, binaries and device nodes into the
container rootfs from inside the user namespace, and running `ldconfig` in the rootfs — on the Unraid 6.18 kernel.
UbuntuDev has no NVIDIA GPU or driver, so no local test can claim equivalence for that step. The read-only probe
`setpriv --reuid=1000000 … nvidia-container-cli --user info` on Tower already succeeds (driver 595.104.02, RTX 5070 Ti),
so NVML is reachable for that uid outside a user namespace.

## Diagnostics for the next approved Tower run

`/var/lib/polyseat-lxc/hooks/nvidia` is the Polyseat wrapper; it runs the verbatim hook (`nvidia.upstream`) and
keeps, in `/var/lib/polyseat-lxc/hook-log` (container-root-owned, 0700, each file capped): `env.txt` (LXC_*/NVIDIA_*/
PATH, identity, namespaces), `hook.out` (complete stdout+stderr, ≤ 256 KiB), `status`, and the CLI's own
`nvidia-container-cli.log`. The controller copies them (≤ 1 MiB each) into the run's evidence on success and,
on failure, **before** deleting the instance, together with `lxc.log`/`forklxc.log`. No LXC/daemon log level is
raised (raising it would make the hook point the CLI's debug log at a root-only directory).
