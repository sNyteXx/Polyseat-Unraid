#!/bin/sh
# WIP DO NOT USE: read-only runtime verification; no installation.
set -eu
printf '%s\n' 'WIP DO NOT USE: Polyseat/Incus runtime verification'
if command -v incus >/dev/null 2>&1; then
  incus version || true
else
  echo 'BLOCKED: Incus is not installed; no automatic installation attempted.'
fi
for p in /sys/fs/cgroup/cgroup.controllers /dev/net/tun /dev/nvidiactl /dev/nvidia-uvm; do
  if [ -e "$p" ]; then echo "PRESENT $p"; else echo "MISSING $p"; fi
done
printf '%s\n' 'No services started and no host configuration changed.'
