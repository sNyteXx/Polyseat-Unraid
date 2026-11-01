#!/bin/bash
# Stub polyseatctl for UI tests: records argument vectors (unit-separator joined), answers from stub.conf.
dir=$(dirname "$0")
conf="$dir/stub.conf"
(IFS=$'\x1f'; printf '%s\n' "$*") >> "$dir/calls.log"
get() { sed -n "s/^$1=//p" "$conf" | head -n1; }
gate=$(get gate); sleep_s=$(get sleep); fail=$(get fail); mode=$(get mode)
[[ -n $sleep_s ]] && sleep "$sleep_s"
case "$1" in
  status) printf '{"state":"RUNNING","release_gate":"%s","approval_mode":"%s","version":"1.0","current":"","previous":"","controller":"t","data_dir":""}\n' "${gate:-closed}" "${mode:-signed}"; exit 0 ;;
  metrics) echo '{"load":[0.1,0.2,0.3],"cpus":4,"memory":{"total_kb":100,"available_kb":50},"daemon":{"running":false},"data_dir":null,"incus":null}'; exit 0 ;;
  list) echo '[]'; exit 0 ;;
  config-get) echo '[]'; exit 0 ;;
  logs) echo "line"; exit 0 ;;
  polyseatd-status)
    case "$(get play)" in
      up) echo '{"ok":true,"url":"https://192.168.178.24:47800","alive":true,"pid":"4242","listeners":["x"],"session":{"setup":false,"note":"<script>"},"seats":[{"name":"seat1","status":"Running","autostart":"false"},{"name":"Bad Name","status":"Running"},{"name":"seat2","status":"Stopped"}],"expires_at":2000000000}'; exit 0 ;;
      evil) echo '{"url":"javascript:alert(1)","alive":true,"seats":"x","expires_at":"soon"}'; exit 0 ;;
      broken) exit 1 ;;
      idle) echo '{"state":"idle","last_end":{"reason":"memory-floor","clean":true,"recovered":false,"ended_at":1700000000,"evil":"<x>"}}'; echo "no polyseatd session is running" >&2; exit 2 ;;
      busy) echo '{"state":"busy"}'; echo "no polyseatd session is running" >&2; exit 2 ;;
      recover) echo '{"state":"needs-recovery","reason":"memory-floor-hard","last_end":null}'; echo "no polyseatd session is running" >&2; exit 2 ;;
      recover-evil) echo '{"state":"needs-recovery","reason":"<script>alert(1)</script>","last_end":{"reason":"A B","clean":"yes","ended_at":1}}'; exit 2 ;;
      bogus-state) echo '{"state":"exploding"}'; exit 2 ;;
      *) echo "no polyseatd session is running" >&2; exit 2 ;;
    esac ;;
esac
if [[ -n $fail && $fail != 0 ]]; then get stderr | base64 -d >&2; exit "$fail"; fi
echo "done" >&2
exit 0
