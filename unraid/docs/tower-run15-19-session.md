# Tower runs 15–19 — M3 Moonlight LAN session, 2026-11-01: PASS (run 19)

Candidate `2357728` (full lane 41250611154, 13/13). Its CI rehearsal proved the session plumbing on the fixture
bridge: start, the PIN path to Sunshine's API, the refusal of a malformed PIN, and a stop that leaves nothing.
Runtime `b59e55cb…` (verified reuse), controller `641bec44…`, seat image `bd3e8d0f…`. Run under the owner's M3
decision (see [steam.md](steam.md)). Each run had its own token, residue removal and rollback.

| Run | Result | Why |
|---|---|---|
| 15 (2026-10-31 23:24Z) | refused before start, rollback clean | host load 26 > 14: four Jellyfin `ffmpeg` processes at nice 10. The watchdog also checks load during a session, so the run waited for low load (read-only check every 60 s) |
| 16 (01:06Z) | session up 23 min, **nobody paired** | `192.168.178.153`, `h264_nvenc`, Steam up; the owner was not available at night. Ended on token time; teardown clean |
| 17 (07:55Z) | refused before start, rollback clean | the owner's weekly user script "New Permissions" (Fix Common Problems `newperms.php`) had set `/mnt/cache/polyseat` to `nobody:users` 777/666 at 03:31Z. The storage check refused foreign-owned storage |
| 18 (08:04Z) | stopped at W5 by the run driver, rollback clean | staging had worked; the driver read only the last output line, and SSH delivered stdout and stderr in the other order. The driver now captures the whole output and looks for the exact line |
| **19 (08:11Z)** | **PASS**, `SESSION_RC=0` | below |

## Run 19

| Step (UTC) | Result |
|---|---|
| 08:15:50 | session up at `192.168.178.153` (the same address as run 16: DHCP keyed to the MAC), `h264_nvenc`, Steam up |
| 08:20:47 | the owner relayed Moonlight's PIN from the Tab; `steam-session-pin` → `{"ok":true,"paired":true}`; one paired client |
| 08:23 | app "Desktop" streaming: one NVENC session at **59 fps**, GPU P1 at 7 % and about 49 W. The owner signed in to Steam by QR code with the Steam Mobile app; one account in `loginusers.vdf` |
| 08:25:06 | `steam-session-stop`; `polyseat-session stop` → `{"ok":true,"stopped":true}`; seat stop, GPU and veth released, delete, daemon stop |
| after | uninstall RC=0; GPU on `nvidia`, idle; **`BR0 UNCHANGED`**; `/tmp` kit removed exactly. The Steam login stays on `cache/polyseat` (owner `1001000`, mode 0700) |

Resources: the canary group peaked at 4.2 GB of 8 GiB. The lowest host MemAvailable was 16.7 GiB and the host load
stayed at about 5. The host diff showed only the expected plugin paths and a mount count that moved with the host's
own backup and Docker activity (no Polyseat, Incus or LXC mount).

As designed, touch and the controller did nothing: Sunshine's keyboard, mouse and controller are disabled (see
[steam.md](steam.md)).

## Host changes the owner approved during these runs

- `/mnt/cache/polyseat/steam-seat` was restored to its setup state. Every entry (29,406) went back to
  `1001000:1001000` with `find -xdev … chown -h`, and the directory to 0700.
- Fix Common Problems: `excludedPerms` was `""` and is now `"polyseat"` in
  `/boot/config/plugins/fix.common.problems/settings.json`. Exactly one line changed. The original is next to it
  as `settings.json.pre-polyseat-exclude-20261101T0804Z`. "New Permissions" now skips the `polyseat` folder on
  every disk and pool.

## Not covered

- input from the client (touch, gamepad, keyboard);
- games and Proton;
- Big Picture and gamescope;
- two seats;
- the upstream daemon.
