#!/bin/bash
# WIP / DO NOT USE. Builds polyseat-daemon.tar: upstream polyseatd, upstream's input helpers and udev rule, a bundled
# Python for them, the console guard. Deterministic tar with a MANIFEST of every file's SHA-256; the canary controller
# pins the tar's hash. Build host only, never on Unraid. Needs Go, curl, sha256sum, GNU tar.
# Layout: bin/ (polyseatd, polyseat-console, bpftrace stand-in), lib/polyseat/ (helpers), py/ (Python), rules/, etc/.
# usage: build-daemon-bundle.sh VERSION OUTDIR
set -euo pipefail
[[ ! -e /etc/unraid-version ]] || { echo 'never build on Unraid' >&2; exit 2; }
[[ $# -eq 2 && $1 =~ ^[0-9A-Za-z.+-]+$ && -d $2 ]] || { echo 'usage: build-daemon-bundle.sh VERSION OUTDIR' >&2; exit 2; }
version=$1 out=$(cd "$2" && pwd -P)
repo=$(cd "$(dirname "$0")/../.." && pwd)
# shellcheck source=unraid/daemon/pins.env
. "$repo/unraid/daemon/pins.env"
[[ $PYTHON_STANDALONE_SHA256 =~ ^[0-9a-f]{64}$ ]] || { echo 'bad Python pin' >&2; exit 2; }
work=$(mktemp -d "${TMPDIR:-/tmp}/polyseat-daemon.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
b="$work/bundle"
mkdir -p "$b/bin" "$b/lib/polyseat" "$b/rules" "$b/etc"
export CGO_ENABLED=0 GOFLAGS=-mod=readonly
(cd "$repo" &&
  go build -trimpath -ldflags "-s -w -buildid= -X github.com/superuser404notfound/Polyseat/internal/version.Version=$version" \
    -o "$b/bin/polyseatd" ./cmd/polyseatd &&
  go build -trimpath -ldflags "-s -w -buildid=" -o "$b/bin/polyseat-console" ./unraid/cmd/polyseat-console &&
  go build -trimpath -ldflags "-s -w -buildid=" -o "$out/polyseat-passwd" ./unraid/cmd/polyseat-passwd)
install -m 0755 "$repo/unraid/daemon/bpftrace-unavailable" "$b/bin/bpftrace"
for f in broker.py device_owner.py uhid_observer.py fakeudev.py; do
  install -m 0755 "$repo/spike/m2-input-broker/$f" "$b/lib/polyseat/$f"
done
# udev runs device_owner.py by its fixed path; on Unraid that copy needs this bundle's interpreter (no python3 there).
{ printf '#!@BUNDLE@/py/bin/python3 -I\n'; tail -n +2 "$repo/spike/m2-input-broker/device_owner.py"; } > "$b/lib/polyseat/device_owner.udev"
chmod 0644 "$b/lib/polyseat/device_owner.udev"
install -m 0644 "$repo/host/72-polyseat-hide.rules" "$b/rules/72-polyseat-hide.rules"
# The daemon's fixed settings on Unraid; the controller fills in @BUNDLE@, @LISTEN@, @STATE@, @LIBRARY@ and @UPLINK@.
# Host actions stay off (prepare, update, restart, uninstall, LAN bridge answer 403); the helpers use this Python.
cat > "$b/etc/polyseatd.json.in" <<'JSON'
{"listen": "@LISTEN@", "state_dir": "@STATE@", "library_dir": "@LIBRARY@", "uplink": "@UPLINK@",
 "helper_dir": "@BUNDLE@/lib/polyseat", "python": "@BUNDLE@/py/bin/python3",
 "update_check": false, "web_update": false, "web_uninstall": false, "web_lan_bridge": false}
JSON
curl -fsSL --retry 3 -o "$work/python.tar.gz" "$PYTHON_STANDALONE_URL"
printf '%s  %s\n' "$PYTHON_STANDALONE_SHA256" "$work/python.tar.gz" | sha256sum --check --strict --quiet
tar -xzf "$work/python.tar.gz" -C "$work" --no-same-owner
mv -- "$work/python" "$b/py"
[[ -x $b/py/bin/python3 ]] || { echo 'python3 missing in the standalone build' >&2; exit 2; }
# The helpers use the standard library only: no terminal database, tests, IDLE or Tk. What remains has no symlink
# leaving its own directory, which daemon-stage requires.
rm -rf "$b/py/share/terminfo" "$b/py/lib/python3.12/test" "$b/py/lib/python3.12/idlelib" \
  "$b/py/lib/python3.12/tkinter" "$b/py/lib/python3.12/turtledemo" "$b/py/lib/python3.12/ensurepip" \
  "$b/py/lib/tcl"* "$b/py/lib/tk"* "$b/py/lib/itcl"* "$b/py/lib/thread"* \
  "$b/py/lib/python3.12/lib-dynload/_tkinter"*
if find "$b" -type l -lname '/*' -o -type l -lname '*..*' | grep -q .; then
  echo 'a symlink leaves its directory' >&2; exit 2
fi
"$b/py/bin/python3" -I -c 'import ctypes, json, os, re, shutil, signal, socket, stat, struct, subprocess, threading' ||
  { echo 'the bundled Python lacks a module the helpers import' >&2; exit 2; }
find "$b" -type d -exec chmod 0755 {} +
# Written beside the tree and moved in, so that the manifest does not list itself.
(cd "$b" && find . -type f -printf '%P\n' | LC_ALL=C sort | while IFS= read -r f; do sha256sum -- "$f"; done) > "$work/MANIFEST"
mv -- "$work/MANIFEST" "$b/MANIFEST"
chmod 0644 "$b/MANIFEST"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime=@0 --format=gnu -C "$b" -cf "$out/polyseat-daemon.tar" \
  MANIFEST bin etc lib py rules
(cd "$out" && sha256sum polyseat-daemon.tar polyseat-passwd > SHA256SUMS && printf '%s\n' "$version" > version.txt)
cat "$out/SHA256SUMS"
