# 🚧 WIP — DO NOT USE

This directory contains a **non-installing feasibility scaffold**, not a
working Unraid plugin. `plugin/polyseat-unraid.plg.disabled` is an inert
placeholder. There is intentionally no installable `.plg`.

For a later, separately approved **read-only** Unraid assessment:

```sh
sh unraid/scripts/preflight.sh
```

These commands report observations only, and do **not** certify compatibility.
They must not be used as permission to run upstream `polyseat-prepare`.

The separate `build-incus-artifact.sh` refuses to run on Unraid or outside an
isolated Linux GitHub Actions runner. Its artifacts are build proofs only,
**not** redistributable Unraid installation packages.

## Offline simulation (CI and developer workstations)

```sh
python3 -m unittest discover -s unraid/tests -p 'test_*.py' -v
```

These tests include pure GPU/VM/network models **and** a real filesystem
transaction rehearsal strictly inside a new private `/tmp/polyseat-transaction-*`
directory. No existing host paths, services, VMs, bridges or GPUs are touched.
An active gaming VM with an exclusively passed-through GPU is explicitly
blocked for that GPU. The archive verifier itself is read-only; the separate
transaction lab writes only checked files below its disposable root.

For a real container boot **without any GPU** on disposable infrastructure,
see [the no-GPU test plan](docs/no-gpu-test-plan.md). The Incus 7.5.1 Ubuntu
runner test has now passed a real container start/exec/stop/restart; this is
not an Unraid runtime or production installation.

The controller, plugin template and design are described in
[plugin lifecycle](docs/plugin-lifecycle.md); host facts and the Ubuntu-CI
differences are in the [Unraid 7.3.3 inventory](docs/host-inventory-unraid-7.3.3.md);
the local-dev build is described in [dev-build](docs/dev-build.md), the production CPU-only canary **plan (No-Go today, no approval)** in [production-canary-plan](docs/production-canary-plan.md), supply-chain data in [security-analysis](docs/security-analysis.md) and the key policy in [signing-key-policy](docs/signing-key-policy.md); the pinned desktop seat image and its in-image self-test are in [seat-image](docs/seat-image.md); the CPU-only test build and its safe plan are in [cpu-only-test-plan](docs/cpu-only-test-plan.md); the procedure for a **separate** test system is the
[test-system runbook](docs/test-system-runbook.md). `polyseatctl` refuses all
host mutation while its release gate is closed (the committed state).

Read [the security and rollback contract](docs/security-rollback-contract.md)
and [acceptance matrix](docs/acceptance.md) before implementing any mutating
host operation. The committed controller makes none on a real host.
