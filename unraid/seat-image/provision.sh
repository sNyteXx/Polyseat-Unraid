#!/bin/sh
# Runs INSIDE the Docker build container (never on a host). Phase `packages` installs everything from the pinned
# Arch Linux Archive snapshot; phase `finalize` runs after the rootfs overlay is copied in.
set -eu

: "${SEAT_ARCHIVE_DATE:?}" "${SUNSHINE_PKG_URL:?}" "${SUNSHINE_PKG_SHA256:?}" "${SUNSHINE_PKG_VERSION:?}"

PACKAGES='sway swaybg foot waybar grim pipewire pipewire-pulse wireplumber mesa egl-gbm egl-wayland ttf-dejavu jq
  dbus xdg-utils'

packages() {
  # Signature checking stays ON (the distro default SigLevel). Only the mirror is replaced: one dated
  # snapshot of the Arch Linux Archive, so the package set cannot move.
  # shellcheck disable=SC2016  # $repo and $arch are pacman's, not the shell's
  printf 'Server = https://archive.archlinux.org/repos/%s/$repo/os/$arch\n' "$SEAT_ARCHIVE_DATE" > /etc/pacman.d/mirrorlist
  grep -Eq '^SigLevel *= *Required' /etc/pacman.conf || { echo 'unexpected pacman SigLevel' >&2; exit 1; }
  # shellcheck disable=SC2086  # PACKAGES is a word list
  pacman -Syu --noconfirm --needed $PACKAGES

  # Polyseat's patched Sunshine: verified against the pinned sha256, then installed as a local file (pacman
  # applies the Optional LocalFileSigLevel; nobody publishes a .sig). Its dependencies come from the same snapshot.
  curl --fail --silent --show-error --location --retry 5 --output /root/sunshine.pkg.tar.zst "$SUNSHINE_PKG_URL"
  printf '%s  /root/sunshine.pkg.tar.zst\n' "$SUNSHINE_PKG_SHA256" | sha256sum --check --strict -
  pacman -U --noconfirm --needed /root/sunshine.pkg.tar.zst
  rm -f /root/sunshine.pkg.tar.zst
  [ "$(pacman -Q sunshine)" = "sunshine $SUNSHINE_PKG_VERSION" ] || { echo 'sunshine version mismatch' >&2; exit 1; }
}

finalize() {
  # COPY keeps the checkout's file modes (umask dependent); pin them.
  chown root:root /etc/systemd/user/polyseat-*.service /etc/sway/config /usr/share/polyseat/sunshine.conf.in /usr/local/bin/polyseat-*
  chmod 0644 /etc/systemd/user/polyseat-*.service /etc/sway/config /usr/share/polyseat/sunshine.conf.in
  chmod 0755 /usr/local/bin/polyseat-*
  # The seat user: uid/gid 1000, supplementary groups only where they exist.
  groups=''
  for g in video input audio render; do
    if getent group "$g" >/dev/null; then groups="${groups:+$groups,}$g"; fi
  done
  useradd --create-home --uid 1000 --user-group --shell /bin/bash --groups "$groups" player
  [ "$(id -u player)" = 1000 ] && [ "$(id -g player)" = 1000 ]
  # Lingering by file: user@1000.service starts at boot without a login.
  install -d -m 0755 /var/lib/systemd/linger
  : > /var/lib/systemd/linger/player

  # Default profile = cpu. Writes the player's drop-ins and sunshine.conf.
  /usr/local/bin/polyseat-profile cpu

  # Audio plumbing only; the desktop (polyseat-sway) and Sunshine are NEVER enabled: the controller starts them.
  systemctl --global enable pipewire.socket pipewire-pulse.socket wireplumber.service
  # Nothing network-facing, and nothing that only makes sense with a NIC, runs in a seat.
  systemctl mask systemd-networkd.service systemd-networkd.socket systemd-networkd-wait-online.service \
    systemd-resolved.service systemd-timesyncd.service avahi-daemon.service avahi-daemon.socket

  # An empty machine-id is regenerated on first boot.
  : > /etc/machine-id

  # Size and determinism: no package cache, no sync databases (the mirror is pinned and in mirrorlist), no logs.
  pacman -Scc --noconfirm >/dev/null 2>&1 || true
  rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/* /var/log/pacman.log /var/log/journal /root/provision.sh /tmp/* /var/tmp/*
  rm -rf /root/.cache /root/.config
  find /etc/pacman.d/gnupg -name 'S.*' -delete 2>/dev/null || true
}

case "${1:-}" in
  packages) packages ;;
  finalize) finalize ;;
  *) echo 'usage: provision.sh packages|finalize' >&2; exit 2 ;;
esac
