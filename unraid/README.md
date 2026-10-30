# 🚧 WIP — DO NOT USE

This directory contains a **non-installing feasibility scaffold**, not a
working Unraid plugin. `plugin/polyseat-unraid.plg.disabled` is an inert
placeholder. There is intentionally no installable `.plg`.

For a later, separately approved **read-only** Unraid assessment:

```sh
sh unraid/scripts/preflight.sh
sh unraid/scripts/check-host.sh
```

These commands report observations only, and do **not** certify compatibility.
They must not be used as permission to run upstream `polyseat-prepare`.

The separate `build-incus-artifact.sh` refuses to run on Unraid or outside an
isolated Linux GitHub Actions runner. Its artifacts are build proofs only,
**not** redistributable Unraid installation packages.

Read [the security and rollback contract](docs/security-rollback-contract.md)
and [acceptance matrix](docs/acceptance.md) before implementing any mutating
host operation. The current branch makes none.
