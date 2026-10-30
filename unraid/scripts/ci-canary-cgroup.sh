#!/bin/bash
# Exercise the *same* canary cgroup code on disposable GitHub-hosted Linux.
# No runtime installation and never run on Unraid or ubuntudev.
set -euo pipefail

fail() { printf 'CANARY CGROUP CI FAILED: %s\n' "$*" >&2; exit 2; }
[[ ${CI:-} == true && ${GITHUB_ACTIONS:-} == true &&
   ${RUNNER_ENVIRONMENT:-} == github-hosted && ${RUNNER_OS:-} == Linux &&
   ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ && ! -e /etc/unraid-version && $(id -u) == 0 ]] ||
  fail 'disposable GitHub-hosted runner as root required; Unraid refused'
[[ -n ${GITHUB_WORKSPACE:-} ]] || fail 'workspace missing'

ctl="$GITHUB_WORKSPACE/unraid/plugin/src/emhttp/scripts/polyseatctl"
[[ -f $ctl && ! -L $ctl ]] || fail 'controller missing'
# Strip only the final main invocation; all tested functions are unmodified.
# shellcheck disable=SC1090
source <(sed '$d' "$ctl")
[[ $POLYSEAT_RELEASE_GATE == closed ]] || fail 'committed controller gate was changed'
POLYSEAT_RELEASE_GATE=canary
CANARY_CGROUP="/sys/fs/cgroup/polyseat-unraid-canary-ci-$GITHUB_RUN_ID"
# shellcheck disable=SC2034  # consumed by the sourced controller functions
CANARY_WATCHDOG_CGROUP="/sys/fs/cgroup/polyseat-unraid-canary-watchdog-ci-$GITHUB_RUN_ID"
CANARY_HOST_CPUSET=$(< /sys/fs/cgroup/cpuset.cpus.effective)
CANARY_CPUSET=${CANARY_HOST_CPUSET%%,*}
CANARY_CPUSET=${CANARY_CPUSET%%-*}
[[ $CANARY_CPUSET =~ ^[0-9]+$ ]] || fail 'unable to select a delegated CI CPU'
cfg_get() { printf '%s\n' "$CANARY_DATA_DIR"; }

cleanup() {
  canary_cgroup_stop || fail 'cannot reclaim disposable-runner test cgroup'
}
trap cleanup EXIT

canary_cgroup_prepare || fail 'refused to prepare bounded cgroup'
canary_cgroup_check || fail 'initial cgroup limits invalid'
sleep 30 &
child=$!
printf '%s\n' "$child" > "$CANARY_CGROUP/daemon/cgroup.procs"
grep -q "/polyseat-unraid-canary-ci-$GITHUB_RUN_ID" "/proc/$child/cgroup" ||
  fail 'child process did not enter cgroup'
[[ $(< "$CANARY_CGROUP/cpuset.cpus.effective") == "$CANARY_CPUSET" ]] ||
  fail 'effective CPU set is not the bounded CPU'
[[ -z $(< "$CANARY_CGROUP/cgroup.subtree_control") &&
   -z $(< "$CANARY_CGROUP/daemon/cgroup.controllers") ]] ||
  fail 'canary daemon leaf must not delegate controllers (aggregate limits remain at parent)'
printf '1\n' > "$CANARY_CGROUP/cgroup.kill"
wait "$child" 2>/dev/null || true
[[ $(< "$CANARY_CGROUP/cgroup.events") == *'populated 0'* ]] ||
  fail 'hard stop did not empty cgroup'
printf 'PASS: production canary cgroup functions limited CPU/RAM/swap/pids and killed CI child.\n'
