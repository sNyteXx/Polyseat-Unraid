# 🚧 WIP — DO NOT USE — First test without a GPU

The active Windows gaming VM is protected. **No test described here may touch
the physical Unraid host, its devices, Docker, libvirt, GOW or `br0`.** A GPU
is not necessary to test basic Incus source builds, metadata, process lifecycle
or a container boot on a **separate disposable Linux environment**.

## Test levels (do not confuse them)

| Level | Environment | What would count as success | Status |
|---|---|---|---|
| 0 — code | GitHub Actions Ubuntu 24.04 | Incus 7.5.1 and C sources compile; checksums, limited SBOM; `incus --version` and `incusd --version` without a daemon | **Passed** ([CI](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41091221184)) |
| 0 — models | GitHub Actions Ubuntu 24.04 | 18 pure tests: active VM/vfio, network policy, start/stop/rollback and malicious archives | **Passed** ([CI](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41091221252)); not hardware evidence |
| 1 — container | Disposable GitHub-hosted Ubuntu Linux VM, **not** Unraid | Independent `incusd` process starts with dedicated socket and state directory, no host bridge; locally generated BusyBox container boots, executes, stops, restarts and cleans up | CI workflow **implemented, pending first successful run** |
| 2 — Unraid parity | Dedicated disposable Unraid 7.2-compatible host/VM | Same test with non-systemd lifecycle, Unraid libraries, kernel, cgroup v2, uidmaps and selected pool; clean rollback | **NOT implemented/tested** |
| 3 — streaming | Separate hardware window | Host-owned GPU and Sunshine/Moonlight/NVENC one-/two-seat tests | Blocked while GPU is exclusively used by gaming VM |

## Implemented first level-1 attempt

The existing `.github/workflows/incus-feasibility.yml` now calls
`unraid/scripts/incus-cpu-smoke.sh` in a **disposable GitHub-hosted Ubuntu
runner**. The smoke script runs only in CI as root, with `INCUS_DIR`,
`INCUS_CONF` and `INCUS_EXEC_PATH` redirected to a newly created temporary
staging directory. Its explicit preseed config defines a directory storage
pool and a disk-only profile. **It does not call `incus admin init --minimal`,
which would create a bridge.**

The test uses the upstream, source-pinned BusyBox image generator and a local
`busybox-static` executable, without a third-party image registry. It then
starts, executes in, stops, restarts, and deletes a single CPU-only instance.
The script checks for an unexpected `incusbr0` and stops with an error if
the daemon or container cannot work in a GitHub-hosted runner.

## Remaining before level 2 / production use

1. Complete and verify an **isolated relocatable runtime dependency closure**
   for the different Unraid system libraries (Ubuntu test packages are not it).
2. Verify UID/idmap, socket ownership, storage and kernel parity on a separate
   disposable Unraid-compatible host; never reuse an existing Incus DB.
3. Audit and attest the pinned BusyBox image inputs, and prove network isolation
   beyond the absence of `incusbr0` before any client-reachable networking.
4. Introduce a Unraid-specific non-systemd supervisor and a real implementation of
   stage/activate/start/stop/rollback with a strict owned-path manifest,
   crash injection and cleanup verification. Existing Python code is pure
   simulation only.
5. Keep host-privileged CI jobs exclusively on disposable GitHub-hosted
   runners. Never run them via a deployment hook or a self-hosted runner.

## Pass/fail evidence to capture

- Exact Incus/Go/raft/cowsql pins and operating system fingerprint.
- No unresolved dynamic dependencies; no privileged TCP admin socket.
- CPU-only container boot, health, stop and repeat test with bounded timeout.
- Clean rollback after intentional failed activation; no remaining owned
  processes, sockets, mounts or unexpected changes to host bridge/routes.
- A **failed** runtime test must report a blocker, not attempt host repair.

When these tests pass on a disposable VM, level 2 may be planned. It still
does **not** authorize testing the production Unraid host while the gaming VM
is needed. Any future real-host test requires explicit separate approval.
