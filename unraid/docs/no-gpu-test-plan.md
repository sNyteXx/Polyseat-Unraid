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
| 1 — container | Disposable Linux test VM, **not** Unraid | Independent `incusd` process starts with dedicated socket and state directory, no host bridge; one CPU-only Linux container boots and stops | **NOT implemented/tested** |
| 2 — Unraid parity | Dedicated disposable Unraid 7.2-compatible host/VM | Same test with non-systemd lifecycle, Unraid libraries, kernel, cgroup v2, uidmaps and selected pool; clean rollback | **NOT implemented/tested** |
| 3 — streaming | Separate hardware window | Host-owned GPU and Sunshine/Moonlight/NVENC one-/two-seat tests | Blocked while GPU is exclusively used by gaming VM |

## Minimum implementation before level 1

1. Complete and verify an **isolated runtime dependency closure** (including
   `incusd`, runtime libraries and helper binaries) for the disposable test OS.
2. Use a separate socket, state directory, ID maps and storage path **inside
   the disposable VM**. Never reuse an existing Incus daemon or DB.
3. Supply a pinned, checksum-verified rootfs from an approved source; do not
   require a LAN-facing management socket. Keep guest networking disabled for
   the first boot and validate that no host bridge or firewall is changed.
4. Introduce a non-systemd supervisor and a real implementation of
   stage/activate/start/stop/rollback with a strict owned-path manifest,
   crash injection and cleanup verification. Existing Python code is pure
   simulation only.
5. Gate the job with an explicit separate test workflow and a disposable
   runner. Never run it via a generic deployment hook or a self-hosted runner.

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
