# 🚧 WIP — DO NOT USE — CPU-only runbook for a separate Unraid test system

**Never run this on the production server.** It is the checklist for a
disposable Unraid 7.x host or VM that you explicitly approve (no GPU, no
important VMs or shares, nothing you cannot reinstall). The production host
with the exclusive-passthrough GPU is out of scope.

## Preconditions to approve first

1. A separate Unraid ≥ 7.2 test system with a pool for `DATA_DIR` and no
   production data. Record its version, kernel, glibc.
2. Create `/boot/config/plugins/polyseat-unraid/DISPOSABLE_TEST_HOST` whose
   first line is exactly `polyseat-unraid-disposable-test-host-v1`.
3. Use a **test build** of the controller package (gate `test-host`, from
   `build-plugin-package.sh VERSION OUT test-host`). The committed source has
   gate `closed` and will refuse. Never use `open` for testing.
4. A signed test bundle produced by CI from `check-bundle-abi.py` +
   `make-runtime-bundle.py`, signed with a throwaway key whose public line is
   in `allowed_signers`.

## Steps and expected results

| # | Step | Expected |
|---|---|---|
| 1 | `polyseatctl preflight` | all PASS; no LSM noted; read-only |
| 2 | Set `DATA_DIR` (e.g. `/mnt/<pool>/polyseat`), mode `o+x` on the path | `polyseatctl status` shows it |
| 3 | `polyseatctl update ARCHIVE MANIFEST SIG --approve` | `state=ACTIVE`, journal absent |
| 4 | `polyseatctl start` then `health` | `healthy`; `/etc/subuid` has the managed block |
| 5 | Using the bundle's `incus` with `INCUS_DIR=$DATA_DIR/incus`: preseed a **dir** pool and a disk-only profile (no NIC — never `incus admin init --minimal`), import a tiny image, `init`, `start`, `exec echo OK` | prints `OK`; no new bridge appears (`ip -br link` unchanged) |
| 6 | `incus stop` / `incus start` the container | works, repeatedly |
| 7 | `polyseatctl restart` | container keeps running; daemon PID changes |
| 8 | Watch `cgroup2-unraid` interplay: start/stop several times | no vanished LXC cgroups, no errors in `incusd.log` |
| 9 | Update to a second signed bundle | service restarted, previous kept |
| 10 | Update to a bundle whose `incusd` fails to start | exit 4, previous restored and running |
| 11 | Kill `polyseatctl update` mid-run (e.g. `kill -9`), then `boot` | recovery to previous bundle |
| 12 | Reboot the test system | RAM slot rebuilt; service starts only if `SERVICE="enabled"` |
| 13 | `polyseatctl uninstall --approve` | slots, bundles, state, subuid block gone; settings and `DATA_DIR` intact |
| 14 | Compare `ip -br link`, `iptables -S` chain names, `ps` against the pre-test capture | no change attributable to the plugin |

Also check on the test system: unprivileged container start with idmapped
mounts on the chosen filesystem (ZFS/XFS/btrfs behave differently), and that
no `incusd` was left running after uninstall.

## What to attach when reporting

`polyseatctl diagnose` (redacts token/password/key values), the controller and
bundle versions, filesystem type of `DATA_DIR`, and the `incusd.log`.

## Evidence status

No Unraid test system has run any of this. The same sequence is exercised only
with stub daemons in `unraid/tests/test_polyseatctl.py` and, once the runtime
bundle job exists, with a real `incusd` on a disposable GitHub-hosted Ubuntu
runner. Neither is Unraid evidence.
