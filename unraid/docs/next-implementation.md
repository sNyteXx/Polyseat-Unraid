# 🚧 WIP — DO NOT USE

## Next implementation milestone
The workflow is intentionally metadata-only, **not an Incus binary build**. It must not be mistaken for a validated runtime. Before implementing a real binary build, select a verified Incus source commit, toolchain, dependencies and source integrity checks; compile in isolated CI; test in a disposable VM; verify boot, security and networking behavior; and then evaluate on a disposable Unraid test environment. No automatic deployment to Tower.

## Exit criteria
A signed or attestable build manifest, verified binary checksum, SBOM, reproducible build procedure, isolated Incus startup, GPU/NVENC test, isolated network test, and tested rollback. Until then, installer remains disabled.
