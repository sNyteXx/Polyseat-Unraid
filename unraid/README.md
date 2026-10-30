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

Read [the security and rollback contract](docs/security-rollback-contract.md)
and [acceptance matrix](docs/acceptance.md) before implementing any mutating
host operation. The current branch makes none.
