#!/bin/sh
# Runs INSIDE the Docker build container (never on a host). Phase `packages` installs everything from the pinned
# Arch Linux Archive snapshot; phase `finalize` runs after the rootfs overlay is copied in.
set -eu

: "${SEAT_ARCHIVE_DATE:?}" "${SUNSHINE_PKG_URL:?}" "${SUNSHINE_PKG_SHA256:?}" "${SUNSHINE_PKG_VERSION:?}"

PACKAGES='sway swaybg foot waybar grim xorg-xwayland pipewire pipewire-pulse wireplumber mesa egl-gbm egl-wayland
  ttf-dejavu jq dbus xdg-utils'
# Steam and the 32 bit userspace its client and most games need (multilib), as upstream's stepSteam installs it.
# The GPU's own GL/Vulkan driver is never a package here: on NVIDIA it is injected at start (nvidia.runtime with
# compat32), so the Vulkan driver dependencies are assumed installed. mesa and lib32-mesa stay as the software path
# the rehearsals use; they share no file with the injected NVIDIA libraries.
STEAM_PACKAGES='steam lib32-libglvnd lib32-mesa lib32-vulkan-icd-loader ttf-liberation zenity mesa-utils lib32-mesa-utils
  vulkan-tools'
STEAM_ASSUME='--assume-installed vulkan-driver --assume-installed lib32-vulkan-driver'

packages() {
  # Signature checking stays ON (the distro default SigLevel). Only the mirror is replaced: one dated
  # snapshot of the Arch Linux Archive, so the package set cannot move.
  # shellcheck disable=SC2016  # $repo and $arch are pacman's, not the shell's
  printf 'Server = https://archive.archlinux.org/repos/%s/$repo/os/$arch\n' "$SEAT_ARCHIVE_DATE" > /etc/pacman.d/mirrorlist
  grep -Eq '^SigLevel *= *Required' /etc/pacman.conf || { echo 'unexpected pacman SigLevel' >&2; exit 1; }
  # multilib from the same dated snapshot (the mirrorlist's $repo).
  grep -q '^\[multilib\]' /etc/pacman.conf || printf '\n[multilib]\nInclude = /etc/pacman.d/mirrorlist\n' >> /etc/pacman.conf
  # shellcheck disable=SC2086  # word lists
  pacman -Syu --noconfirm --needed $STEAM_ASSUME $PACKAGES $STEAM_PACKAGES
  # Steam refuses a non-UTF-8 locale.
  sed -i 's/^#en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
  locale-gen
  printf 'LANG=en_US.UTF-8\n' > /etc/locale.conf

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
  chown root:root /etc/systemd/user/polyseat-*.service /etc/sway/config /usr/share/polyseat/sunshine.conf.in /usr/local/bin/polyseat-* \
    /usr/local/lib/polyseat/netns-classify /etc/systemd/network/20-polyseat-lan.network
  chmod 0644 /etc/systemd/user/polyseat-*.service /etc/sway/config /usr/share/polyseat/sunshine.conf.in \
    /etc/systemd/network/20-polyseat-lan.network
  chmod 0755 /usr/local/bin/polyseat-* /usr/local/lib/polyseat/netns-classify
  # The seat user: uid/gid 1000, supplementary groups only where they exist.
  groups=''
  for g in video input audio render; do
    if getent group "$g" >/dev/null; then groups="${groups:+$groups,}$g"; fi
  done
  useradd --create-home --uid 1000 --user-group --shell /bin/bash --groups "$groups" player
  [ "$(id -u player)" = 1000 ] && [ "$(id -g player)" = 1000 ]
  # Steam's home (~/.local/share/Steam) is the persistent mount point for the Steam seat; it must exist with the
  # player as owner, or Incus creates the missing parents as root and ~/.local/share becomes unwritable.
  install -d -o player -g player -m 0755 /home/player/.local /home/player/.local/share
  install -d -o player -g player -m 0700 /home/player/.local/share/Steam
  # Lingering by file: user@1000.service starts at boot without a login.
  install -d -m 0755 /var/lib/systemd/linger
  : > /var/lib/systemd/linger/player

  # Default profile = cpu. Writes the player's drop-ins and sunshine.conf.
  /usr/local/bin/polyseat-profile cpu

  # Audio plumbing only; the desktop (polyseat-sway) and Sunshine are NEVER enabled: the controller starts them.
  systemctl --global enable pipewire.socket pipewire-pulse.socket wireplumber.service
  # Nothing network-facing, and nothing that only makes sense with a NIC, runs in a seat. Only the LAN self-test
  # (polyseat-selftest steam*, a seat that was given eth0) unmasks networkd and resolved in its own running copy.
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
