#!/bin/sh
# CI-only: advisory findings are report-only, scanner failures are fatal.
# Never run this helper on an Unraid host or use it as a release approval.
set -eu

[ "$#" -eq 3 ] || { echo 'usage: ci-govulncheck.sh SCANNER BINARY REPORT' >&2; exit 2; }
scanner=$1
binary=$2
report=$3

[ ! -f /etc/unraid-version ] || { echo 'refusing to scan on Unraid' >&2; exit 1; }
[ -x "$scanner" ] || { echo 'govulncheck scanner is missing' >&2; exit 1; }
[ -f "$binary" ] && [ ! -L "$binary" ] || { echo 'Go binary is missing or a symlink' >&2; exit 1; }

set +e
"$scanner" -mode=binary "$binary" > "$report" 2>&1
scan_status=$?
set -e

# govulncheck uses 3 for found vulnerabilities, 1 for operational errors.
# Both 0 (clean) and 3 (findings requiring triage) are real scan results.
case "$scan_status" in
  0|3) ;;
  *)
    echo "govulncheck failed (exit=$scan_status); refusing to accept the security report" >&2
    sed -n '1,35p' "$report" >&2
    exit 1
    ;;
esac

[ -s "$report" ] || { echo 'govulncheck returned an empty report' >&2; exit 1; }
if grep -Eqi 'unrecognized binary format|could not parse provided binary' "$report"; then
  echo 'govulncheck did not understand this binary' >&2
  exit 1
fi

printf 'govulncheck completed (exit=%s; 3 means findings for review)\n' "$scan_status"
