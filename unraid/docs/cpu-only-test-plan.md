# 🚧 WIP — DO NOT USE — CPU-only test build and the minimal safe plan for an isolated test VM

Decision (2026-10-30): the first CPU-only test stays on **Ubuntu 24.04** (GitHub-hosted runner). A
Slackware-current compatibility VM was considered and deferred; its groundwork is parked on the local branch
`parked/unraid-compat-slackware` (not pushed, not wired into CI).

## What the Ubuntu end-to-end proves — and what it does not

The job `WIP Incus pinned build proof` builds Incus 7.5.1 with LXC 6.0.6 (AppArmor-less), checks the ELF closure against
the recorded Unraid 7.3.3 baseline, builds a reproducible runtime bundle and then drives the real `polyseatctl` with a real
`incusd` (no systemd, CPU-only, no GPU, no NIC, no bridge): install, LSM gate, start, container create/exec/stop/restart,
service restart with a running container, update, failing update with automatic rollback, crash recovery, manual rollback,
graceful stop, simulated reboot, uninstall with state comparison.

It is **not** evidence for: the Unraid kernel and `cgroup2-unraid`, idmapped mounts on ZFS/XFS, Unraid boot order, the
Unraid plugin manager and a rendered `.plg`, the Unraid WebUI, vfat flash behaviour, or anything GPU/streaming. Those
gates stay open and are listed in [acceptance.md](acceptance.md). Differences to Unraid: glibc 2.39 vs 2.44, systemd host,
active AppArmor (handled by the explicit `ACCEPT_NO_LSM` gate), ext4 instead of ZFS/XFS, a sandbox root instead of the
real `/boot`, `/usr/local` and `/etc`.

## The test artifact

Every green run uploads `polyseat-cpu-only-testbuild-NOT-FOR-UNRAID` (14 days) containing:

| File | Purpose |
|---|---|
| `TEST-REPORT.md` | commit SHA, run URL, pinned inputs, steps passed, what is and is not proven |
| `bundle/<sha256>.tar.gz` + `.manifest.json` | the runtime bundle (the SHA-256 is its file name); **unsigned** |
| `polyseat-unraid-<version>-noarch-1.txz` | controller/WebUI package built with gate `test-host` |
| `abi-report.json`, `build-info.txt`, `SBOM-build-source-only.cdx.json` | ABI closure, pins, build-source inventory |
| `e2e-steps.txt`, `e2e-result.txt`, `logs/` | what passed, controller and daemon logs |
| `SHA256SUMS` | checksums of everything above (`sha256sum --check SHA256SUMS`) |

It is deliberately not a GitHub Release and not signed with a release key. A tester signs the manifest with their own
throw-away key (`ssh-keygen -Y sign -n polyseat-unraid-bundle <manifest>`) and puts the public line into
`allowed_signers`; the controller verifies exactly that signature.

## Minimal safe plan for a later isolated test VM (not on the production Tower)

Preconditions (all need your explicit approval, none is done): a **separate** machine or VM with no production data and
no important VMs or shares; Linux with cgroup v2, `jq`, `tar`, `xz`, `ssh-keygen`, `flock`, `rsync`; no GPU, no
passthrough; a pool or disk path for `DATA_DIR` with `o+x` on every parent.

1. Download the artifact, verify `SHA256SUMS`, compare the commit SHA with the PR head.
2. Sign the manifest with a throw-away key; create `/boot/config/plugins/polyseat-unraid/DISPOSABLE_TEST_HOST` whose first
   line is exactly `polyseat-unraid-disposable-test-host-v1` (on a non-Unraid Linux VM, run the CI procedure from
   `ci-e2e-controller.sh` instead, which uses a sandbox root).
3. Run `polyseatctl preflight` (read-only), then follow [test-system-runbook.md](test-system-runbook.md).
4. Record evidence per step; compare `ip -br link`, firewall chain names and processes before and after.
5. Stop on any unexpected change; do not repair host networking, drivers or storage automatically.

Real Unraid remains a later, separate gate: it needs its own test system, USB boot media and license, which is a separate
approval. TPM licensing inside an Unraid VM is not supported, and no licensing check is to be circumvented.

## Risks that remain after a green Ubuntu run

Containers run without AppArmor or SELinux on Unraid (no LSM exists there); isolation relies on user namespaces,
seccomp and capabilities. `cgroup2-unraid` removes empty cgroups and could interfere with LXC cgroups. Incus restores the
power state of instances when the daemon returns, which is wanted for an array stop/start but must be understood. Release
signing keys and a vulnerability review are still open.
