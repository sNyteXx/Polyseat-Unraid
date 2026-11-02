# 🚧 WIP — DO NOT USE — Security and rollback contract

**No Unraid installation, service control, migration, upgrade or rollback is
enabled in this branch.** `plugin/src/emhttp/scripts/polyseatctl` implements
this contract in bash but refuses every host mutation while its committed
release gate is `closed` ([lifecycle](plugin-lifecycle.md)). It has only been
exercised in `/tmp/polyseat-ctl-*` sandboxes with stub daemons. The contract is
the minimum review bar, not evidence that the implementation is safe on Unraid.

`unraid/lab/` contains **pure Python policy models** and a strictly confined
**disposable filesystem transaction rehearsal**, not host executors:
`guards.py` rejects unsafe GPU/network proposals, `lifecycle.py` checks state
transitions and approvals, and `archive.py` checks hostile tar entries **without
extracting anything**. `transaction.py` verifies an archive and writes only
inside a pre-created private `/tmp/polyseat-transaction-*` lab directory,
fsyncs a journal, atomically switches a content-addressed bundle pointer,
and restores the prior pointer after failed health checks or simulated crashes.
It rejects execution on Unraid and cannot start a service, touch VM/GPU/Docker
assets, or uninstall anything. These tests **do not validate real Unraid
filesystems, process rollback or host compatibility**.

## Assets and ownership

- The plugin may own only explicitly recorded paths in its own namespace.
  Persistent state belongs on a user-selected pool; the Unraid boot device
  may contain only minimal declarative plugin settings.
- Seats, game libraries, Steam prefixes, Docker appdata, VM disks, existing
  network bridges and GOW are **not plugin-owned** and must be preserved.
- Never use broad recursive deletion, globs or guessed directories for
  cleanup. Only a manifest of validated, exact owned paths can be touched.
- Sensitive tokens and pairing credentials must not enter CI artifacts,
  diagnostics or world-readable files.
- The Incus administrative socket stays local with root-only access until
  separately reviewed; no Docker socket or host network namespace exposure.

## State machine (implemented in `polyseatctl`; unproven on Unraid)

| State | Allowed operator action | Required evidence |
|---|---|---|
| ABSENT | Read-only capability check | Host/driver/VM/network inventory |
| STAGED | Verify a separately approved signed bundle | Signer, digest, SBOM, dependency and path checks |
| PREPARED | Review proposed exact-path changes | Dry-run diff, baseline backup and explicit consent |
| ACTIVE | Start isolated service only after approval | Health, GPU ownership, socket permissions and network isolation |
| ROLLBACK_PENDING | Stop only plugin-owned processes, restore backup | Quiescence, file checksums, preserved seats and GOW |

Each transition must be idempotent and crash-recoverable. Unknown, partially
written or mismatched state **blocks** further changes. A failed health check
must not guess how to repair host networking, drivers or storage.

## Transaction and rollback rules

1. Before any mutation, record the Unraid release, kernel, driver, bridge,
   Docker/libvirt status, selected pool and exact current plugin-owned paths.
2. Verify signed content, path traversal/symlink safety, permissions, available
   space and the rollback archive before starting.
3. Write new binaries/configuration into a private staging path, verify them,
   then switch an atomic reference; do not overwrite live files in place.
4. Journal every intended change to an owned manifest, fsync it before
   activation, and keep a known-good prior bundle.
5. If activation or boot health fails, stop only plugin-owned services and
   restore the previous bundle/config. Never touch Docker, libvirt, `br0`,
   VM GPU assignments or GOW as a side effect.
6. Uninstall retains all seats and game data by default. Deleting any of them
   must be a separate targeted action with its own explicit confirmation.
7. Test abrupt power loss, repeated install, failed upgrade, failed rollback,
   insufficient disk space, tampered manifest and idempotent uninstall on a
   disposable Unraid-compatible host before enabling a `.plg`.

An exclusive PCIe-passthrough GPU and host Incus NVENC cannot be presumed to
coexist. If a test detects `vfio-pci` ownership or uncertain IOMMU grouping,
it must stop with an explanation, not attempt to detach the Windows VM.
The active gaming VM is a protected availability dependency, not an asset to
be paused for testing. See [gaming-VM protection](gaming-vm-protection.md).

## CI guardrails

`unraid/tests/safety-contract.sh` requires the installable `.plg` to stay
absent and the disabled placeholder to contain no install/remove actions.
The CI Incus build script rejects non-CI and non-Linux hosts, including an
Unraid host. Python simulation tests exercise active-VM exclusion, no `br0`
or Docker/libvirt changes, malformed lifecycle states and malicious archives.
The separate disposable filesystem tests exercise exact-path archive extraction,
symlink/tamper rejection, journal recovery, health-check rollback and external
data preservation. They never promote an artifact into a real host service.
These guards protect the *current WIP*, but cannot replace functional
runtime/rollback acceptance tests.
