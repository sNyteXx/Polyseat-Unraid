#!/bin/sh
# Polyseat - build the MangoHud shim package a seat installs.
#
#   packaging/mangohud-shim/build.sh <container>
#
# Runs inside a running Arch container that Incus knows by that name, a seat for
# instance, and leaves the package next to this script. Not on the host, because
# makepkg wants Arch whatever the host is. A minute, most of it pacman.
#
# The compiler and base-devel are taken out again at the end if this script put
# them in: a seat has neither on purpose.
set -eu

ct=${1:?usage: build.sh <container>}
here=$(cd "$(dirname "$0")" && pwd)
dir=/var/tmp/polyseat-mangohud-shim-build

say() { echo "build.sh: $*" >&2; }

# The driver in a seat is a set of files mirrored in from the host and not a
# package. Same flags and same reason as in provision.go.
assume="--assume-installed opengl-driver --assume-installed vulkan-driver
        --assume-installed lib32-opengl-driver --assume-installed lib32-vulkan-driver
        --assume-installed opencl-nvidia"

added=
for p in base-devel gcc; do
    incus exec "$ct" -- pacman -Q "$p" >/dev/null 2>&1 || added="$added $p"
done

say "build dependencies"
# shellcheck disable=SC2086
incus exec "$ct" -- pacman -S --needed --noconfirm $assume \
    base-devel gcc lib32-glibc lib32-gcc-libs mangohud lib32-mangohud

say "recipe into $ct:$dir"
incus exec "$ct" -- sh -c "rm -rf $dir && install -d -o nobody -g nobody $dir"
for f in PKGBUILD shim-rtld-next.patch; do
    incus file push -q "$here/$f" "$ct$dir/$f"
done
incus exec "$ct" -- chown -R nobody:nobody "$dir"

say "building"
# makepkg refuses to run as root. --nodeps because the dependencies are in and
# makepkg would otherwise ask pacman, which trips over the driver again.
incus exec "$ct" -- runuser -u nobody -- env HOME="$dir" PKGEXT=.pkg.tar.zst \
    sh -c "cd $dir && makepkg --nodeps --noconfirm --clean"

pkg=$(incus exec "$ct" -- sh -c "ls $dir/polyseat-mangohud-shim-*.pkg.tar.zst | grep -v -- -debug | head -1")
[ -n "$pkg" ] || { say "makepkg left no package behind"; exit 1; }

incus file pull -q "$ct$pkg" "$here/"
say "$(basename "$pkg")"
sha256sum "$here/$(basename "$pkg")"

if [ -n "$added" ]; then
    say "taking$added out again"
    # shellcheck disable=SC2086
    incus exec "$ct" -- pacman -Rns --noconfirm $added >/dev/null
fi
