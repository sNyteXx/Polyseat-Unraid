# Tower run 8 — M2 GPU canary, 2026-10-31: PASS

This run was done under the owner's standing GO, with the same limits as [run 7](tower-run7-gpu.md). The steps were
driven by one fail-closed script: the read-only preconditions came first, and from staging on the rollback always
ran.

Candidate: commit `4a9a43e` (full lane 41221824997, all five jobs green). Its runtime was reused after verification
from run 41219296632:
- runtime `6a4013f7…`; controller package `a70b6d5c…`, file identical to the CI rehearsal;
- seat image `7534faba…`, manifest `2b55f84f…`;
- CI hook rehearsal 13 PASS including `--no-pivot`; the same runtime gave 13 PASS locally;
- token `53ec7e02…` (gpu-smoke).

| Step (UTC) | Result |
|---|---|
| pre 17:01 | `gpu-preflight` PASS (GamingVM and Windows 11 shut off, no lock or marker, no holder, MemAvailable 18.6 GiB); run-7 residue digest `0db9e8a8…` (measured twice, re-checked right before) |
| R0f, S0, W1–W7 17:02–17:04 | residue removed exactly; 11/11 hashes and signatures; stage, activate, seat stage; `gpu-preflight` PASS incl. the hook |
| W8g 17:04:20–17:07:25 | **PASS**: storage, import, start; the nvidia self-test passed on start and on restart; the GPU was released after each stop; then delete and daemon stop |
| after 17:08 | uninstall RC=0; GPU on `nvidia`, no holder, P8, 2 MiB, no process; `/var/lib/polyseat-lxc` gone; `/tmp` kit removed exactly; residue 24.7 MB (canary one-shot state, kept for inspection by design, removed by digest before the next run) |

## In-container self-test (both times)

| Check | Result |
|---|---|
| nvidia-gpu | `GPU 0: NVIDIA GeForce RTX 5070 Ti (UUID: GPU-3cf9157a-…)` |
| system / user manager | `running`; `user@1000.service` active with session bus |
| profile | `nvidia` (sway `WLR_RENDERER=gles2`, `GBM_BACKEND=nvidia-drm`) |
| sway, output | active, `wayland-1`; `HEADLESS-1 1280x720` |
| screenshot | PNG 1280x720, 200 distinct colours (identical hash on start and restart) |
| Sunshine | active; `Found H.264 encoder: h264_nvenc [nvenc]`; capture `wlr` |
| netns-only | `lo` plus the inert `tunl0`; listener tcp :47989 only inside the container's own netns (no NIC) |
| stop-session | Sunshine, sway and their children gone |

Hook: status 0; the CLI log shows `executing /sbin/ldconfig from host` with no confinement error (`--no-pivot` via
the shim).

## Resources and GPU

- **Canary group:** memory peak 1.98 GB of the 3 GiB limit. Limits were 2 CPUs on cores 24–27 and 2048 pids. Lowest host MemAvailable during the run was 16.2 GiB.
- **GPU during the self-tests:** P0/P1, 263–276 MiB in use, of which `/usr/bin/sunshine` (in the container) held 228 MiB; up to 38 W.
- **GPU after each stop:** P8, 2 MiB, no process.
- **No interference:** GamingVM and Windows 11 stayed off; the VM-priority check never fired.
- **Sampler "escaped":** now labelled. It shows only `incus:0::/63371`, the controller's client in the SSH session's elogind cgroup ([run 7](tower-run7-gpu.md)).

## Host diff

Before vs after shows only:
- the expected plugin paths and the flash token/signer files;
- mount lines 455 → 441 and UDP sockets 61 → 60.

The second item is the **before** snapshot catching a transient owner state: the backup tool's snapshot mounts plus one
UDP socket. Every other sample has 441 and 60, both before and after the run: 16:22, 16:23 and 16:35 (run 7), 17:01
(pre), 17:09 (after) and 17:11 (live). No Polyseat, Incus or LXC mount remains.

## What M2 does not claim

- No streaming to a client yet (M3: network decision pending).
- No input devices.
- No persistence or autostart.
- No concurrent use with a VM that passes the GPU through. The VM-priority stop and `gpu-preflight` keep these exclusive.
