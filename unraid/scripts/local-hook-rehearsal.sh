#!/bin/bash
# Level 2 (local, ~1 min): real bundled incusd + liblxc call the Polyseat nvidia hook wrapper and the verbatim
# upstream hook exactly as LXC does on a host: as the container's root inside its user namespace, with LXC's
# environment, during a real container start. A stub nvidia-container-cli replaces NVIDIA's (no GPU, no driver).
#   local-hook-rehearsal.sh BUNDLE.tar.gz OUT_DIR [REAL_CLI_DIR]
# REAL_CLI_DIR (optional): NVIDIA's libnvidia-container-tools + libnvidia-container1 1.20.1 .debs extracted with
# dpkg-deb -x (verified by sha256, never installed on the developer machine); adds case D with the real CLI.
# Runs ONLY inside a disposable, privileged, network-less Docker container on a developer machine or CI.
# What this cannot show: the real nvidia-container-cli talking to an NVIDIA driver (NVML, driver RPC, driver file
# discovery via ldcache, bind-mounting driver libraries and device nodes, ldconfig in the rootfs) on the Unraid
# kernel. That step is only reproducible on Tower with the RTX.
set -euo pipefail
[[ ! -e /etc/unraid-version ]] || { echo 'never on Unraid' >&2; exit 2; }
bundle=$(readlink -f "$1") out=$(readlink -f "$2") real=${3:+$(readlink -f "$3")}
repo=$(cd "$(dirname "$0")/../.." && pwd)
command -v docker >/dev/null || { echo 'docker required' >&2; exit 2; }
[[ -f $bundle && $(basename "$bundle" .tar.gz) == "$(sha256sum "$bundle" | cut -d' ' -f1)" ]] ||
  { echo 'bundle must be <sha256>.tar.gz with a matching hash' >&2; exit 2; }
mkdir -p "$out"
tag=polyseat-hook-rehearsal:$(sha256sum "$repo/unraid/scripts/dev/hook-rehearsal.Dockerfile" | cut -c1-12)
docker image inspect "$tag" >/dev/null 2>&1 ||
  docker build -q -t "$tag" -f "$repo/unraid/scripts/dev/hook-rehearsal.Dockerfile" "$repo/unraid/scripts/dev" >/dev/null
started=$SECONDS
docker run --rm --privileged --cgroupns=private --network none --memory 2g --pids-limit 4096 \
  --tmpfs /var/lib/incus-test:exec,size=1500m --tmpfs /tmp:exec,size=256m \
  -v "$bundle:/in/bundle.tar.gz:ro" -v "$repo/unraid/plugin/src/emhttp:/in/emhttp:ro" \
  -v "$repo/unraid/scripts/local-hook-rehearsal-inner.sh:/in/inner.sh:ro" -v "$out:/out" \
  ${real:+-v "$real:/realcli:ro"} \
  "$tag" bash /in/inner.sh
printf "local_hook_rehearsal_seconds=%s\n" "$((SECONDS - started))" | tee "$out/timing.txt"
