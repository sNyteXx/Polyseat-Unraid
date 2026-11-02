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
uploaded proof artifact, and saves it to the Actions cache as `polyseat-runtime-v1-<key>-<run id>-<attempt>`
(same directory and path for restore and save: the path is part of the cache version). A later run restores the newest
entry with the prefix `polyseat-runtime-v1-<key>-` (`cache-matched-key`), so an entry that is later refused (e.g. its
proof artifact expired after 7 days) never blocks a newer one. It reuses it only if
`ci-runtime-reuse.py verify` passes all of: key and inputs identical, every file and symlink identical to the
sealed list (nothing extra/missing, symlinks stay inside), the proof's own `SHA256SUMS`, bundle named by its hash and
named by its manifest, the producer run is a **completed, successful** `incus-feasibility.yml` run of this repository
at the recorded commit/attempt, and its proof artifact still exists, is not expired and has the recorded digest.
In the same step the verifier must **refuse** a copy with one flipped byte, `INCUSD_REQUIRED_PROGRAMS` of the current
controller must equal the runtime's, and the ABI check is re-run on the reused payload. Any failure, lookup error or
missing record → full rebuild (never a red build because of the cache). Each run records
`runtime-provenance.json` (`reused`, producing run, checks) into its proof and test artifact.

Found by the first reuse run (41220919953): the CPU canary now starts about a minute into the job instead of after
the ~4 minute source build, and Azure's Accelerated Networking VF (`enP23362s1`, enslaved to `eth0`) hot-plugged
between its before and after snapshots ("unscoped host change: addresses"). It is runner noise, not a canary
effect. The fix keeps the race visible: `unraid/scripts/lib/ci-runner-noise.sh` leaves out of the address/link
snapshots of both canary rehearsals only an interface named `enP<n>s<n>`, enslaved to `eth0`, with driver
`mlx4_core`/`mlx5_core`/`mana`, and records it in `excluded-runner-vfs`. Any other interface change still fails.
A fresh build hid the race only by its timing.

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

Measurement 1 (commit `5c3d0a5`, push, run 41214824348, cache miss as expected): full lane 12:09 wall, `build`
638 s (source build 229, GoBGP 18, CPU canary 121, controller E2E 214); `desktop-canary` 86 s (L2 hook 14 s);
snapshot sealed and saved (393 MB). Preflight push 172 s (`hook-l2` 6 s: no cache yet, skipped with a notice); the
same-repo PR duplicate skipped (2 s).

Measurement 2 (same commit, `workflow_dispatch`, run 41215993159): **no reuse** — `Cache not found` for the very key
saved 4½ minutes earlier, then `Unable to reserve cache` on save. Cause: the save used `runtime-snapshot-new`, the
restore `runtime-snapshot`; actions/cache puts the path into the cache version, so the entry could never match.
Fail-closed (full rebuild, 731 s wall, green), but no saving. Fixed with one directory for snapshot/seal/save, the
per-run key with prefix lookup above, and `WorkflowCacheTests` (mutation-checked: a differing save path or a
missing `restore-keys` fails the suite).

After the fix (all on the WIP branch, 2026-10-31):

| Run | Change | Lane | Cache | Result |
|---|---|---|---|---|
| 41219296632 (`536a913`, push) | workflow (a runtime key input) | full | miss (new key) | wall 10:13; `build` 516 s (source build 169, GoBGP 13, CPU canary 107, E2E 180); sealed and saved as `…-cf386fec…-41219296632-1` |
| 41219296734 (`536a913`, push) | same | fast | miss (full lane still running) | wall 2:22; `hook-l2` 5 s, skipped with a notice |
| 41220919987 (`6d65e21`, push) | controller/hook only | fast | **hit, verified** | wall 2:29; `hook-l2` **31 s** incl. L2 19 s (real incusd/LXC, `--no-pivot` shim, real CLI 1.20.1); `shell-check` 145 s |
| 41220919953 (`6d65e21`, dispatch) | controller/hook only | full | **hit, verified** | source build + GoBGP skipped; `build` failed after 142 s in the CPU canary: Azure VF hot-plug race (above), fixed in `4a9a43e` |
| 41221824997 (`4a9a43e`, dispatch, **M2 candidate**) | CI scripts only | full | **hit, verified** (restore 3 s, verify incl. tamper refusal, drift and ABI recheck 3 s) | wall **7:13** (before 11:48); `build` **328 s** (before 629 s): CPU canary 102, E2E 169, scans 19; `desktop-canary` 100 s incl. L2 17 s; `seat-image` 227 s in parallel; kit qualified from it in 38 s and M2 PASS on Tower (run 8) |

Summary:
- A controller/hook change runs the fast lane (about 2.5 min, `hook-l2` 31 s on the verified cached runtime) instead of the 11–12 min full lane.
- A candidate run with an unchanged runtime takes about 7 min instead of about 12 min: −40 % wall, −48 % `build`.
- A runtime change still builds everything (about 10–12 min).
- No security step was removed. Only the rebuild of byte-identical, verified input is skipped; every rehearsal, scan and CI artifact still runs per commit.
