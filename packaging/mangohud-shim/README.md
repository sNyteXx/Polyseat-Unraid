# MangoHud's shim, as a seat gets it

A seat runs the distribution's MangoHud, and one file of it from here instead:
the OpenGL shim, `libMangoHud_shim.so`, which is what carries the framerate cap
into an OpenGL game. It is built from MangoHud's 0.8.4 release with one patch,
for both word lengths, as a package of its own:

    polyseat-mangohud-shim-0.8.4-1-x86_64.pkg.tar.zst

It installs next to MangoHud and not over it, at
`/usr/lib/polyseat/mangohud/` and `/usr/lib32/polyseat/mangohud/`, so a
`pacman -Syu` that brings a new MangoHud cannot quietly put the broken shim
back. Each directory has a relative link to the distribution's
`libMangoHud_opengl.so`, because the shim loads that from its own directory.
Relative because a native game from Steam runs in the Steam Linux Runtime's
container, where the seat's `/usr` is `/run/host/usr`: an absolute link there
points into the runtime, and the game starts with the shim and without
MangoHud.

`provision.go` names it with `MangoHudShimVersion`, `MangoHudShimBuild` and
`mangoHudShimPackageSHA256`, fetches it from a release of this repository called
`mangohud-shim-<version>-<build>` and refuses anything whose checksum is not
the one in the source. `mangoHudPreload` in `apps.go` and `polyseat-capped`
point at it.

## What went wrong

Steam starts in a seat through `polyseat-capped`, so the shim is preloaded into
Steam and into everything it starts, and Steam appends its own
`gameoverlayrenderer.so` behind it for every game. Since the Steam client of
2026-10-13 that pair hangs every process at 100 % of a core before `main`, and
with it every game in every seat, native and Proton alike. `/bin/true` hangs
just the same.

The overlay exports `dlopen` and `dlsym`, and asks for the real `dlopen` with
`dlsym(RTLD_NEXT, "dlopen")`. Because the shim comes first, that call lands in
the shim's `dlsym`, which answers it with libc's `dlsym` from inside the shim.
`RTLD_NEXT` is resolved against the caller, and the caller is now the shim, so
"the next one after the shim" is the overlay's own `dlopen`. The overlay keeps
that as the real one and tail-jumps into itself forever. Seen in gdb as a
process spinning on one instruction in the overlay's `dlopen`, called from
`libGLdispatch`'s constructor.

## Why not MangoHud's master

MangoHud's 8bd15ed, "fix dlsym RTLD_NEXT lookup origin", is aimed at exactly
this and is not enough. It resolves `RTLD_NEXT` lookups by walking the link map
behind the caller, and lookups no longer reach the overlay's own `dlsym` at
all. The overlay only fills in its pointer to the real `close` there, so its
`close()` does nothing, and a pipe is never closed: any `$(...)` in a launch
script waits for an end of file that never comes. Measured here, with the
overlay from a seat: `/bin/true` passes, `sh -c 'x=$(echo hi)'` hangs, and
glxgears with both preloaded segfaults. Reported upstream as
flightlessmango/MangoHud#2177, which is also where this patch's idea comes from.

## What the patch does

`shim-rtld-next.patch` makes the shim's `dlsym` hand every name it does not
hook straight on to the *next* `dlsym` in the search order, as a tail call.
Through the tail call the next `dlsym` sees the original caller's return
address, which is what `RTLD_NEXT` is resolved against, and since it is the
next `dlsym` and not libc's, an interposer behind the shim, the overlay, still
sees the lookup. Names the shim does hook take the path they always took.

Measured with the seat's overlay, on the host and in a seat:

| | 0.8.4 | master | 0.8.4 with the patch |
|---|---|---|---|
| `/bin/true` | hangs | runs | runs |
| `$(...)` in sh | hangs | hangs | runs |
| 32 bit `popen` and `dlopen` | hangs | | runs |
| glxgears, cap at 30 | hangs | segfaults | 30.0 fps |
| glxgears, cap at 30, no overlay | 30.0 fps | | 30.0 fps |

And through Steam in a seat: Valheim native under OpenGL with
`libMangoHud_opengl.so` loaded, and Dredge under Proton.

## Building it

    packaging/mangohud-shim/build.sh <container>

in any running Arch container Incus knows, a seat for instance. It installs the
compiler there, runs `makepkg` on the `PKGBUILD` next to it, pulls the package
out, prints its checksum and takes the compiler out again if it put it in. A
minute.

## When it goes away

When a MangoHud release carries a shim that works beside Steam's overlay:
`mangoHudPreload` and `polyseat-capped` go back to
`/usr/$LIB/mangohud/libMangoHud_shim.so`, the step comes out of the recipe and
the package out of the seats.
