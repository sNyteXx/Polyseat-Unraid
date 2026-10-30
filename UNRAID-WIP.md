# 🚧 WIP — DO NOT USE — Experimental Unraid port

> **Not installable or supported.** This branch is a feasibility project, not an
> Unraid release. Do not run upstream `polyseat-prepare`, initialize Incus,
> modify bridges, change GPU passthrough, or install anything on a production
> Unraid host. Keep the existing GOW installation intact.

## Goal

Evaluate a native Unraid plugin lifecycle for Polyseat without replacing or
modifying Unraid's Docker, libvirt, existing network bridges, host NVIDIA
driver, or game data. No installable `.plg` is shipped.

## What this branch can prove

- Read-only, non-certifying host capability reports in `unraid/scripts/`.
- Syntax/ShellCheck checks and a CI guard that rejects an installable `.plg`.
- An **Ubuntu-only, source-pinned Incus userspace build proof**, successfully
  [built with Incus 7.0.2 LTS in CI](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41088560407).
  Its binaries are not a complete runtime bundle, and success says nothing
  about Unraid ABI, kernel features, confinement, GPU mapping, boot, or rollback.
- **Offline-only Python safety rehearsals** for GPU ownership while a gaming VM
  runs, VM and Docker preservation, isolated network proposals, lifecycle
  transitions, and hostile archive rejection. They have zero host execution
  capability and do not constitute hardware acceptance.
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

**Active gaming VM safety:** All real host, VM, GPU and Unraid operations are
deferred. An exclusive `vfio-pci` GPU cannot be shared with host NVENC by
pretending it is available. We must either prove the presence of a separate
host-owned GPU or agree another architecture in a separate review. See
[gaming-VM protection](unraid/docs/gaming-vm-protection.md).

See [the acceptance matrix](unraid/docs/acceptance.md). There is no automatic
installation, GOW migration, host service startup, or merge to `main` in this
proof-of-concept.
