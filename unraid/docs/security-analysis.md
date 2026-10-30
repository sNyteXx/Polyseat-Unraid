# 🚧 WIP — DO NOT USE — Runtime security analysis (supply chain and exposure)

**Toolchain update (2026-10-30):** The WIP build pin is now Go **1.26.10** (official
Linux/amd64 archive SHA-256), with two matching local-dev bundles. The binary
inventory below is from the earlier Go 1.26.7 CI proof; refreshed CI vulnerability
reports and triage of all 11 linked-module advisories are still required.

This is an analysis of the **pinned inputs**, not a vulnerability review and not an Unraid certification. It exists so the
Go/No-Go report for any live-host test has real data to triage. Method: runtime SBOM (`SBOM-runtime.cdx.json`), OSV lookup
(`osv-check.py`, report only), `govulncheck -mode=binary` (CI report only), review of the pinned Incus source.

## Inventory (from the local-dev and CI builds of the same pins)

| Layer | What ships in the bundle | Source |
|---|---|---|
| Daemon/CLI | `incusd` (92 MB, Go 1.26.7), `incus` (36 MB) | lxc/incus `b20ec6c3…` (v7.5.1) |
| C libraries | `liblxc.so.1`, `libcowsql.so.0`, `libraft.so.0` as SONAME-named files, `RUNPATH $ORIGIN` | lxc/lxc `2597434a…` (v6.0.6), cowsql/raft `9edb176a…` (v0.22.1), cowsql/cowsql `783815b9…` (v1.15.9) |
| Go dependencies | 157 modules linked into `incusd` (338 in the module graph) | `go version -m incusd` |
| Supplied by Unraid, not shipped | `libc`, `libcap`, `libacl`, `libseccomp`, `libsqlite3`, `libudev`, `libuv`, `liblz4`, `libgcc_s`, `ld-linux` | ABI report (10 sonames, glibc ≤ 2.38 required, host has 2.44) |

## OSV lookup (2026-10-30, by version, not by reachability)

- Pinned C sources (incus, lxc, raft, cowsql by commit): **0 advisories**.
- Go modules **linked into `incusd`: 11 advisory references on 5 modules**; 31 more on modules that are only in the module
  graph (e.g. `docker/docker`, `aws-sdk-go`, `runc`) and are not linked, so they do not ship.

| Module (linked) | Advisories | Triage status |
|---|---|---|
| `golang.org/x/net v0.59.0` | GO-2026-6603, 6610, 6611, 6612, 6617, 6860, 6866 | open; HTTP/2/HTML class issues; reachability unknown |
| `golang.org/x/crypto v0.57.0` | GO-2026-5932 | open |
| `github.com/go-jose/go-jose/v4 v4.1.5` | GHSA-mgcc-cww5-fp2q | open; only relevant to OIDC/JWT features |
| `github.com/go-chi/chi/v5 v5.3.2` | GHSA-j6qf-3m9c-vwj3 | open |
| `github.com/osrg/gobgp/v4 v4.9.0` | GO-2026-4736 | open; BGP features, not used |

`govulncheck` in binary mode (CI step, `govulncheck.txt`) narrows this to reachable symbols; **a human must read it** and
record accepted/mitigated/blocking per advisory before a Go. Nothing here was accepted yet.

## Exposure reduction already in place

- No network listener: the controller starts `incusd` without `core.https_address`, and the health check fails if one appears.
- Management socket in `DATA_DIR/incus`, root-only; no Docker socket, no host namespaces.
- Disk-only profile; the pinned source loads kernel modules only for devices, QEMU VMs and `linux.kernel_modules`, none of
  which a disk-only container uses (P6 will enforce this in the controller).
- No image registry access in the canary: the BusyBox image is imported from local files.
- Bundle: signed manifest, exact file list, owner 0:0, no setuid, no symlinks, ABI/loader closure checked against the Unraid
  baseline, reproducible build (same SHA-256 from cold and warm caches).

## What weakens isolation on Unraid (documented, not hidden)

No AppArmor/SELinux exists on the host (see [lsm-and-security.md](lsm-and-security.md)); isolation relies on user
namespaces, seccomp and capabilities. `cgroup2-unraid` and idmapped mounts on ZFS are unverified there.

## Decision options for the open advisories (owner decision, not taken)

1. Wait for an Incus patch release that updates `x/net`/`x/crypto` and re-pin (preferred).
2. Rebuild with `go.mod` replace directives for those modules: changes the pinned source set, needs its own review and tests.
3. Accept after reachability analysis shows the affected code paths are unreachable in the canary configuration
   (no listener, no OIDC, no BGP, no registry), documented per advisory.
