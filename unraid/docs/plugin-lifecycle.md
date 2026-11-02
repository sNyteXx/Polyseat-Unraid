# 🚧 WIP — DO NOT USE — Plugin architecture and lifecycle

Nothing here is released. `polyseatctl` refuses every host mutation while its
committed `POLYSEAT_RELEASE_GATE` is `"closed"`, and the repository contains no
installable `.plg`.

## Where things live

| Kind | Location | Survives reboot | Owned by plugin |
|---|---|---|---|
| Settings | `/boot/config/plugins/polyseat-unraid/polyseat-unraid.cfg` | yes (flash) | **no** — user data, kept on uninstall |
| Verified bundles | `/boot/config/plugins/polyseat-unraid/bundles/<sha256>.{tar.gz,manifest.json,manifest.json.sig}` | yes | yes, exact names only |
| Pinned signer | `.../allowed_signers` | yes | yes |
| Transaction state | `.../state/pointer.json`, `journal.json` | yes | yes |
| Controller, WebUI, events | `/usr/local/emhttp/plugins/polyseat-unraid/` (from a `.txz`) | no, re-installed by the plugin manager | yes |
| Runtime slots | `/usr/local/polyseat-unraid/slots/<sha256>/`, symlink `current` | no (RAM), rebuilt at boot from flash | yes |
| PID, lock, client config | `/var/run/polyseat-unraid/` | no | yes |
| Logs | `/var/log/polyseat-unraid/` (RAM, rotated at 1 MB) | no | yes |
| Incus state, seats, games | `DATA_DIR` (user-selected pool, below `/mnt`, not `/mnt/user`) | yes | **no** — never deleted |

Flash only holds archives and small JSON files, so the design works on vfat
and ZFS flash alike (no symlinks or modes needed there).

## Commands

`polyseatctl status [--json] | verify [SHA] | health | preflight | diagnose`
are read-only. `start | stop | restart | boot` manage the service.
`stage | activate | update | rollback | recover | uninstall` change the
installation and require `--approve`.

## Update/activate transaction

1. Verify on flash: archive SHA-256 equals its file name, manifest schema and
   `archive_sha256`, **`ssh-keygen -Y verify` signature** against the pinned
   signer, `tar` listing equals the manifest exactly (regular files only,
   owner 0:0, modes 0644/0755, safe names, sizes), and a streaming pass checks
   every member hash without writing anything.
2. Host compatibility: Unraid/glibc/kernel minimums and every `needed_libs`
   soname present in `ldconfig -p`.
3. Space check, then extract only declared members into `.incoming-<sha>` in
   RAM, re-verify hashes/modes/file set, rename into `slots/<sha>`, and run
   `incus --version` and `incusd --version` once with a clean environment. A
   bundle with a missing library fails here, **before** the live state changes.
4. Write `journal.json` (phase, target, previous, whether the service was
   running) with write-temp, fsync, rename.
5. Stop the daemon only if it was running, switch `current` by one atomic
   `rename`, write `pointer.json`, restart only if it was running.
6. Health: verified daemon PID, management socket, `incus info`, and **no
   network listener** (`core.https_address` empty). On failure the previous
   slot is restored, the service restarted if it had been running, the journal
   cleared and the exit code is 4.
7. Prune: keep current and previous only, removing manifest-listed files then
   empty directories bottom-up. Undeclared files are left in place.

`recover` (also run by `boot`) finishes or reverts any journalled transaction
to the previous bundle; unknown or tampered state stops everything (exit 3).
`rollback --approve` returns to the previous bundle.

## Service control without systemd

`start` launches `incusd` under `setsid` with an empty environment
(`INCUS_DIR=$DATA_DIR/incus`, `umask 022`, no inherited lock descriptor),
refuses if any other `incusd` exists, and verifies the PID by its command line.
`restart` sends SIGTERM (containers keep running); `stop` first asks
`incus admin shutdown` for a graceful instance stop. Events: `disks_mounted`
runs `boot` in the background, `stopping_svcs` runs `stop`.

## Boot and plugin re-install

The plugin manager re-installs every `.plg` at each boot, possibly before pools
are mounted. Re-activating the already active bundle is a no-op, and a switch
only restarts a service that was running, so early re-installs never need
`DATA_DIR`. Starting an enabled service is the job of `boot` after
`disks_mounted`.

## Uninstall

`uninstall --approve` stops the daemon, removes manifest-listed files and the
managed `/etc/subuid`/`/etc/subgid` block (only if unmodified), the flash
bundles and state files. Settings and `DATA_DIR` are untouched; leftovers it
cannot attribute are reported and kept.

## Host modifications a real install would make (all need approval)

- `/etc/subuid` and `/etc/subgid` managed blocks (both are empty on the
  inspected Unraid and live in RAM), added at service start, never overlapping
  an existing allocation.
- Files only under the paths in the table above. No bridge, firewall rule,
  Docker/libvirt setting, module, GPU binding or `go` file edit.

## Not implemented yet

Settings form in the WebUI, update-availability UI (the plugin manager's
`plugin check` compares the `.plg` `version`), release signing key management,
lxcfs, OCI/VM support, and any GPU/streaming integration.
