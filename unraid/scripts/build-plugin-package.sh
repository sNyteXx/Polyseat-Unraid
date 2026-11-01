#!/bin/sh
# WIP / DO NOT USE. Builds the controller/WebUI/event package as a deterministic
# Slackware-style .txz. Build host only: refuses on Unraid and never installs anything.
# usage: build-plugin-package.sh VERSION OUTDIR GATE   (GATE: closed | test-host)
set -eu

die() { printf 'BLOCKED: %s\n' "$1" >&2; exit 2; }

[ ! -e /etc/unraid-version ] || die 'never run the package build on Unraid'
[ "$#" -eq 3 ] || die 'usage: build-plugin-package.sh VERSION OUTDIR GATE'
version=$1
outdir=$2
gate=$3

case "$version" in
  [0-9][0-9][0-9][0-9].[0-9][0-9].[0-9][0-9]*) ;;
  *) die 'VERSION must look like YYYY.MM.DD[suffix]' ;;
esac
case "$version" in
  *[!0-9A-Za-z.]*) die 'VERSION may contain only digits, letters and dots' ;;
esac
case "$gate" in
  closed | test-host) ;;
  *) die 'GATE must be closed or test-host; "open" is reserved for a separately approved release job' ;;
esac
if [ ! -d "$outdir" ] || [ -L "$outdir" ]; then die 'OUTDIR must be an existing real directory'; fi
# The archive is written after changing into the staging directory, so the path must be absolute.
outdir=$(cd "$outdir" && pwd -P)

here=$(cd "$(dirname "$0")" && pwd)
src="$here/../plugin/src/emhttp"
[ -f "$src/scripts/polyseatctl" ] || die 'plugin sources not found'

package="polyseat-unraid-$version-noarch-1.txz"
[ ! -e "$outdir/$package" ] || die "refusing to overwrite $package"

stage=$(mktemp -d "${TMPDIR:-/tmp}/polyseat-pkg.XXXXXXXX")
case "$stage" in
  */polyseat-pkg.*) ;;
  *) die 'unexpected staging directory' ;;
esac
dest="$stage/usr/local/emhttp/plugins/polyseat-unraid"
mkdir -p "$dest" "$stage/install"
cp -R "$src/." "$dest/"

ctl="$dest/scripts/polyseatctl"
[ "$(grep -c '^POLYSEAT_RELEASE_GATE="closed"' "$ctl")" -eq 1 ] || die 'release gate line not found exactly once'
sed -i "s/^POLYSEAT_RELEASE_GATE=\"closed\"/POLYSEAT_RELEASE_GATE=\"$gate\"/" "$ctl"
grep -q "^POLYSEAT_RELEASE_GATE=\"$gate\"" "$ctl" || die 'could not set the release gate'
# Optional (CI): pin the exact polyseat daemon bundle (polyseat-daemon.tar) built in the same run. Without a pin the controller
# refuses to stage or run any daemon.
if [ -n "${POLYSEATD_SHA256:-}" ]; then
  printf '%s' "$POLYSEATD_SHA256" | grep -Eqx '[0-9a-f]{64}' || die 'POLYSEATD_SHA256 must be 64 lowercase hex digits'
  [ "$(grep -c '^POLYSEATD_SHA256=""$' "$ctl")" -eq 1 ] || die 'polyseatd pin line not found exactly once'
  sed -i "s/^POLYSEATD_SHA256=\"\"$/POLYSEATD_SHA256=\"$POLYSEATD_SHA256\"/" "$ctl"
  grep -q "^POLYSEATD_SHA256=\"$POLYSEATD_SHA256\"$" "$ctl" || die 'could not pin polyseatd'
fi

# Normalize every shipped directory, including install/usr/local ancestors.
# The outer mktemp directory stays private (0700); it is not part of the archive.
# Caller umasks such as 077 (CI) or 002 (shared development) must not leak into it.
find "$stage" -mindepth 1 -type d -exec chmod 0755 {} +
find "$dest" -type f -exec chmod 0644 {} +
chmod 0755 "$dest/scripts/polyseatctl" "$dest/event/disks_mounted" "$dest/event/stopping_svcs"
printf '%s\n' \
  'polyseat-unraid: Polyseat/Incus runtime controller (WIP, gated)' \
  'polyseat-unraid:' \
  'polyseat-unraid: Experimental. Not for production use.' > "$stage/install/slack-desc"
chmod 0644 "$stage/install/slack-desc"

(
  cd "$stage"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' --format=gnu \
    -cf - install usr | xz -9e -T1 > "$outdir/$package"
)
# Remove only the staging tree created above, after the path guard.
rm -rf "$stage"

sha256sum -- "$outdir/$package" | awk '{print $1 "  '"$package"'"}'
