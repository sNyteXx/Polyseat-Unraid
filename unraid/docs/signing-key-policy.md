# 🚧 WIP — DO NOT USE — Artifact signing and separate host-approval policy

Goal: nothing runs on a real host unless a maintainer deliberately signed the exact
artifact and the owner separately approved the exact host actions. Artifact creation
or signing is never approval to install, start Incus or change production.

## Mechanism (implemented)

`polyseatctl` verifies `ssh-keygen -Y verify -n polyseat-unraid-bundle -I polyseat-unraid-release` of the manifest against
the pinned `/boot/config/plugins/polyseat-unraid/allowed_signers`. The manifest binds the archive SHA-256, every file hash,
the supported host range and the LSM/rootfs facts, so one signature covers the whole bundle. An optional `revoked_keys`
file (public keys, one per line) is passed to `ssh-keygen -Y verify -r`; a revocation file that is not a regular file makes
verification fail. Tests cover wrong signer, tampered manifest, missing signature, missing signer file and revocation.

## Rules

1. **Key type and custody:** Ed25519; preferably `ssh-keygen -t ed25519-sk -O verify-required` on a FIDO2 hardware key.
   Generated and used only on a maintainer's machine, never in CI, never in the repository, never on Unraid.
2. **CI never release-signs.** CI produces the unsigned bundle, the SBOM and `SHA256SUMS`; a maintainer verifies the CI run URL and
   commit SHA, rebuilds or compares if required, then signs the manifest locally and attaches the `.sig` to the release.
3. **One key, two holders at most;** a second key is only added with `valid-after`/`valid-before` options for rotation.
4. **Rotation:** publish a new plugin version whose `allowed_signers` lists the old and new key with overlapping validity;
   the pluginmanager keeps an existing `allowed_signers` file, so the version bump must replace it explicitly (SHA256 attribute).
5. **Revocation:** add the public key to `revoked_keys` (shipped by a plugin update and by hand on hosts), remove it from
   `allowed_signers`, publish a statement of the affected bundle SHA-256 values. Revocation does not depend on network access.
6. **Canary host approvals** use the identity `polyseat-unraid-canary-approver` and namespace
   `polyseat-unraid-canary-approval-v1`. They bind the host DMI UUID hash, one runtime archive SHA-256, explicit actions and
   a maximum 1800-second validity. They use a **different key** from the artifact signer. The committed gate remains
   `closed`; the implemented `canary` validator requires this approval and allows emergency teardown after expiry.
   A bundle signature alone never authorizes a production run (see P5 in the canary plan).
7. **Ceremony record:** date, key fingerprint, artifact SHA-256, commit SHA, run URL, signer; stored with the release notes.
8. **Test keys** (CI e2e, local tests) are throw-away, labeled, and never trusted by a host that has the real policy.
   Disposable tests may sign their fixtures with these keys. The owner-artifact `seal` CLI refuses known CI environments.

## Canary-only artifact identity (an explicit owner choice, not a default)

The owner may choose a dedicated Ed25519 software key on an owner-controlled development
machine instead of the general release key for the first canary. This key has only the
artifact role below; it must not be trusted for unrelated releases or as the host-approval
key. Keep its directory mode `0700`, private key mode `0600`, and its custody outside the
repository, CI, build containers, caches, artifacts and Tower. Prefer an encrypted key
selected through an owner-controlled signing agent or a hardware key. The signing tool
accepts a `.pub` path to select an already loaded agent key; it never generates a key.

**Limit:** a private key readable by the same account that executes dependency builds or
coding agents is not isolated from those builds/agents. Moving it to another directory
alone does not establish that boundary. Separate owner/account/hardware custody is
stronger. This tradeoff and the public-key fingerprint require an explicit owner decision;
the tooling does not silently substitute its own key when a provider API is exhausted.

The first public-key fingerprint is verified independently of the kit, before any first
write to Tower. A public key downloaded with its own unsigned installer cannot authenticate
that installer. The kit verifier consequently rejects `--trusted-key` paths inside the kit
and requires the separately supplied `--fingerprint`. `artifact-signer.pub` inside a kit is
documentation only. A plain public-key `revoked_keys` list is supported; a malformed,
non-regular or matching revoked key fails closed.

## Artifact formats and namespaces

| Object | SSH identity | SSH signing namespace | What is bound |
|---|---|---|---|
| Runtime manifest | `polyseat-unraid-release` | `polyseat-unraid-bundle` | Exact archive, all runtime files, ABI/LSM contract; includes the fixed BusyBox image |
| Canary controller `.txz` | `polyseat-unraid-canary-artifact` | `polyseat-unraid-canary-controller-v1` | Exact controller package bytes, including the `canary` gate |
| `canary-kit.json` | `polyseat-unraid-canary-artifact` | `polyseat-unraid-canary-kit-v1` | Controller/runtime signatures, exact source commit, plan, image facts, provenance and every evidence file |
| Live host approval | `polyseat-unraid-canary-approver` | `polyseat-unraid-canary-approval-v1` | One host, one bundle, explicit actions and at most 30 minutes; separate key and owner approval |

Kit validity is at most seven days, normally one day. This is artifact review freshness,
**not** the live approval lifetime. Expired/future-dated kits, unknown scope changes,
unlisted files, symlinks/hardlinks, changed hashes, wrong namespaces/signers and revoked
keys are rejected. `SHA256SUMS` covers the complete sealed kit including its manifest and
signature. The signature anchors those checksums, not the other way around.

## Offline preparation and signing ceremony (implemented, not executed on Tower)

1. Obtain an exact green runtime build from the pinned source commit. The BusyBox image
   must already be `payload/share/canary/busybox.tar.gz` inside the runtime bundle, built
   from the pinned Ubuntu `.deb` and binary hashes. Do not append an image to a previously
   tested archive and call it the same artifact.
2. Build the separate controller artifact outside the repo:

   ```sh
   python3 unraid/scripts/build-canary-package.py --version 2026.10.30.canary \
     --out "$CANARY_OUTPUT" --acknowledge-artifacts-only
   ```

   The ordinary `build-plugin-package.sh` still accepts only `closed`/`test-host`. The
   dedicated builder emits no `.plg`, performs no install, and leaves source gate `closed`.
   Package files, modes, ownership and timestamps are normalized and reproducibility-tested.
3. Rehearse that exact `.txz` and runtime bundle on a disposable Linux runner. The result
   is `canary-rehearsal.json`, schema 1, with the full source `commit`, `archive_sha256`,
   `controller_sha256` (the **entire `.txz`**, not just the script), `platform: disposable-linux`,
   `status: passed`, and unique `steps` including `signature-stage`, `activate`,
   `prestart-policy`, `canary-smoke`, `expiry-refusal`, `cleanup`, `host-state-unchanged`.
   Record any CI-only cgroup/CPU placement overrides separately; do not present those as
   proof of Unraid cgroup topology. The canary's data path is ephemeral
   `/var/lib/polyseat-unraid-canary`; this first test proves no persistent storage behavior.
4. Record provenance JSON (`schema: 1`, `repository: sNyteXx/Polyseat-Unraid`, full `commit`,
   `archive_sha256`). `origin: github-actions` also requires numeric `run_id`, `artifact_id`
   and the matching GitHub `run_url`; `origin: local` instead requires an explicit `build_id`
   and may not contain CI identifiers. Validate the GitHub run and commit independently.
   This record is not a SLSA attestation and cannot by itself prove an external CI result.
5. Assemble the unsigned kit, using the exact canary package from the rehearsal:

   ```sh
   python3 unraid/scripts/canary-artifacts.py prepare \
     --archive "$RUNTIME_ARCHIVE" --manifest "$RUNTIME_MANIFEST" \
     --controller "$CANARY_PACKAGE" --plan unraid/docs/production-canary-plan.md \
     --provenance "$PROVENANCE_JSON" --evidence-dir "$PROOF_DIR" \
     --out "$NEW_KIT_DIRECTORY" --expires-in 86400 --acknowledge-artifacts-only
   ```

   Preparation compares **every** packaged controller source file and the plan against
   the named Git commit; the image's binary/version must match that commit's pins. It
   requires green ABI, exact-bundle SBOM, controller E2E and exact-artifact canary-rehearsal
   evidence. Missing or stale evidence blocks preparation. Read the complete dependency
   triage and compare local/CI builds before deciding whether to sign.
6. Only the owner/maintainer performs the artifact signing ceremony:

   ```sh
   python3 unraid/scripts/canary-artifacts.py seal --kit "$NEW_KIT_DIRECTORY" \
     --signing-key "$OWNER_ARTIFACT_KEY" --trusted-key "$TRUSTED_PUBLIC_KEY" \
     --fingerprint "$INDEPENDENTLY_VERIFIED_FINGERPRINT" --revoked-keys "$REVOCATIONS" \
     --acknowledge-artifacts-only
   ```

   This signs runtime/controller/kit, never creates a private key or live approval. It
   renders only an external `polyseat-unraid-canary.plg.disabled` **preview**. Even if
   renamed to `.plg`, its first inline step unconditionally aborts before any download or
   host write. It has no activation/start command or trust bootstrap. It is not the
   production installer; the exact first-write commands remain a separate owner-reviewed
   handoff. No source `.plg` is activated and no automatic release is published.
7. Verify with the independently pinned trust root, off-host, before any transfer:

   ```sh
   python3 unraid/scripts/canary-artifacts.py verify --kit "$NEW_KIT_DIRECTORY" \
     --trusted-key "$TRUSTED_PUBLIC_KEY" --fingerprint "$INDEPENDENTLY_VERIFIED_FINGERPRINT" \
     --revoked-keys "$REVOCATIONS" --require-rehearsal
   ```

   A success explicitly reports `unraid_test_approved: false`. The owner still has to
   approve the current Go/No-Go plan and independently issue a short-lived host approval
   using the different approval key. Production handling must check the exact package
   hash/signature **before** running any package-provided program.

## Local/CI provenance comparison

`canary-artifacts.py compare-runtime --left-archive ... --left-manifest ...
--right-archive ... --right-manifest ... --output comparison.json` statically verifies
both archives and images, then compares exact payload bytes and ABI/LSM contracts. It
reports archive equality separately. Only the explicitly reported `name`/`version`
labels are excluded from the contract comparison because CI/localdev labels differ.
An ELF build path, linked library, mode, source epoch or binary difference is a real
reported difference and causes exit 3. This is not a substitute for reproducible builds
or independent source provenance. No differing archive is relabeled as the CI artifact.

## Remaining owner / target decisions

- Choose actual artifact-key and **distinct** host-approval-key custody and pin their
  public fingerprints independently. Test keys never migrate into production trust.
- Verify the final CI run, candidate hashes, dependency triage, replay evidence and live
  host preflight; explicitly accept any disclosed CI/local differences.
- Confirm the maintenance window, independent recovery access and backup risk decision.
- Approve the exact first-write commands and only then issue the live approval token.
- Unraid plugin-manager/boot, cgroup2-unraid, GPU and persistent-storage behavior remain
  separate target evidence; a successful Linux rehearsal is not Unraid certification.
