# 🚧 WIP — DO NOT USE — Incus build and verification

## Scope of the current workflow

`.github/workflows/incus-feasibility.yml` runs only on a disposable Ubuntu
GitHub Actions runner. `unraid/scripts/build-incus-artifact.sh` actively
refuses to run outside Linux CI or on an Unraid host.

The workflow now uses a signed Incus **v7.0.2 LTS** tag's immutable target
commit (as checked against the upstream GitHub tag API) and release-tagged C
dependencies. The runner uses Go 1.26.7:

| Source | Revision |
|---|---|
| `lxc/incus` (`v7.0.2` LTS) | `01f81e5e607bbb5818410318e4b3496ad1820824` |
| `cowsql/raft` (`v0.22.1`) | `9edb176a7924ce2b943cb56552366dc4b5a1a6b7` |
| `cowsql/cowsql` (`v1.15.9`) | `783815b901470e27b7dfbcce3a67c888dad19e78` |

The workflow builds the C libraries in a temporary prefix and then compiles
Incus with the matching headers and libraries. It records the three exact
source revisions, Go/toolchain information, Go module inventory, dynamic
dependency listing, a limited CycloneDX **build-source** SBOM and SHA-256
checksums. The runner checks that the inventory is parseable JSON.

The [isolated LTS build run](https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41088560407)
passed compilation, runner dependency inspection, checksums and artifact
upload. That run predates the new build-source SBOM step, which requires its
own successful Actions run. **Neither run authorizes Unraid execution.**

**Limitations:** Ubuntu package dependencies and GitHub's runner image are
not pinned, so this is source-pinned but not bit-for-bit reproducible. The output
does **not** include all required runtime libraries, a tested Unraid loader
path, complete target-runtime SBOM, signature, vulnerability review, or
provenance attestation. **Newer LTS sources do not by themselves make a
redistributable package safe.**
Checksums establish integrity of the CI output, not its trustworthiness.

## Remaining gates before any runtime use

1. Keep the LTS release pinned; separately review vulnerability advisories,
   support window, and lock system packages/toolchain and all runtime inputs.
2. Produce a complete, relocatable runtime with SBOM, signed provenance,
   dependency inventory and verified checksums.
3. Scan and inspect all archive entries (symlinks, paths, ownership and modes)
   before any extraction.
4. Verify in a disposable compatible Linux VM, then on an isolated
   Unraid-compatible test host; never on a production host first.
5. Test non-systemd supervision, root socket permissions, idmaps, storage,
   device cgroups, GPU/NVENC, network isolation, boot, and rollback.
6. Require a separate explicit approval before deploying any runtime to Unraid.

Incus runs with host-level privileges. Never expose its management socket to
the LAN by default, mount the Docker socket, or run an unreviewed installer.
