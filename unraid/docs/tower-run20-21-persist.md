# Tower runs 20 and 21 — persistent Incus state on the cache dataset, 2026-11-01: PASS

Step 2 of [upstream-integration-plan.md](upstream-integration-plan.md).

**Candidate:**
- commit `1f0bcfe`, full lane 41292898688 (all jobs green, including `persist-canary` on a real ZFS dataset `cache/polyseat`: fresh, reused, refused without a clean stop);
- runtime `9e9c6a3a…`, rebuilt by CI from the same pins (Incus 7.5.1);
- controller `5a516bfd…`;
- seat image `1234434c…`.

The owner chose the location (`cache/polyseat/incus`, the existing 150 GB quota) and said "go, zieh durch".

## Setup (one time, owner GO)

`/mnt/cache/polyseat`, the dataset's mount root, went from `nobody:users 0777` back to `root:root 0755`:
- "New Permissions" had reset it before `polyseat` was excluded (see [tower-run15-19-session.md](tower-run15-19-session.md)).
- The dataset identity was checked first, and `steam-seat` was not touched (`1001000:1001000 0700`).

## Runs

| | Run 20 (fresh) | Run 21 (reused) |
|---|---|---|
| Result | **PASS**, `PERSIST_RC=0` | **PASS**, `PERSIST_RC=0` |
| State | workspace created: `/mnt/cache/polyseat/incus` (root 0711), `incus/` (0711), `persist.json` (Incus 7.5.1) | the existing database reopened after its clean-stop record was consumed |
| Seat | pool, image and `polyseat-persist-cpu` created (31 s); the image deleted after `init` | the persisted seat was found **Stopped with `boot.autostart=false`** at daemon start, so nothing started by itself |
| Self-test | desktop PASS (sway, screenshot, Sunshine software encoder) | desktop PASS |
| Stop | seat stopped, `incus admin shutdown`, the daemon exited by itself, `clean-stop.json` written (root 0600) | the same; `clean-stop.used.json` holds the consumed record |
| Size | 990 MB on the dataset (dataset total 2.99 GB with Steam, 147 GB available) | 991 MB |
| After | uninstall RC=0, GPU idle, **`BR0 UNCHANGED`**, `/tmp` kit removed; host diff only the expected plugin paths | the same |

## What stays on Tower by design

`/mnt/cache/polyseat/incus` (about 1 GB) holds the persisted database and seat. Uninstall and the residue removal
before a run never touch it.

To remove it on purpose, make sure no Polyseat `incusd` runs (`pgrep -x incusd` shows nothing), then run
`rm -rf /mnt/cache/polyseat/incus`. The next `persist-smoke` starts fresh.

## Not covered

- a crash on Tower (CI run C proves the refusal);
- an Incus upgrade with persisted state (refused by design until it has its own gate);
- polyseatd;
- the GPU, Steam or a NIC on the persisted seat.
