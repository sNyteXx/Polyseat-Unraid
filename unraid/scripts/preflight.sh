#!/bin/sh
# WIP — DO NOT USE FOR INSTALLATION. Read-only diagnostic only.
set -u
printf '%s\n' 'WIP / DO NOT USE — Polyseat Unraid read-only preflight' '========================================================='
printf 'Kernel: '; uname -r
printf 'OS: '; sed -n '1,4p' /etc/os-release 2>/dev/null || true
printf 'Cgroup fs: '; stat -fc %T /sys/fs/cgroup 2>/dev/null || true
for ns in user net pid mnt; do
  if [ -e "/proc/self/ns/$ns" ]; then printf 'Namespace %s: available\n' "$ns"; else printf 'Namespace %s: missing\n' "$ns"; fi
done
for bin in incus incusd newuidmap newgidmap nvidia-smi; do
  if command -v "$bin" >/dev/null 2>&1; then printf '%s: present\n' "$bin"; else printf '%s: absent\n' "$bin"; fi
done
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null || true; fi
printf '\nMounts (read-only listing):\n'; mount | grep -E ' on /mnt/(cache|user|disk|pool)' | head -25 || true
printf '\nNetwork links (read-only listing):\n'; if command -v ip >/dev/null 2>&1; then ip -brief link show 2>/dev/null || true; fi
printf '\nExisting runtime processes:\n'; if command -v pgrep >/dev/null 2>&1; then pgrep -a -x incusd || true; pgrep -a -x dockerd || true; pgrep -a -x libvirtd || true; pgrep -a -x polyseatd || true; fi
printf '\nRESULT: DIAGNOSTICS ONLY; installation intentionally blocked.\n'
