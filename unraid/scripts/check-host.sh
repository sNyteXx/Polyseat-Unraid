#!/bin/sh
# WIP DO NOT USE. Read-only checks only.
set -u
printf '%s\n' 'WIP DO NOT USE — host capability checks'
for p in /sys/fs/cgroup/cgroup.controllers /proc/sys/kernel/unprivileged_userns_clone /proc/sys/user/max_user_namespaces /etc/subuid /etc/subgid /dev/net/tun /dev/uinput /dev/nvidiactl /dev/nvidia-uvm; do
  if [ -e "$p" ]; then printf 'present: %s\n' "$p"; else printf 'missing: %s\n' "$p"; fi
done
printf 'cgroup controllers: '; cat /sys/fs/cgroup/cgroup.controllers 2>/dev/null || true
printf 'user namespaces max: '; cat /proc/sys/user/max_user_namespaces 2>/dev/null || true
printf 'overlay filesystem: '; grep -w overlay /proc/filesystems || true
printf 'idmapped mounts: kernel and filesystem dependent; requires separate functional test\n'
printf 'No changes made.\n'
