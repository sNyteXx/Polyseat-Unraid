# 🚧 WIP — DO NOT USE — Local-dev build (NOT FOR UNRAID)

`unraid/scripts/dev-build.sh` compiles the pinned Incus 7.5.1 + LXC 6.0.6 runtime bundle on a developer machine for a
fast edit–build–check loop. It is **not** a release path and not a source of test artifacts: those come from the GitHub
workflow only. The CI gate in `build-incus-artifact.sh` is untouched, and the dev script never sets or fakes a CI
variable (inside CI it refuses to run).

## What it does and never does

| Does | Never does |
|---|---|
| builds raft, cowsql, liblxc, incus/incusd from SHA-pinned sources (`unraid/build-pins.env`) | start `incusd`, create a container, run the controller |
| runs the ABI gate against the Unraid 7.3.3 baseline, builds the reproducible bundle and a runtime SBOM | run as root, use `sudo`, install or change any host package or service |
| writes `PROVENANCE.txt` + `NOTICE.txt` ("LOCAL-DEV / NOT-FOR-UNRAID") | delete anything (interrupted caches and old build trees are moved aside) |
| refuses on Unraid, as root, in CI, on non-x86_64 Linux, with an existing output directory | reuse CI paths or caches |

## Usage

    sh unraid/scripts/dev-build.sh --dry-run          # validate and print the plan, create nothing
    sh unraid/scripts/dev-build.sh                    # docker mode, 4 CPUs, 12 GB
    sh unraid/scripts/dev-build.sh --jobs 2 --memory 8
    sh unraid/scripts/dev-build.sh --native           # host toolchain; installs nothing, refuses if tools are missing

Everything lives below `POLYSEAT_DEV_HOME` (default `~/.cache/polyseat-unraid-dev`, must be below your home, not a
symlink, owned by you, mode 0700): `cache/` (source mirrors, dependency builds, Go caches) and
`out/<UTC>-<git7>[-dirty]/` (one directory per run; never overwritten). `PROVENANCE.txt` records the git commit, the
dirty-file count and diff hash, pins, tool versions, builder image id, limits, cache hit/miss and timings.

## Isolation and security limits

- Docker mode runs the build in a pinned builder image (base by digest, Go by version + SHA-256) as **your uid**, with
  `--cap-drop ALL`, `no-new-privileges`, `--read-only` root, `--cpus/--memory/--memory-swap/--pids-limit`, a private IPC
  namespace, no devices, no docker socket and exactly three bind mounts: the repo (`:ro`), the cache and this run's output.
  Network access stays on (git, Go modules). Building the image needs `docker build` once (apt + Go download).
- apt packages inside the image are not version-pinned, so results are source-pinned and reproducible per image, not
  across image rebuilds. The dependency cache key includes the pins, the core script hash and the **image id**, so a
  changed toolchain can never reuse an old entry; the CI and dev caches never mix.
- The dev bundle's version label is `localdev`, it uses `-trimpath`, and its hash differs from the CI bundle by design.
- Cleaning is manual: `out/` and `cache/work/*.old.*` / `cache/deps/*.incomplete.*` can be removed by you when you wish.

## Measured (28-core workstation, container limited to 4 CPUs / 12 GB; GitHub numbers from run 41124288366)

| Case | Wall time |
|---|---|
| first ever run (builder image build 43 s + empty caches) | 191 s (deps 39 s, Incus 95 s, package 14 s) |
| deps cache miss, Go cache warm | 91 s |
| everything warm (deps hit) | **21–26 s** |
| GitHub: build step / toolchain setup / whole workflow | 221 s / 21 s / 482 s (the end-to-end part, 197 s, is needed either way) |

Reproducibility: two builds with a cold and a warm dependency cache produced the **same bundle SHA-256**
(`96569ba5…`, `incus` and `incusd` byte-identical). A first version used a per-run build path and `incusd` differed;
the fixed build path fixed it.

Recommendation: use the local build for the compile → ABI → bundle → SBOM loop (about 10× faster when warm); keep CI for
the controller end-to-end and for every artifact that is shared.

## `ubuntudev` as build host

Not evaluated: the name is unresolvable from the workstation (no SSH alias, no DNS) and no address was guessed or
scanned. Needed before any use: the host name or address, permission to run `docker run` (or a Go 1.26.7 + C toolchain
for `--native`), at least 10 GB free in the cache path and 14 GB of available memory. With Forgejo/BuildKit running
there, `--cpus 4 --memory 12` limits the build container but not `docker build` of the one-time image; `--native` avoids
the shared BuildKit altogether. Abort if memory or disk is tight (the script checks both before starting).
