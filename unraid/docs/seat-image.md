# Polyseat desktop seat image (WIP, NOT FOR UNRAID YET)

Upstream Polyseat builds each seat *online* at creation time (`pacman -Syu` of ~40 packages, mirror of the day).
That cannot be allowed on Unraid, so this directory builds one **pinned, offline-importable Incus image**, and a
self-test that ships *inside* the image so that CI and a later Unraid canary run byte-identical checks.

Everything lives in `unraid/seat-image/`; the CI proof is `.github/workflows/seat-image.yml`
(`unraid/scripts/ci-desktop-smoke.sh`). The image is a CI artifact named `polyseat-seat-image-NOT-FOR-UNRAID`.

## What is pinned (`unraid/build-pins.env`)

| Pin | Meaning |
| --- | --- |
| `SEAT_BASE_IMAGE` | official Docker `archlinux` image, by manifest-list digest (the builder resolves `linux/amd64`) |
| `SEAT_ARCHIVE_DATE` | Arch Linux Archive snapshot (`https://archive.archlinux.org/repos/YYYY/MM/DD/$repo/os/$arch`) every package comes from; pacman's SigLevel stays `Required` (keyring + signatures ON) |
| `SUNSHINE_PKG_VERSION`, `_URL`, `_SHA256` | Polyseat's patched Sunshine package; same values as `internal/seat/provision.go` (a unit test enforces equality). The builder checks the sha256 before `pacman -U`; its dependencies come from the same snapshot |

`SOURCE_DATE_EPOCH` defaults to 00:00 UTC of the archive day and becomes `creation_date` in `metadata.yaml`.

## Rebuild

```sh
sh unraid/seat-image/build-seat-image.sh        # needs docker + python3; SEAT_IMAGE_OUT=dir to choose the output
```

Output: `polyseat-desktop.tar.xz` (Incus unified image: `metadata.yaml` + `rootfs/`), `seat-image.json` (image sha256, sizes,
base digest, archive date, Sunshine sha256, full `pacman -Q`, build commit), `polyseat-desktop.cdx.json` (CycloneDX package
list) and `polyseat-desktop.tar.xz.sha256`. Import with `incus image import polyseat-desktop.tar.xz --alias polyseat-desktop`
(xz-compressed tarball: no `unsquashfs` needed; `tar` + `xz` on the host).

Reproducibility, honestly: the *package set* is exactly reproducible (pinned snapshot). The tar is normalised (sorted as exported,
mtimes clamped to the epoch, numeric owners, fixed xz settings), but `pacman` records install dates and `ldconfig`/keyring
state are produced at build time, so two builds do **not** give byte-identical archives. Identity is the sha256 of the
published artifact (recorded in `seat-image.json`), not a rebuild.

## Image contents

* Packages: sway, swaybg, foot, waybar, grim, pipewire, pipewire-pulse, wireplumber, mesa, egl-gbm, egl-wayland, ttf-dejavu, jq,
  plus the pinned `sunshine`. No Steam, Firefox, Lutris or Flatpak.
* `player` uid/gid 1000 (video, input, audio, render if present), lingering by `/var/lib/systemd/linger/player`.
* User units `polyseat-sway.service` (`WLR_BACKENDS=headless`, `WLR_RENDERER=pixman`, `WLR_HEADLESS_OUTPUTS=1`,
  `LIBSEAT_BACKEND=noop`, `WLR_LIBINPUT_NO_DEVICES=1`) and `polyseat-sunshine.service` in `/etc/systemd/user`.
  **Neither is enabled**: nothing starts the desktop or anything network-facing on boot; the controller starts them.
  Only the audio sockets (`pipewire.socket`, `pipewire-pulse.socket`, `wireplumber`) are enabled for the user manager.
* Masked system units that need a NIC or the network: `systemd-networkd(-wait-online)`, `systemd-resolved`, `systemd-timesyncd`, `avahi-daemon`.
* `polyseat-profile cpu|nvidia|status`: writes user-unit drop-ins and `sunshine.conf` under the player's home. `cpu` (default):
  `capture = wlr`, `encoder = software`. `nvidia`: `WLR_RENDERER=gles2`, `GBM_BACKEND=nvidia-drm`,
  `__GLX_VENDOR_LIBRARY_NAME=nvidia`, `encoder = nvenc`, plus `/usr/lib/gbm/nvidia-drm_gbm.so -> ../libnvidia-allocator.so.1`
  and `/usr/share/glvnd/egl_vendor.d/10_nvidia.json` exactly as upstream `stepNvidiaUserspace`.
* `polyseat-selftest desktop|nvidia`: see below.

## `polyseat-selftest`

Run as root inside the seat: `incus exec <ct> -- /usr/local/bin/polyseat-selftest desktop`. It waits (≤ 90 s) for
`systemctl is-system-running` (`running`, or `degraded` with the failed units recorded), checks `user@1000.service`, starts
`polyseat-sway.service` as the player, requires **only** `HEADLESS-*` outputs, takes a `grim` screenshot
(`/var/lib/polyseat-selftest/screenshot.png`: PNG magic, width/height, distinct colours ≥ 2), starts Sunshine and extracts the log
lines for the software encoder and wlr capture, checks the container has only `lo` and lists its listening sockets from
`/proc/net/*`, then stops Sunshine and sway. stdout is exactly one JSON object (also `result.json` there); exit 0 only on `PASS`.
No network, no package manager. `nvidia` runs the same flow after `nvidia-smi -L`; without a GPU it fails cleanly
(`verdict: FAIL`, reason `nvidia-gpu: …`) before changing anything.

## CI desktop smoke

`ci-desktop-smoke.sh` refuses to run outside a GitHub-hosted runner. Fixed host-side sequence: `image import`, `init`
(`boot.autostart=false security.privileged=false security.nesting=false limits.cpu=2 limits.memory=2GiB limits.processes=1024`,
root disk only, no NIC), then twice `start` → `exec polyseat-selftest desktop` → `file pull` (screenshot, Sunshine log) →
`stop --timeout 60`, then `delete` and `image delete`. CI-only extra: `selftest nvidia` must fail cleanly. `INCUS_BIN`, `INCUSD_BIN`
and `INCUS_DIR` parametrise the daemon; the iteration runs use Ubuntu's apt `incus`, the pinned 7.5.1 bundle replaces it later.

## Measured CI results

Green run: <https://github.com/sNyteXx/Polyseat-Unraid/actions/runs/41199794061> (`seat-image` + `desktop-smoke`, Ubuntu 24.04 runner,
apt Incus, Docker image build from the pins above). Artifacts: `polyseat-seat-image-NOT-FOR-UNRAID`, `polyseat-desktop-smoke-NOT-FOR-UNRAID`
(screenshots, self-test JSON, Sunshine logs, `measurements.json`).

| What | Value |
| --- | --- |
| image sha256 (that run) | `a52c26c6510127bc2720f3d59c9c92bbd1d4ffe3d311c7e715929ae286460bb2` |
| compressed (`.tar.xz`) | 273,245,376 bytes (260.6 MiB) |
| unpacked, file bytes in the tar | 1,227,792,336 bytes, 346 packages, 35,045 files |
| rootfs as stored by the `dir` pool | 1,247,214,315 bytes apparent, 1,354,018,816 bytes allocated (budget this as RAM when the pool is tmpfs) |
| container `memory.peak` (2 GiB limit) | 139,124,736 / 127,086,592 bytes (cycle 1 / 2: sway + foot + swaybar + pipewire + Sunshine, software x264) |
| container `pids.peak` (1024 limit) | 42 / 42 |
| container CPU time | 3.1 s / 3.6 s per cycle (boot + selftest) |
| `incusd` scope `memory.peak` | 1,803,309,056 bytes - dominated by importing/unpacking the image, not steady state |
| whole-runner used-memory delta peak | 268,226,560 bytes (`MemTotal - MemAvailable` sampled every second; includes incusd, lxc, container) |
| graceful `incus stop` | 2 s / 0 s (no force) |
| `systemctl is-system-running` | `running`, no failed units, both cycles |

Self-test evidence (cycle 1): `HEADLESS-1 1280x720`, `grim` PNG 1280x720 with 198 distinct colours (a foot terminal and the swaybar; screenshot
sha256 `3f43e506…11bb`), Sunshine log lines

```
Info: config: 'capture' = wlr
Info: [wayland] Found interface: zwlr_screencopy_manager_v1(28) version 3
Info: [wlgrab] Monitor 0 is HEADLESS-1: Headless output 1
Info: Screencasting with Wayland's protocol
Info: Trying encoder [software]
Info: Found H.264 encoder: libx264 [software]
```

and listeners `tcp 0.0.0.0:{47984,47989,47990,48010}` in a netns whose only interface is `lo`. Run twice (start, selftest, graceful stop, start,
selftest, graceful stop), then delete, and no process, bridge or cgroup is left. `selftest nvidia` fails cleanly:
`nvidia-gpu: nvidia-smi not found in the seat: no NVIDIA userspace was injected, no GPU`.

Known log noise from Sunshine in this container (not failures of the checks): `Gamepad ... disabled due to gamepads.virtualhid-not-available`
(no `/dev/uinput`, deliberately not provided), `Failed to drop/gain CAP_SYS_ADMIN` (the package's file capability is not usable unprivileged;
wlr capture does not need it), `Failed to create client: Daemon not running` (no avahi, so no mDNS announcement).


## What is NOT proven

* No GPU: the NVIDIA profile (gles2, NVENC, GBM/glvnd fix-ups) is only statically validated; the `nvidia` self-test is proven to fail cleanly, not to pass.
* No Moonlight client has streamed from it: Sunshine's capture/encode *initialisation* is proven, not a paired session, audio, input
  (`/dev/uinput` is not available in the container) or latency.
* Not run on Unraid: kernel, cgroup layout, tmpfs-backed storage pool and the pinned Incus 7.5.1 bundle are exercised by other jobs, not this one.
  The runner kernel and apt's Incus differ from the target.
* Software rendering/encoding speed (2 CPUs) says nothing about gaming performance.
