# shellcheck shell=bash
# Sourced by the CI canary rehearsals (disposable GitHub-hosted runners only; never on a host).
#
# Azure Accelerated Networking: hv_netvsc hot-plugs an SR-IOV VF under eth0 at some point after the runner boots.
# With a reused runtime the CPU canary starts about a minute into the job and run 41220919953 saw `enP23362s1`
# appear between its before and after snapshots ("unscoped host change: addresses"). The VF is runner noise, not a
# change by the canary, so the host snapshots leave it out. Only an exact match is excluded: name enP<n>s<n>,
# enslaved to eth0 (sysfs `master`), driver mlx4_core, mlx5_core or mana. Anything else still fails the check, and
# every snapshot records which interfaces were left out.

runner_vf_names() { # [sysfs net root] -> JSON array of interface names
  local root=${1:-/sys/class/net} d n drv
  for d in "$root"/enP*; do
    [[ -e $d ]] || continue
    n=${d##*/}
    [[ $n =~ ^enP[0-9]+s[0-9]+$ ]] || continue
    [[ $(readlink -- "$d/master" 2>/dev/null) =~ (^|/)eth0$ ]] || continue
    drv=$(readlink -- "$d/device/driver" 2>/dev/null) || continue
    case ${drv##*/} in mlx4_core | mlx5_core | mana) printf '%s\n' "$n" ;; esac
  done | jq -R . | jq -cs .
}
