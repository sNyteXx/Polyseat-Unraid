# Tower runs 13 and 14 — Steam on persistent cache storage, 2026-10-31: PASS, persistence proven

Candidate `25125ef` (full lane 41244866396, 13/13). Its CI rehearsal: "Steam's files are on the persistent storage
(2.5G, owned by 1001000)", bridge MAC unchanged. Runtime `b59e55cb…` (verified reuse), controller `92c7b259…`, seat
image `b4a91c61…`. Storage `cache/polyseat` (quota 150G), created on the owner's word "on the cache" (see
[steam.md](steam.md)). Both runs used the same qualified kit, each with its own token, residue removal and rollback.

| | Run 13 (first start) | Run 14 (second start) |
|---|---|---|
| Result | **PASS**, `STEAM_RC=0` | **PASS**, `STEAM_RC=0` |
| steam-storage | `cache/polyseat[/steam-seat] zfs`, 150G free, **run 1** | same mount, 149G free, **run 2** |
| Steam | bootstrapped, **downloaded its 424 MB update** to the cache, sign-in window after 98 s | **no download**, sign-in window after **16 s** |
| Used on `cache/polyseat` | 1.69 GB (ZFS-compressed) | 1.69 GB (unchanged) |
| Canary group memory peak | **3.54 GB** (7.64 GB with RAM-only data in run 12) | 3.14 GB |
| GL 64/32 bit, Vulkan (vkcube), NVENC | PASS on the RTX 5070 Ti | PASS |
| br0 | `BR0 UNCHANGED` | `BR0 UNCHANGED` |
| Host diff | expected plugin paths; the known transient UDP count (61 → 60); links and addresses identical | expected plugin paths only |

Login, client and library now live on the cache and survive restarts. Signing in (by the owner, through Moonlight) is
therefore remembered by Steam's own "Remember me".
