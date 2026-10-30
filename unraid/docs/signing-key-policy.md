# 🚧 WIP — DO NOT USE — Release signing key policy (draft; no key exists yet)

Goal: nothing runs on a real host unless a maintainer deliberately signed exactly that bundle manifest.

## Mechanism (implemented)

`polyseatctl` verifies `ssh-keygen -Y verify -n polyseat-unraid-bundle -I polyseat-unraid-release` of the manifest against
the pinned `/boot/config/plugins/polyseat-unraid/allowed_signers`. The manifest binds the archive SHA-256, every file hash,
the supported host range and the LSM/rootfs facts, so one signature covers the whole bundle. An optional `revoked_keys`
file (public keys, one per line) is passed to `ssh-keygen -Y verify -r`; a revocation file that is not a regular file makes
verification fail. Tests cover wrong signer, tampered manifest, missing signature, missing signer file and revocation.

## Rules

1. **Key type and custody:** Ed25519; preferably `ssh-keygen -t ed25519-sk -O verify-required` on a FIDO2 hardware key.
   Generated and used only on a maintainer's machine, never in CI, never in the repository, never on Unraid.
2. **CI never signs.** CI produces the unsigned bundle, the SBOM and `SHA256SUMS`; a maintainer verifies the CI run URL and
   commit SHA, rebuilds or compares if required, then signs the manifest locally and attaches the `.sig` to the release.
3. **One key, two holders at most;** a second key is only added with `valid-after`/`valid-before` options for rotation.
4. **Rotation:** publish a new plugin version whose `allowed_signers` lists the old and new key with overlapping validity;
   the pluginmanager keeps an existing `allowed_signers` file, so the version bump must replace it explicitly (SHA256 attribute).
5. **Revocation:** add the public key to `revoked_keys` (shipped by a plugin update and by hand on hosts), remove it from
   `allowed_signers`, publish a statement of the affected bundle SHA-256 values. Revocation does not depend on network access.
6. **Canary approvals** use a separate namespace (`polyseat-canary-approval`), are bound to one bundle SHA-256 and carry an
   expiry; a bundle signature alone never authorizes a production run (see P5 in the canary plan).
7. **Ceremony record:** date, key fingerprint, artifact SHA-256, commit SHA, run URL, signer; stored with the release notes.
8. **Test keys** (CI e2e, local tests) are throw-away, labeled, and never trusted by a host that has the real policy.

## Open

Key generation and storage choice (owner), the `canary` gate and approval file (not implemented), SLSA-style build
attestation for the release job (planned), independent rebuild comparison.
