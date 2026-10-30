# Stevia, as a seat gets it

A seat's on-screen keyboard is Stevia, the one Phosh replaced squeekboard with,
built from its 0.58.0 release with one patch as a package of its own:

    polyseat-stevia-0.58.0-1-x86_64.pkg.tar.zst

It installs next to squeekboard and not in conflict with it, because a seat
built before this has squeekboard and `pacman -U --noconfirm` answers no to a
conflict. The price is the files they share, the desktop entry, the D-Bus
activation and the systemd user unit, which the recipe leaves out. Nothing uses
them: sway starts the keyboard.

`provision.go` names it with `SteviaVersion`, `SteviaBuild` and
`steviaPackageSHA256`, fetches it from a release of this repository called
`stevia-<version>-<build>` and refuses anything whose checksum is not the one in
the source. `polyseat-keyboard` starts it, with the seat's language and the dark
style, and ends it again.

## Why not the keyboard from the repositories

Compared on 2026-10-30 in a seat, on a 4K output, over the application grid:

- **squeekboard** is half the screen and white. Its height has no setting:
  `scale-in-horizontal-screen-orientation` and
  `layout-shape-changes-to-fit-panel` change the width of the keys and nothing
  else. It sits on the top layer, so under the grid, and pressing L3 there
  opened it where nobody could see it.
- **Stevia as Arch packages it** does not start on sway. It waits five seconds
  for `zphoc_device_state_v1`, which only phoc offers, and gives up.
- **wvkbd** works, is small and sits on the overlay layer, and it was a real
  candidate. Its layouts are compiled in, so a language is a table somebody has
  to write, and every language without one gets US. It also lost the first key
  pressed in the grid's search field, twice out of two, where Stevia did not.
  Stevia writes through the input method protocol rather than as key presses.
- **plasma-keyboard** was not tried. It brings KDE's libraries into every seat.
  **onboard** runs only through XWayland.

## What the patch does

Four things, each small, and each because a seat is not a phone.

- **`zphoc_device_state_v1` is optional.** It tells the keyboard about hardware
  keyboards and nothing else, so leaving it out loses one thing, which the next
  point makes unnecessary.
- **Text focus does not hide it.** Upstream ties it to text focus, which on a
  phone is the point. In a seat it closed the moment somebody clicked into the
  field they had opened it for, and there was no reason for it to open by itself
  for a client that has a keyboard of its own.
- **It sits on the overlay layer**, because the application grid does. Upstream
  uses the top layer, which puts the keyboard underneath.
- **Its height is 26 % of the screen.** Upstream works it out from the size of
  the display in millimetres, and a seat's output has none, so it fell back to a
  fixed 200 pixels. A share of the screen follows a client changing resolution
  with no help.

## Why it is not kept running

It looks like the obvious thing, and it was tried, and four patches to its
animation did not make it work. Hidden, it keeps a surface from the start of the
session, under the grid that opened after it, or a new one off the screen, where
sway sends no frames and GTK's frame clock stops. Either way the first press
showed nothing for up to two seconds. `polyseat-keyboard` starts it on the press
and ends it on the next, so it is on the screen from its first frame and on top
of what was there, and none of that needs patching.

## Building

    packaging/stevia/build.sh <container>

Runs in a running Arch container, a seat will do, and puts the compiler back out
afterwards. It prints the checksum for `steviaPackageSHA256`.

**The release order matters.** The package goes on its own release first, with
`--latest=false` like the Sunshine and shim ones, and the Polyseat release that
names its checksum after it.

Going to a new Stevia means checking the patch still applies and that the
keyboard still comes up on sway: `src/pos-wayland.c` is the list of protocols it
insists on, and it says nothing but `Failed to find all Wayland globals` when one
is missing.
