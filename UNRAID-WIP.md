# 🚧 WIP — DO NOT USE — Experimental Unraid port

> **Not installable or supported.** This branch is a feasibility project, not an
> Unraid release. Do not run upstream `polyseat-prepare`, initialize Incus,
> modify bridges, change GPU passthrough, or install anything on a production
> Unraid host. Keep the existing GOW installation intact.

## Goal

Evaluate a native Unraid plugin lifecycle for Polyseat without replacing or
modifying Unraid's Docker, libvirt, existing network bridges, host NVIDIA
driver, or game data. No installable `.plg` is shipped.

## Proven on the owner's Unraid server (signed one-time canaries)

Unraid 7.3.3. Every run had its own signed approval token and a verified rollback. Details are in the
[acceptance matrix](unraid/docs/acceptance.md).
- **J2:** a desktop seat (systemd, sway, Sunshine) in an unprivileged Incus 7.5.1 container on the Unraid kernel.
- **J3:** the RTX by device access, with NVENC. This works only while the passthrough VMs are off; a guard and a
  per-second watchdog give them priority.
- **J4:** Steam with 32-bit GL and Vulkan, the LAN via br0 for the session only, and persistent storage on the
  cache pool.
- **J5:** the first Moonlight stream (PIN pairing, NVENC at 59 fps over the LAN only) and a Steam sign-in by QR code.

Not proven:
- input (disabled on purpose);
- games;
- two seats;
- the upstream daemon on Unraid;
- persistent Incus state;
- upgrades;
- an installable `.plg`.

The order for the rest is in [the upstream integration plan](unraid/docs/upstream-integration-plan.md).

## What this branch can prove

- Read-only, non-certifying host capability reports in `unraid/scripts/`.
- Syntax/ShellCheck checks and a CI guard that rejects an installable `.plg`.
- An **Ubuntu-only, source-pinned Incus userspace build proof**. The requested
  **7.5.1** build and no-GPU binary smoke
  [passed GitHub Actions](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41091221184).
  A **real no-GPU BusyBox container** now also boots, executes, stops, restarts
  and is deleted on a separate disposable Ubuntu runner
  ([successful CI](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41096323759)).
  These binaries are not a complete Unraid runtime bundle; successful Ubuntu
  boot says nothing about Unraid ABI, kernel features, confinement, GPU mapping
  or real-host rollback.
- **Offline-only Python safety rehearsals** for GPU ownership while a gaming VM
  runs, VM and Docker preservation, isolated network proposals, lifecycle
  transitions, and hostile archive rejection. They have zero host execution
  capability and do not constitute hardware acceptance.
- A **real filesystem staging/rollback rehearsal** restricted to a unique,
  private `/tmp/polyseat-transaction-*` path on a disposable Linux environment:
  checked bundle content, atomic activation, fsynced journal, simulated crashes
  and health-check rollback. **It refuses to run on Unraid**, never controls a
  process or touches external data, and does not certify production rollback.
- The Polyseat **0.44.0 upstream** updates are included on this WIP branch; 0.46.0 is a separate draft
  ([#2](https://github.com/sNyteXx/Polyseat-Unraid/pull/2)). The normal fork `main` remains untouched.
- Build-source inventory (CycloneDX) and dependency metadata are prepared for
  CI; they are **not a complete target-runtime SBOM or a vulnerability clearance**.
- An explicit security/rollback design in
  [unraid/docs/security-rollback-contract.md](unraid/docs/security-rollback-contract.md).

## Acceptance gates (none waived by green CI)

1. Read-only preflight on the **actual Unraid release** and hardware.
2. Build an audited, versioned Incus runtime bundle, with signed provenance,
   runtime dependencies, SBOM, and compatibility verification.
3. Demonstrate isolated start/stop without systemd and without touching Docker,
   libvirt, `br0`, or an existing Incus installation.
4. Validate host GPU ownership and NVIDIA NVENC for one headless seat (no Steam).
   A GPU exclusively passed through to a Windows VM cannot be assumed available
   to host containers at the same time.
5. Validate Sunshine/Moonlight, input isolation, two seats, resource limits.
6. Test filesystem-specific shared-library semantics; ZFS is not automatically
   equivalent to Btrfs/XFS reflinks.
7. Verify an operator-approved staging, upgrade, rollback, and uninstall cycle
   that retains seats, Steam libraries and GOW.

**Active gaming VM safety:** A GPU that a running VM holds through `vfio-pci` is never shared. A seat uses
the GPU by device access only while it is bound to the host driver and every VM that passes it through is off.
A start is refused otherwise, and the watchdog stops the seat as soon as a VM needs the GPU. See
[gaming-VM protection](unraid/docs/gaming-vm-protection.md) and [the GPU path](unraid/docs/gpu-path.md).

The safe no-GPU progression is documented in
[the first no-GPU test plan](unraid/docs/no-gpu-test-plan.md): version-only
binary smoke and the actual CPU-only BusyBox container lifecycle passed on a
disposable GitHub-hosted Ubuntu runner. The real-host canaries above followed.

See [the acceptance matrix](unraid/docs/acceptance.md). There is no automatic
installation, GOW migration, host service startup, or merge to `main` in this
proof-of-concept.
