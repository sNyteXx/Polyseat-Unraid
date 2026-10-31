# Tower run 6 — M2 GPU canary, 2026-10-31: failed closed in the LXC nvidia hook (exit 1)

Owner GO for exactly: R0d (run-5 residue, digest `ac4a4cb5…`), then M2 with commit
`e0cbf65aaeb71b85882f6dfb175685158d514346` (CI 41209744754, 11/11), runtime `21f3262b…`, controller `65657f8b…`
(file `3a1c3030…` = CI rehearsal), seat image `9f49730c…` (manifest `7a1925d8…`), token `10085b02…` (gpu-smoke).

| Step (UTC) | Result |
|---|---|
| R0d 14:37 | digest re-computed = `ac4a4cb5…` (78 entries) → removed exactly |
| S0 | baseline clean; MemAvailable 18.2 GiB; only UbuntuServer running; GPU on `nvidia` |
| W1–W7 14:38 | 11/11 hashes+signatures; stage, activate, seat stage; `gpu-preflight` PASS incl. "active bundle ships the LXC nvidia hook" |
| W8g 14:39:26–14:39:43 | daemon healthy; import; init + `gpu` device `pci=0000:01:00.0 mode=0666`; **start failed**: `lxc.log` `run_buffer: Script exited with status 1` → `Failed to run mount hooks`. The run-5 cause (exit 126, unreachable hook) is fixed: the hook now **ran**. At LXC's default WARN level nothing else is recorded. |
| after | failure path deleted the instance; uninstall RC=0; GPU: driver `nvidia`, no holder, P8, 2 MiB, 0 encoder sessions, persistence unchanged; host diff vs baseline: only expected plugin paths; mount-table change = owner's backup snapshot mounts (hash back to the run-4 baseline); `/tmp` kits removed exactly (owner-approved); residue 24.7 MB |

**Why there is no cause yet:** LXC records a mount hook's output only at log level DEBUG, and the hook's own automatic
CLI debug log (enabled at DEBUG) would point at the instance directory, which the container root cannot write;
libnvidia-container 1.20.1 asserts on an unopenable debug log. Raising the level would therefore have changed the
failure. See [local-test-matrix.md](local-test-matrix.md) for the local reproduction (runs 5 and 6 failure shapes
reproduced in seconds) and the diagnostic wrapper that makes the next approved run report the CLI's own error.
