# 🚧 WIP — DO NOT USE — Runtime security analysis (supply chain and exposure)

**Security overlay (2026-10-30):** Go remains pinned to **1.26.10**. A reviewed
dependency overlay now upgrades `x/net` to **v0.62.0**, `go-jose/v4` to **v4.1.6**
and `chi/v5` to **v5.4.0**, plus only their seven mandatory transitive graph
changes. Every changed module is pinned by version, module-zip h1 and go.mod h1
in [`dependency-overrides.json`](../dependency-overrides.json). These are build
inputs, **not a claim that an existing older binary has been fixed**. A newly
rebuilt **LOCAL-development** candidate now has valid scans of both original
binaries, recorded below. **Independent CI proof for this overlay is still
pending**; local scans alone do not prove the CPU-only lifecycle or Unraid safety.

This is a bounded, source-backed triage of the findings below, **not a complete
security audit or Unraid certification**. Method: original unmodified Go binary
build metadata, runtime SBOM, OSV version lookup, valid `govulncheck -mode=binary`
results and pinned upstream source review. Findings remain in raw reports;
nothing is filtered out to make a report green. For a candidate, scan both
shipped binaries (`bin/incusd` and `bin/incus`) and use `--cli-buildinfo` for the
SBOM/OSV inventory union. An incusd-only inventory is explicitly labelled so.

## Bundled `unsquashfs` (squashfs-tools 4.7.5, added after Tower run 1)

| Item | Value |
|---|---|
| Why | Incus 7.5.1 `cmd/incusd/main_daemon.go` requires `unsquashfs` on PATH at daemon start and uses it to unpack squashfs images; Unraid 7.3.3 has no squashfs-tools (run 1 failed there) |
| Source | `https://github.com/plougher/squashfs-tools`, annotated tag `4.7.5` object `1fdc2fe01d681438a04e2a137a9f51d829ba3358` → commit `708c59ae80853b0845017c33b42e56061cc546cd` (release 2026-03-01); the build checks tag type and peel |
| License | GPL-2.0-or-later; `COPYING` ships as `payload/share/licenses/squashfs-tools/COPYING`; the corresponding source is the pinned upstream commit recorded in the SBOM |
| Build | only the `unsquashfs` target, upstream default compressors (gzip, xz, lzo, lz4, zstd) + xattrs, legacy LZMA1 off; `-fstack-protector-strong -D_FORTIFY_SOURCE=2`, PIE, full RELRO; `-ffile-prefix-map`; stripped |
| Linkage | NEEDED allowlist `libz.so.1 liblzma.so.5 liblzo2.so.2 liblz4.so.1 libzstd.so.1 libm.so.6 libc.so.6 (libpthread.so.0)`, all present on Tower and in the ABI baseline; no RUNPATH; payload ABI gate re-checks symbol versions |
| Inventory | SBOM source component `pkg:github/plougher/squashfs-tools@708c59ae…` with tag object and license; OSV queries the pinned commit; the shipped file is hash-bound by the manifest and covered by the artifact signature |

Canary exposure: the fixed BusyBox canary image is a unified **tarball**, so `unsquashfs` is not executed during the
canary lifecycle; it is only found by Incus' startup lookup and executed once as `unsquashfs -version` during
activation. In later real use it parses image files: historical unsquashfs path-traversal issues (for example
CVE-2021-40153 and CVE-2021-41072, fixed in 4.5/4.5.1) show that this is attacker-facing parsing code for untrusted
images. 4.7.5 contains those fixes; the current OSV result for the exact commit comes from CI and is triaged per run.
This is not a claim that squashfs-tools or the host compression libraries are vulnerability-free.

## LOCAL-development overlay scan (2026-10-30)

The fresh UbuntuDev reports in
`/tmp/polyseat-security-overlay-scan-20261030/` were reviewed read-only. Both
`govulncheck v1.9.0` invocations scanned the original, unmodified Go binaries;
scanner output was retained, including the remaining warning. `scan.log`
records `SCAN_EXIT=0` for the reporting pipeline, **not zero vulnerabilities**.

| Check | Actual local result | Interpretation |
|---|---|---|
| `incusd` binary scan | Exit 3; **1 symbol finding**, GO-2026-4736 in GoBGP v4.9.0 | Raw finding remains. The exact source fix and successful upstream regression test are documented below; the database still has no fixed range |
| `incus` CLI binary scan | Exit 0; **0 symbol findings** | No vulnerable symbols were reported; one module-level warning remains, so this is not an absolute clean-bill-of-health claim |
| OSV binary union | **160 linked entries**, **2 advisory references** | GoBGP GO-2026-4736 and x/crypto/openpgp GO-2026-5932; the latter is not fixed by x/crypto v0.58.0 and must stay absent from both package inventories |
| OSV pinned source lookup | **4 pins**, **0 advisory references** | No known matching source-commit advisories returned; this does not prove C libraries or native code vulnerability-free |
| OSV graph-only inventory | **178 entries**, **31 advisory references** | These modules are not linked into either shipped binary according to the union; their findings remain in the raw report |

The new reports no longer contain the four old x/net symbol findings, the
version-level x/net/go-jose/chi findings, or the seventeen Incus pseudo-version
findings. No vulnerability IDs were suppressed to achieve this result. The OSV
report explicitly records `binary_inventory_coverage=incusd-and-incus`.

| Raw local report | SHA-256 |
|---|---|
| `govulncheck.txt` | `627de8d6f4fffe75bf1739cd25c3d93059205a7eb272c48c191e1333bc014a86` |
| `govulncheck-cli.txt` | `b5ae54ddcfa4cb39e85f84ad2d9e43556bd817c480eaab2a71bab63bfc29353b` |
| `osv-report.json` | `f3a494c0460ffc62256632231dd563e679c3fb0956f8d8a54fdd4943e0e257a3` |

This evidence applies to the **local overlay candidate**, not the older CI
artifact. The original binary hashes, build provenance, package inventories,
runtime manifest and these reports must remain together. Rebuilding or changing
the candidate invalidates transfer of this evidence until the new binaries are
checked. Current-overlay CI, candidate reproducibility, full lifecycle evidence
and the separate bounded-host authorization remain distinct gates.

## Historical inventory (before the security overlay)

| Layer | What ships in the bundle | Source |
|---|---|---|
| Daemon/CLI | `incusd` (92 MB, Go 1.26.7), `incus` (36 MB) | lxc/incus `b20ec6c3…` (v7.5.1) |
| C libraries | `liblxc.so.1`, `libcowsql.so.0`, `libraft.so.0` as SONAME-named files, `RUNPATH $ORIGIN` | lxc/lxc `2597434a…` (v6.0.6), cowsql/raft `9edb176a…` (v0.22.1), cowsql/cowsql `783815b9…` (v1.15.9) |
| Go dependencies | 157 modules linked into `incusd` (338 in the module graph) | `go version -m incusd` |
| Supplied by Unraid, not shipped | `libc`, `libcap`, `libacl`, `libseccomp`, `libsqlite3`, `libudev`, `libuv`, `liblz4`, `libgcc_s`, `ld-linux` | ABI report (10 sonames, glibc ≤ 2.38 required, host has 2.44) |

## Historical OSV lookup (2026-10-30, before the security overlay)

The following table and twenty-two-symbol report describe the **old binary**;
they are retained as the baseline, not the current local candidate status.

- Pinned C sources (incus, lxc, raft, cowsql by commit): **0 advisories**.
- Go modules **linked into `incusd`: 11 advisory references on 5 modules**; 31 more
  on modules only in its graph (e.g. `docker/docker`, `aws-sdk-go`, `runc`), not
  linked into the daemon. CLI coverage must be established separately; the new
  inventory union prevents daemon-only results from claiming full bundle coverage.

| Module (linked) | Advisories | Triage status |
|---|---|---|
| `golang.org/x/net v0.59.0` | GO-2026-6603, 6610, 6611, 6612, 6617, 6860, 6866 | Fixed-version overlay prepared: v0.62.0; candidate rebuild/scan required |
| `golang.org/x/crypto v0.57.0` | GO-2026-5932 | No fixed release. Reject either shipped binary if its package inventory imports openpgp |
| `github.com/go-jose/go-jose/v4 v4.1.5` | GHSA-mgcc-cww5-fp2q | Fixed-version overlay prepared: v4.1.6; not dismissed merely because OIDC is disabled |
| `github.com/go-chi/chi/v5 v5.3.2` | GHSA-j6qf-3m9c-vwj3 | Fixed-version overlay prepared: v5.4.0; candidate rebuild/scan required |
| `github.com/osrg/gobgp/v4 v4.9.0` | GO-2026-4736 | Upstream fix already present in exact pinned source; database still has no fixed range. Evidence below |

**Scanner validity incident (2026-10-30):** The `b2b8ad2` CI artifact contained only
`govulncheck: unrecognized binary format` in `govulncheck.txt`. The old workflow
ignored this error with `|| true` and `continue-on-error`; consequently **no
reachability check was completed**, despite green CI. The WIP pipeline now scans
the original Go binary (before the `patchelf` RUNPATH rewrite) and rejects scanner
errors or empty reports while still recording discovered vulnerabilities for
human triage. This scanner fix was confirmed by CI run **41140325600** for
`4400c1e`: the valid report (SHA-256
`9283d3e98c09863a600ed33f8366e09d3c34b615a8d173e8b6455663dbaf926a`)
was byte-identical to the independently produced UbuntuDev report. That proof
precedes the dependency overlay and cannot clear the new candidate by itself.

**Reproduced on UbuntuDev (disposable builder container, Go 1.26.10,
`govulncheck v1.9.0`):** Scanning the *unmodified* local-dev `bin/incusd`
returned exit **3** with **22 symbol findings across 3 modules**:

| Category | Result | Decision for the canary |
|---|---|---|
| `golang.org/x/net@v0.59.0` | 4 HTTP/2 findings: GO-2026-6617, 6612, 6611, 6603; fixed in `v0.60.0` | Real old-binary findings. The overlay uses v0.62.0, also covering later HTML fixes; verify disappearance in fresh reports |
| `github.com/lxc/incus/v7` | 17 findings against `v7.0.0-20260925…` | Incorrect shallow-checkout version metadata. Restore the exact official annotated tag, preserve `+dirty` for the security overlay, and rescan without exemptions |
| `github.com/osrg/gobgp/v4@v4.9.0` | GO-2026-4736, no database fixed range | Linked symbol but fix is present in the pinned v4.9.0 source. Keep raw warning and exact source evidence, rather than declare globally safe BGP |

The scanner also listed one imported-package and three module-level warnings without
matched vulnerable symbols; the OSV report lists additional version-level advisories.
The 22 symbol results must not be misinterpreted as 22 confirmed exploitable paths
on the canary. Equally, a valid scanner result is **not** proof that running on
the production Unraid host is safe. CI must independently scan its new overlay
artifact rather than reuse either this historical baseline or the local proof.
Individual decisions and their limits follow.

## Individual dependency findings and primary evidence

The fixed versions below were checked against the official Go vulnerability
database or the maintainer's advisory; the module h1 values were checked against
`sum.golang.org` and are rechecked by the Go toolchain during every build.

| Advisory | Affected behavior | Reviewed action |
|---|---|---|
| [GO-2026-6603](https://pkg.go.dev/vuln/GO-2026-6603) | HTTP/2 Trailer header memory exhaustion | x/net fixed v0.60.0; overlay v0.62.0 |
| [GO-2026-6610](https://pkg.go.dev/vuln/GO-2026-6610) | HTTP/2 transport accepts malformed framing headers | x/net fixed v0.60.0; overlay v0.62.0; old module/package-level warning is not proof of a called symbol |
| [GO-2026-6611](https://pkg.go.dev/vuln/GO-2026-6611) | Repeated initial-window changes cause excessive CPU use | x/net fixed v0.60.0; overlay v0.62.0 |
| [GO-2026-6612](https://pkg.go.dev/vuln/GO-2026-6612) | HTTP/2 server flow-control double refund | x/net fixed v0.60.0; overlay v0.62.0 |
| [GO-2026-6617](https://pkg.go.dev/vuln/GO-2026-6617) | Concurrent HPACK encoder mutation can crash server | x/net fixed v0.60.0; overlay v0.62.0. Go stdlib fix is also included in the 1.26.10 toolchain |
| [GO-2026-6860](https://pkg.go.dev/vuln/GO-2026-6860) | HTML parser text-node coalescing has quadratic cost | x/net fixed/overlaid v0.62.0; no reachability waiver needed |
| [GO-2026-6866](https://pkg.go.dev/vuln/GO-2026-6866) | HTML raw-text rendering misses ancestor namespace transitions | x/net fixed/overlaid v0.62.0 |
| [GHSA-mgcc-cww5-fp2q](https://github.com/go-jose/go-jose/security/advisories/GHSA-mgcc-cww5-fp2q) | Crafted RSA JWK parsing causes CPU denial of service | go-jose/v4 v4.1.5 → v4.1.6, exact h1 pins |
| [GHSA-j6qf-3m9c-vwj3](https://github.com/go-chi/chi/security/advisories/GHSA-j6qf-3m9c-vwj3) | ClientIPFromXFF trusts a proxy chain without checking the direct peer | chi/v5 v5.3.2 → v5.4.0, exact h1 pins |
| [GO-2026-5932](https://pkg.go.dev/vuln/GO-2026-5932) | x/crypto/openpgp is unmaintained and unsafe; all versions affected | **Not fixed by x/crypto v0.58.0.** Build fails if `go list -deps` for either shipped binary includes openpgp or subpackages. Preserve module-level warning; acceptance is conditional on absence of these packages, not on absence of OIDC/BGP |
| [GO-2026-4736](https://pkg.go.dev/vuln/GO-2026-4736) | Malformed BGP NEXT_HOP can reach validation after an earlier parse error | Exact source-fixed evidence below; raw warning remains because the database declares all versions affected |

### GoBGP GO-2026-4736: source-fixed, not an unreviewed no-fix waiver

The advisory links the upstream [fix commit
583080a7](https://github.com/osrg/gobgp/commit/583080a7258e22cc884162e15b078771aa2c2c80).
The official [compare to v4.9.0](https://github.com/osrg/gobgp/compare/583080a7258e22cc884162e15b078771aa2c2c80...v4.9.0)
reports `ahead_by=288`, `behind_by=0` and the fix as the merge base. The annotated
v4.9.0 tag `96ef11aedd066031f1bacb2c587baaa769ba07eb` peels to release commit
`01c5c4c27f9a1ac3b5927f433b5115f9b0eee791`.

The v4.9.0 source still guards `bgp.ValidateUpdateMsg` with
`handling == bgp.ERROR_HANDLING_NONE`, and contains the upstream regression test
`TestRecvMessageWithError_MalformedNextHop`. The overlay preparer verifies the
GoBGP module's authenticated zip/go.mod sums and these exact source-file hashes:

| File | SHA-256 |
|---|---|
| `pkg/server/fsm.go` | `587966a5228a2ef4ff4cf43c13663a10e3d9e08f54d456381a3ced8c81191a5e` |
| `pkg/server/fsm_test.go` | `86ea3f1e7947a20327abf73ed807a45dcf89ece952a734e6af9c54bcc254e6ef` |

The authoritative hash for the test file is stored in the machine-checked plan;
both hashes and module identity are copied into `dependency-overrides.json` in
the build evidence. An upstream change invalidates this source review. Run the
targeted upstream test on the isolated builder before the candidate is approved:

```sh
go test -mod=readonly -run '^TestRecvMessageWithError_MalformedNextHop$' -count=1 -v github.com/osrg/gobgp/v4/pkg/server
```

**Executed proof:** On UbuntuDev, a disposable non-root builder using Go 1.26.10,
2 CPU/3 GiB limits, a private writable module/build cache and read-only access to
the repository and shared cache passed this test and both `TreatAsWithdraw` and
`SessionReset` subtests (package test duration 0.006 s, `TEST_EXIT=0`). The Incus
source was freshly checked out, all ten overlay graph changes and the GoBGP
source-file hashes were verified before the test. Raw JSON test report SHA-256:
`28e654ae77dbf0a79cdd1460d8f6e83f5409fcefeb9c07ff1ba1088f47ee81b5`;
dependency proof SHA-256:
`3f89e31bdd927902ca5aa67d3d2ec67d4d0e0424ae3caa50f7e1fba1507c59ea`.
This is the upstream targeted test, not a proof of every BGP execution path.

This is evidence about this one bug, not a blanket assurance that BGP is secure.
The canary must still have no NICs, BGP configuration or external listeners.

## The 17 Incus pseudo-version findings

All primary Go records identify fixes at or before 7.2.0. The pinned source is
the official 7.5.1 commit, not an arbitrary commit labelled 7.5.1. The build now
fetches **the pinned annotated tag object**
`f3242b56ed7ab0127f5716d03e270224e24c0762`, verifies it peels to
`b20ec6c3cb17eaff28fc1145238b8769e44ce39d`, and restores its real
`refs/tags/v7.5.1`. Go then computes the module version itself.

| Go advisory | Reported issue | Fixed version in primary record |
|---|---|---|
| [GO-2026-6319](https://pkg.go.dev/vuln/GO-2026-6319) | Cross-project instance-copy restriction bypass | 7.2.0 |
| [GO-2026-6318](https://pkg.go.dev/vuln/GO-2026-6318) | Cross-project custom-volume-copy restriction bypass | 7.2.0 |
| [GO-2026-5810](https://pkg.go.dev/vuln/GO-2026-5810) | Volume snapshot expiry nil dereference | 7.1.0 |
| [GO-2026-5808](https://pkg.go.dev/vuln/GO-2026-5808) | Backup compression argument injection | 7.2.0 |
| [GO-2026-5806](https://pkg.go.dev/vuln/GO-2026-5806) | Trusted image-hash client arbitrary write | 7.2.0 |
| [GO-2026-5803](https://pkg.go.dev/vuln/GO-2026-5803) | Image template symlink host read/write | 7.2.0 |
| [GO-2026-5802](https://pkg.go.dev/vuln/GO-2026-5802) | S3 multipart path traversal | 7.1.0 |
| [GO-2026-5801](https://pkg.go.dev/vuln/GO-2026-5801) | Image exec-output symlink host write | 7.2.0 |
| [GO-2026-5800](https://pkg.go.dev/vuln/GO-2026-5800) | Dependent-volume backup metadata nil dereference | 7.1.0 |
| [GO-2026-5799](https://pkg.go.dev/vuln/GO-2026-5799) | Restricted-project bypass leading to command execution | 7.2.0 |
| [GO-2026-5798](https://pkg.go.dev/vuln/GO-2026-5798) | Image rootfs symlink host read/write | 7.2.0 |
| [GO-2026-5612](https://pkg.go.dev/vuln/GO-2026-5612) | Custom-volume-import nil dereference | 7.0.0 |
| [GO-2026-5397](https://pkg.go.dev/vuln/GO-2026-5397) | Bucket metadata nil dereference | 7.0.0 |
| [GO-2026-5319](https://pkg.go.dev/vuln/GO-2026-5319) | OVN trusts peer-supplied TLS roots | 7.0.0 |
| [GO-2026-5254](https://pkg.go.dev/vuln/GO-2026-5254) | Image import preflight HEAD SSRF | 7.0.0 |
| [GO-2026-5252](https://pkg.go.dev/vuln/GO-2026-5252) | Missing instance-backup volume nil dereference | 7.1.0 |
| [GO-2026-5127](https://pkg.go.dev/vuln/GO-2026-5127) | Snapshot bounds-check panic | 7.0.0 |

The deliberate go.mod/go.sum overlay must remain visible as main module
**`v7.5.1+dirty`**, `vcs.revision=b20ec6c3…`, and `vcs.modified=true`. The package
step refuses incorrect metadata. No `-buildvcs=false`, fake clean tag or manual
version linker flag is used. A small Go 1.26.10 experiment reproduced the entire
transition (no tag → old pseudo-version; real annotated tag → 7.5.1; go.mod edit
→ 7.5.1+dirty); the real candidate still requires its own checked build metadata.

## Exposure reduction already in place

- No network listener: the controller starts `incusd` without `core.https_address`, and the health check fails if one appears.
- Management socket in `DATA_DIR/incus`, root-only; no Docker socket. The canary
  has private mount, cgroup and PID namespaces, but it still shares the host
  kernel and daemon network namespace. This is not VM isolation.
- Disk-only profile and no VM/device/kernel-module configuration. Incus startup
  also probes storage drivers and can otherwise call `modprobe`, even without
  a container NIC or GPU. The canary drops module/raw-I/O capabilities and uses
  private deny wrappers for module tools and for the host storage/VM tools Incus
  would probe on Tower (`btrfs lvm zfs zpool qemu-system-x86_64`); it does not
  modify host tools or unload modules. The real rehearsal compares loaded modules
  and records every exec (`exec-inventory`).
- No image registry access in the canary: the BusyBox image is imported from local files.
- Bundle mechanism: exact file list, owner 0:0, no setuid, no symlinks and ABI/loader
  closure against the Unraid baseline. A signing mechanism is not evidence that
  this candidate has actually been signed. Older cold/warm matching builds do not
  prove reproducibility of the new candidate; repeat the comparison.
- Offline canary image: exact Ubuntu `busybox-static` package and static-binary
  SHA-256 pins, deterministic root filesystem. This is a small test fixture,
  **not a claim that BusyBox 1.36.1 is globally CVE-free**. No NIC, no downloads,
  no untrusted inputs, no mounts of host/application data; do not enable HTTP,
  telnet or other BusyBox applets as services.

## What weakens isolation on Unraid (documented, not hidden)

No AppArmor/SELinux exists on the host (see [lsm-and-security.md](lsm-and-security.md)); isolation relies on user
namespaces, seccomp and capabilities. `cgroup2-unraid` and idmapped mounts on ZFS are unverified there.

## Evidence required before a bounded CPU-only canary

The local two-binary scan and targeted GoBGP test above are now executed evidence.
They do not by themselves complete the remaining candidate/host gates below.

1. Rebuild the exact security overlay; retain `dependency-overrides.json`, package
   lists, source/build/runtime SBOM, original binary metadata and every checksum.
   The complete module-graph diff must consist only of the ten reviewed pins.
2. Obtain valid govulncheck results for **both original binaries**, plus a fresh
   OSV lookup across their union. Scanner failure, empty binary inventory,
   incomplete OSV results and unversioned replacements fail closed. Any new
   advisory outside this review requires new triage, not automatic acceptance.
3. Verify fixed versions in the actual binaries; verify both OpenPGP package
   lists are empty and GoBGP source/test evidence matches. Run the focused GoBGP
   regression test and the full Ubuntu CPU-only/controller/rollback tests.
4. Record new-candidate reproducibility, trusted signing/host-specific bounded
   authorization, final host-state comparison and cleanup/recovery evidence.
   These are separate gates; dependency tests do not imply live-host approval.

**Residual limits:** no Unraid LSM isolation, untested Unraid-specific cgroups/ZFS
behavior, native root daemon, and production workloads sharing the same kernel.
An Ubuntu test, flash backup and Docker snapshot do not eliminate these risks or
back up an unrelated raw VM disk. The first Unraid run must remain bounded,
CPU-only and without GPU/device/network/VM changes.
