# 🚧 WIP — DO NOT USE — Incus build and verification

## Scope of the current workflow

`.github/workflows/incus-feasibility.yml` runs only on a disposable Ubuntu
GitHub Actions runner. `unraid/scripts/build-incus-artifact.sh` actively
refuses to run outside Linux CI or on an Unraid host.

The build uses full, immutable source SHAs (chosen from the Incus 6.0.0 era):

| Source | Revision |
|---|---|
| `lxc/incus` (`v6.0.0`) | `714bcc5e42b189f54025b8567df1f3408a1cae2c` |
| `cowsql/raft` | `e0928710bcc62806cc7074f010e462547decf4b7` |
| `cowsql/cowsql` | `8c7d8cf13d37a1e8d2ec6fa2ee575b07575248df` |

The workflow builds the C libraries in a temporary prefix and then compiles
Incus with the matching headers and libraries. It records the three exact
source revisions, Go/toolchain information, dynamic dependency listing and
SHA-256 checksums.

**Limitations:** Ubuntu package dependencies and GitHub's runner image are
not pinned, so this is source-pinned but not bit-for-bit reproducible. Incus
6.0.0 is an old baseline and is **not approved for deployment**. The output
does **not** include all required runtime libraries, a tested Unraid loader
path, SBOM, signature, vulnerability review, or provenance attestation.
Checksums establish integrity of the CI output, not its trustworthiness.

## Remaining gates before any runtime use

1. Choose a supported, security-reviewed Incus release and lock the complete
   dependency/toolchain set.
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
