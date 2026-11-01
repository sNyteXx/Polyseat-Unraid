# Tower run 22 — upstream polyseatd on the LAN, no seats, 2026-11-01: PASS

Step 3 of [upstream-integration-plan.md](upstream-integration-plan.md). The owner decided "muss in LAN sein" and asked
me to test everything I can on my own.

**Candidate:**
- commit `a2baab8`, full lane 41300687475 (all jobs green; `persist-canary` runs A–D with real ZFS and polyseatd on a
  fixture LAN);
- runtime `24d12d80…`, controller `1b43b73b…`;
- polyseatd `69922780…`, version `0.46.0-unraid.a2baab80f6aa`, upstream `cmd/polyseatd` built static by CI and
  pinned by the controller.

## Setup (one time)

- The UI password was generated on UbuntuDev and stays there, in the keys directory.
- Only its argon2id hash went to Tower: `/mnt/cache/polyseat/polyseatd/credentials.json` (root 0600, directory 0700).
  It was written by upstream's own `internal/auth` through `polyseat-passwd` from the kit.
- With the hash in place, upstream's "whoever opens the interface first chooses the password" window never opens.

## Run (11:21–11:26 UTC)

| Step | Result |
|---|---|
| W1–W7, W7d | token `716a190b…` with `daemon-stage`; the pinned daemon was staged in RAM |
| start | persistent Incus reopened after the clean-stop record of run 21. polyseatd came up in its own child of the canary cgroup, at `https://192.168.178.24:47800` (certificate `8A:08:20:EE…`) |
| listener | exactly `192.168.178.24:47800` (`ss`, `/proc/net/tcp`). Not on localhost, Tailscale, WireGuard or the Docker networks |
| from UbuntuDev (192.168.178.144, LAN) | `setup:false`. `/api/state` without login: 401. Setup takeover: **409** "already has a password". Wrong password: 401. Login: 200. State: no seats, `update_check`/`web_update`/`web_uninstall`/`web_lan_bridge` false, `uplink` br0, state on `cache/polyseat`. `prepare`, `update`, `restart`, `uninstall`, `lan-bridge`: all **403**. Page: 200 |
| while running | the Incus policy guard every 10 s and the persisted seat still stopped: no violation. polyseatd created nothing in Incus |
| stop | `polyseatd-stop`; polyseatd exited by itself, then `incus admin shutdown`, clean-stop record written, `/run/polyseat` removed. **`POLYSEATD_RC=0`** |
| after | uninstall RC=0 (the staged daemon removed); GPU on `nvidia`, idle; **`BR0 UNCHANGED`**; nothing listening on 47800; no polyseatd or incusd process; `/tmp` kit removed. Host diff: the expected plugin paths and a mount count that moves with host activity (our only mount is the dataset) |

## What stays on Tower by design

On `cache/polyseat`:
- `polyseatd/`: credentials, the TLS certificate, the (empty) seat store;
- `library/`;
- the Incus state from step 2.

Everything in RAM was removed.

## Not covered

- seats created by polyseatd (step 5) and the GPU admission at every container start (step 4);
- input: polyseatd's uhid observer keeps backing off because Unraid has no python3;
- an interface that stays up after the token window (the plugin, step 11).
