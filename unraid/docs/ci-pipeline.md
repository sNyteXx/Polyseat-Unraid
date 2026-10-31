# CI pipeline for the Unraid WIP branch: fast lane, full lane, verified runtime reuse

Draft PR #1, branch `wip/unraid-native-plugin`. No workflow publishes, installs, releases or merges anything.

## What triggers what

| Workflow | Trigger | Jobs | Notes |
|---|---|---|---|
| `ci.yml` (upstream Polyseat) | push `main`/tags, every `pull_request` | go, web, shell, broker, desktop (~2 min, parallel) | upstream code; unchanged, not on the critical path |
| `package.yml` (upstream) | push `main` (packaging paths), dispatch | AUR/nfpm packages | never runs on the WIP branch |
| `unraid-preflight-lint.yml` — **fast lane** | push and pull_request touching `unraid/**` | `shell-check` (lint + whole offline/unit suite), `hook-l2` (push on the WIP branch) | **before:** ran twice per push (push + same-repo pull_request, ~2:09 each); **now:** the same-repo pull_request duplicate is skipped, fork PRs still run |
| `incus-feasibility.yml` — **full lane** | **before:** every push touching any runtime, controller, hook, canary or seat path; **now:** push touching a runtime build input, the seat image or the workflow, plus `workflow_dispatch` for candidates | `canary-cgroup`, `build` (pinned runtime + CPU canary rehearsal + controller E2E + smoke + scans + test artifact), `seat-image` (reusable `seat-image.yml`), `desktop-canary` (+ L2 hook rehearsal) | every Tower candidate is qualified from a full run of its exact commit |
| `seat-image.yml` | push `wip/seat-image-desktop`, `workflow_call` from the full lane, dispatch | seat image build + apt-Incus desktop smoke | not run twice on the WIP branch (removed earlier) |

## Runtime reuse (content-addressed, fail-closed)

`unraid/scripts/ci-runtime-key.py` hashes every input of the pinned runtime build: build scripts and the Python
modules they import, dependency overlay, ABI baseline, runtime pins (seat pins excluded), the whole full-lane
workflow (apt build dependencies, action pins, env pins), the key and reuse scripts themselves, and the runner image
(`ImageOS`, `ImageVersion`, arch) because the apt build dependencies are not version-pinned. A unit test proves that
every input changes the key, that docs/controller/seat pins do not, and that every `$CORE_REPO/...` reference in the
build is covered.

A fresh full run snapshots `incus-build-proof` and the stage parts later steps use, **before** any rehearsal writes
evidence; only if the whole job is green it seals the snapshot to its run id/attempt/commit and to the digest of its
uploaded proof artifact, and saves it to the Actions cache under that key. A later run reuses it only if
`ci-runtime-reuse.py verify` passes all of: key and inputs identical, every file and symlink identical to the
sealed list (nothing extra/missing, symlinks stay inside), the proof's own `SHA256SUMS`, bundle named by its hash and
named by its manifest, the producer run is a **completed, successful** `incus-feasibility.yml` run of this repository
at the recorded commit/attempt, and its proof artifact still exists, is not expired and has the recorded digest.
In the same step the verifier must **refuse** a copy with one flipped byte, `INCUSD_REQUIRED_PROGRAMS` of the current
controller must equal the runtime's, and the ABI check is re-run on the reused payload. Any failure, lookup error or
missing record → full rebuild (never a red build because of the cache). Each run records
`runtime-provenance.json` (`reused`, producing run, checks) into its proof and test artifact.

Skipped on a verified reuse: only the Incus/LXC/raft/cowsql/squashfs-tools source build and the GoBGP regression
test of that unchanged source. Everything else still runs on every full run: CPU canary rehearsal (signed gate,
TTL, supervisor loss, Tower PATH, exec inventory), controller E2E with the real bundle, CPU smoke, proof checksums,
OSV and govulncheck (current databases), test artifact, seat image, desktop canary, L2 hook rehearsal.

Trust boundary (stated, not hidden): cache entries of this branch can only be written by workflow runs on this
branch, i.e. by code with write access. The verification catches corruption, failed or stale producers, recipe and
runner drift and expired provenance; it is no defence against a malicious committer (who could change the workflow
itself). The Tower gates do not depend on the cache: every kit is requalified locally from the candidate run's own
GitHub artifacts (digests checked), with its own rehearsals, signatures, `gpu-preflight` and a separate owner GO.

## Which steps run

| Change | Fast lane (`unraid-preflight-lint.yml`) | Full lane (`incus-feasibility.yml`) |
|---|---|---|
| Docs, controller, hook wrapper, canary/test scripts, unit tests | lint, unit suite, L2 hook rehearsal on the verified cached runtime (skipped with a notice if none) | not automatic; `gh workflow run incus-feasibility.yml --ref wip/unraid-native-plugin` for a candidate (reuses the runtime) |
| Runtime build input (Incus/LXC pins, build scripts, overlay, ABI baseline, workflow) | lint, unit suite; `hook-l2` skips (new key) | automatic, **full rebuild** (cache miss), then sealed for reuse |
| Seat image | lint, unit suite | automatic; runtime reused if its key is unchanged |
| Runner image update (weekly) | `hook-l2` skips until a full run | next full run rebuilds |

## Measurements

Before (commit `e0cbf65`, run 41209744754): full lane 11:48 wall; `build` 629 s (deps 12, **source build 246**,
GoBGP 19, CPU canary 114, controller E2E 199, upload 13); `desktop-canary` 73 s; `seat-image` 196 s (parallel);
preflight 129 s **×2** (push + PR); upstream CI 118 s (PR). Every controller or hook change ran all of it.

After: see the PR description / the measurement table appended below after the first runs.
