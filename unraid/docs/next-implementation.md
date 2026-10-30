# 🚧 WIP — DO NOT USE — Next implementation milestones

The branch now contains a **source-pinned real Incus build workflow** on an
isolated Ubuntu runner. Its first full build, dependency check and checksum
verification [passed in GitHub Actions](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41084969302).
The output is **not** an installable Unraid bundle, and the disabled `.plg`
must remain disabled.

1. Keep the pinned C dependency and Incus build passing in GitHub Actions.
2. Record binary and dynamic library requirements; update the source pins for
   a security-supported Incus release before choosing a deployable baseline.
3. Create a reproducible runtime package with SBOM, signatures/attestations,
   vulnerability audit and safe extraction validation.
4. Implement a test-only, non-systemd supervisor with isolated network and
   explicit UID/GPU ownership checks; never modify `br0` or VM passthrough.
5. Implement staged, operator-confirmed installation and a tested rollback
   state machine per [security-rollback-contract.md](security-rollback-contract.md).
6. Perform one-seat NVENC and two-seat Moonlight acceptance on disposable
   hardware, including boot, upgrades and cleanup without game-data loss.

No automatic deployment, service enablement, or GOW removal.
