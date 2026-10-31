# Tower host observations unrelated to the plugin (documentation only)

Observed read-only on 2026-10-31 on the production Unraid 7.3.3 host. **Nothing here was changed on the host**;
any change needs a separate owner decision and is outside the Polyseat canary.

| Observation | Why it matters | Plugin handling |
|---|---|---|
| `/usr/local/sbin` is mode `0777` (no sticky bit), owner root, and contains world-writable regular files (`syncoid`, `sanoid`, `findoid`, `sleepymutex`) | Root's interactive PATH starts with `/usr/local/sbin`, so any local account could plant a binary that root later runs | The controller is only ever run as `env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin ...`; every daemon/helper call uses that fixed `SAFE_PATH`; `/usr/local/sbin` is never on any canary PATH |
| `/usr` and `/usr/local` are owned by uid `197608`/gid `197121` (no account, no process); `/usr` is an overlay over a squashfs | A future process with that uid could rename entries below `/usr/local` | Disclosed residual risk for `/usr/local/polyseat-unraid` (RAM slots); slots are root-owned `0700`, verified by hash before every execution |
| `cgroup2-unraid --daemon` rmdirs new empty top-level cgroups after ~1 s and can remove a group the moment it empties | Short-lived empty groups disappear | The canary cgroup gets a child immediately; teardown treats an externally removed (necessarily empty) group as removed and then checks for leaked processes |
| elogind creates a numeric top-level cgroup per SSH session | Session processes are not in the canary groups | Supervisor and daemon are moved into their own groups; losing SSH does not extend the canary beyond its TTL |

Suggested owner follow-up (not performed): `chmod 0755 /usr/local/sbin` and review the world-writable files, after
checking which plugin creates them at boot (the directory is rebuilt in RAM on every boot).
