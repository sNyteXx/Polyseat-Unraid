# Tower run 5 — 2026-10-31: **M1 desktop canary PASS**; M2 GPU canary failed closed (hook path)

Owner GO for exactly: R0b (run-4 residue, listing digest `2de35a82…`), then M1, then M2 if M1 passes. Commit
`8a8255062bc42ef924bc054983a2dcb621077b7c` (CI 41206723584, 11/11), runtime `475df02f…`, controller `3d76c782…`
(file `cd5fbf0f…` = CI rehearsal), seat image `bc6ceb55…` (manifest `146911b0…`).

## R0b and M1 (14:03–14:08 UTC)
- R0b: digest re-computed on Tower = `2de35a82…` (36 935 files, 4 031 dirs, 2 478 symlinks, 8 sockets) → removed exactly.
- Baseline: no Polyseat path besides the flash dir; MemAvailable 18.0 GiB; only UbuntuServer running; GPU on `nvidia`.
- W1–W7: token `5e888849…`; 8/8 hashes, 3/3 signatures; stage, activate, seat image on the RAM root.
- **W8 desktop-smoke: PASS** (`DESKTOP_RC=0`): daemon healthy 14:07:45, import 15 s, container start 14:08:00 (own net
  namespace `net:[4026536905]`, unprivileged), self-test start **and** restart 11/11 PASS — systemd `running`, user
  manager, sway `HEADLESS-1 1280x720`, grim screenshot (sha `3f43e506…`, identical to CI), Sunshine `capture = wlr`,
  `Found H.264 encoder: libx264 [software]`, Sunshine listening on :47989 inside the container, interfaces
  `lo=yes inert=tunl0 unexpected=`, session stopped cleanly; stop, restart, delete, daemon stop.
- Sampler: canary group memory peak 2.01 GB (limit 3 GiB), limits exactly `3 GiB / swap 0 / 200000:100000 / 24-27 /
  2048`; host MemAvailable ≥ 14.7 GiB. Only the controller's own `incus` CLI ran outside the bounded tree.
- Uninstall RC=0; the residue was small (83 entries) and was removed exactly by digest `401f58ec…` for M2.

## M2 (14:09–14:13 UTC)
- Token `1e56875a…` (gpu-smoke); fresh `gpu-preflight` PASS (GamingVM and Windows 11 off, no holder, toolkit present).
- Stage/activate/seat stage OK; daemon healthy; import; init with the GPU device; **start failed**:
  `lxc.log`: `run_buffer: Script exited with status 126` → `Failed to run mount hooks`.
- **Cause (verified read-only):** the LXC `nvidia` mount hook runs as the container's root inside its user namespace
  (host uid 1000000); `INCUS_LXC_HOOK` pointed into the slot below `/usr/local/polyseat-unraid` (0700) and `…/slots`
  (0700), which that uid cannot traverse. CI could not see it: without an NVIDIA GPU the path never reaches the hook.
- The new failure path deleted the instance; uninstall RC=0; GPU afterwards: driver `nvidia`, no holder, P8, 2 MiB,
  0 encoder sessions, persistence unchanged. Host diff vs the baseline: only the expected plugin paths and `/tmp` kits
  (removed exactly afterwards, owner-approved). Residue: 24.7 MB Incus state + lock/logs in RAM.
- Read-only probe afterwards: `nvidia-container-cli --user info` works as uid 1000000 (driver 595.104.02, RTX 5070 Ti),
  `getopt`, `ldconfig`, `bash` resolve.

## Fix (next candidate)
The hook is copied from the verified slot into `/var/lib/polyseat-lxc/hooks/nvidia` (0755 chain on the exec-capable RAM
root; `/run` is noexec), re-hashed against the signed manifest on every start, and checked with `setpriv` as the
container root uid before any GPU start (fail-closed for gpu-smoke); uninstall removes it exactly. The CI desktop
rehearsal now asserts that reachability.
