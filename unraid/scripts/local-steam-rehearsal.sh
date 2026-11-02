#!/bin/bash
# Steam rehearsal (local or CI, ~5-15 min): the real bundled incusd starts the real seat image with exactly the
# controller's LAN NIC (bridged on br0, fixed MAC) and runs `polyseat-selftest steam-cpu`: DHCP, DNS, Xwayland,
# 64 and 32 bit GL (software), Steam's bootstrap and self-update from Valve over the internet, its sign-in window.
#   local-steam-rehearsal.sh BUNDLE.tar.gz SEAT_IMAGE.tar.xz OUT_DIR
# br0 here is a fixture inside a disposable, privileged Docker container: a bridge with dnsmasq (DHCP, DNS) and NAT
# to the container's own uplink, standing in for Tower's br0 and the LAN router. Nobody signs in to Steam.
# What this cannot show: NVIDIA (the GPU, compat32 injection, NVENC) and Tower's real LAN; those are Tower-only.
set -euo pipefail
[[ ! -e /etc/unraid-version ]] || { echo 'never on Unraid' >&2; exit 2; }
bundle=$(readlink -f "$1") seat=$(readlink -f "$2") out=$(readlink -f "$3")
repo=$(cd "$(dirname "$0")/../.." && pwd)
command -v docker >/dev/null || { echo 'docker required' >&2; exit 2; }
[[ -f $bundle && $(basename "$bundle" .tar.gz) == "$(sha256sum "$bundle" | cut -d' ' -f1)" ]] ||
  { echo 'bundle must be <sha256>.tar.gz with a matching hash' >&2; exit 2; }
[[ -f $seat ]] || { echo 'seat image missing' >&2; exit 2; }
mkdir -p "$out"
tag=polyseat-hook-rehearsal:$(sha256sum "$repo/unraid/scripts/dev/hook-rehearsal.Dockerfile" | cut -c1-12)
docker image inspect "$tag" >/dev/null 2>&1 ||
  docker build -q -t "$tag" -f "$repo/unraid/scripts/dev/hook-rehearsal.Dockerfile" "$repo/unraid/scripts/dev" >/dev/null
started=$SECONDS
name=polyseat-steam-rehearsal-$$
# Peak memory of the whole rehearsal container (daemon, seat, its RAM rootfs and Steam's files): what the canary
# group on Tower would carry. Sampled from outside every 5 s.
( peak=0 hard=0
  while sleep 5; do
    cur=$(docker stats --no-stream --format '{{.MemUsage}}' "$name" 2>/dev/null | awk '{print $1}') || continue
    [[ -n $cur ]] || { docker inspect "$name" >/dev/null 2>&1 && continue; break; }
    b=$(numfmt --from=iec-i --suffix=B --invalid=ignore <<< "$cur" 2>/dev/null | tr -dc 0-9)
    [[ -n $b ]] && (( b > peak )) && peak=$b
    # anon + shmem (tmpfs: the RAM rootfs and Steam's files) cannot be reclaimed without swap: Tower's hard part.
    id=$(docker inspect --format '{{.Id}}' "$name" 2>/dev/null) || continue
    stat=$(cat "/sys/fs/cgroup/system.slice/docker-$id.scope/memory.stat" "/sys/fs/cgroup/docker/$id/memory.stat" 2>/dev/null) || true
    h=$(awk '$1 == "anon" || $1 == "shmem" {s += $2} END {print s + 0}' <<< "$stat")
    (( h > hard )) && hard=$h
    printf 'container_memory_peak_bytes=%s\nunreclaimable_anon_shmem_peak_bytes=%s\n' "$peak" "$hard" > "$out/memory-peak.txt"
  done ) &
sampler=$!
trap 'kill "$sampler" 2>/dev/null || true' EXIT
docker run --rm --name "$name" --privileged --cgroupns=private --memory 10g --pids-limit 16384 \
  --tmpfs /var/lib/incus-test:exec,size=8g --tmpfs /tmp:exec,size=512m \
  -v "$bundle:/in/bundle.tar.gz:ro" -v "$seat:/in/seat.tar.xz:ro" -v "$repo/unraid/plugin/src/emhttp:/in/emhttp:ro" \
  -v "$repo/unraid/scripts/local-steam-rehearsal-inner.sh:/in/inner.sh:ro" -v "$out:/out" \
  "$tag" bash /in/inner.sh
printf "local_steam_rehearsal_seconds=%s\n" "$((SECONDS - started))" | tee "$out/timing.txt"
