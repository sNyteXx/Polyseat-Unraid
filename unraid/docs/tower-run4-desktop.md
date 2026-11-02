# Tower run 4 — M1 desktop canary, 2026-10-31: desktop RAN, formal verdict FAIL (isolation check too strict)

Owner GO for exactly: commit `de730c9a39431ebc2cf8b9880f81578887cdf55e` (CI 41204671862, 11/11 green), runtime
`301c39bcf1abcb632e3908b069f087ec2f92cdc3b03a764c8102e3a172fba79a`, controller
`c342b0bbc8e41400745e62db8bf8c1ae055383b7523bd0fda4c457b03f4fd664` (file `1b15f113…` = CI rehearsal), seat image
`4f9c2b7223a03350824c78c4c1069cbb1f206e26794b845c0583ac71478a93dd` (manifest `3e7f8d44…`), token `2af5f7bd…`
(13:29:42–13:54:42 UTC, actions stage/activate/seat-image-stage/desktop-smoke).

| Step (UTC) | Result |
|---|---|
| S0 13:29 | baseline: no Polyseat state besides flash dir, lock and logs; GPU on `nvidia`; only UbuntuServer running; MemAvailable 18.2 GiB |
| W1–W4 | token replaced; 8/8 hashes, 3/3 signatures; gate canary once; preflight PASS; `gpu-preflight` allowed |
| W5–W7 13:30:39–13:30:45 | stage, activate, **seat image staged on the RAM root (run-3 fix works)** |
| W8 13:30:50–13:31:19 | daemon healthy 13:30:56; storage; import 9 s; start 13:31:07; containment: unprivileged, own net namespace (`net:[4026536905]` vs host `net:[4026531833]`, sampler); **self-test: systemd `running`, user manager, sway `HEADLESS-1 1280x720`, grim screenshot (198 colours, sha `3f43e506…` = CI), Sunshine active with `capture = wlr` and `Found H.264 encoder: libx264 [software]`, Sunshine listening on :47989 inside the container, session stopped cleanly** |
| verdict | **FAIL** on `netns-only`: it required `/sys/class/net` = `lo` only and saw `bonding_masters lo tunl0` → controller hard-stopped (RC=4); M2 not run (needs M1 PASS); no retry |
| peaks (sampler) | canary group memory 2.0 GB (incl. RAM-backed rootfs), 72 pids, CPU throttled within 2 CPUs on cores 24–27; host MemAvailable ≥ 16.0 GiB |
| W9/S1 | uninstall RC=0; no Polyseat/Incus/LXC mount or process; mount-table change = the owner's backup tool's ZFS snapshot mounts (`/mnt/disks/cloud-backup-source/…`) |
| /tmp kits | owner-approved: 25 files removed exactly |
| residue | **1.33 GB in RAM** under `/var/lib/polyseat-unraid-canary` (43 428 entries; the stopped container's rootfs, because the run failed before `delete`); listing digest `a7496636f771e4c36bc146c770836bf154b3680e95c8cbf7bc4916191afc4256`; inert, blocks a new run until reboot or an approved exact cleanup |

**Why the check failed:** `bonding_masters` is a plain sysfs file added by the host's bonding module, not an interface;
`tunl0` is the IPIP fallback device the kernel creates in every new network namespace while `ipip` is loaded on the host.
Both are Unraid facts the CI runner did not have. The container had its own network namespace.

**Fix (next candidate):** `netns-classify` in the image accepts only real interfaces = `lo` plus kernel fallback tunnel
devices that are DOWN and unaddressed, everything else fails; the controller additionally proves host-side that the
container's net namespace differs from its own; on any failure after start the controller first deletes the test
instance with its allow-listed commands (small residue instead of a full rootfs); CI loads `ipip` and `bonding
max_bonds=0` like Tower before the baseline.
