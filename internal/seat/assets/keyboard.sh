#!/bin/sh
# Polyseat - show or hide the on-screen keyboard in a seat.
#
# This exists because of one case that nothing else in the chain covers.
# Signing in to a store from a client that has no keyboard: an Apple TV, a
# phone, a television. Moonlight cannot help there, it has an open request for
# exactly this and an open bug for keyboard passthrough on tvOS, so what it
# sends is modifiers rather than text. Steam's own keyboard covers Steam and,
# unreliably, whatever has been added to it as a non-Steam game. Neither
# reaches a browser window in a launcher.
#
# So the keyboard is rendered inside the seat and travels in the video stream
# like everything else, and the controller drives it as a mouse. Reachable
# three ways on purpose, because whoever needs it has the fewest input devices
# of anybody: Super+K, the bar, and a gamepad button.
#
# Stevia was written for Phosh and needs the patch in packaging/stevia to run on
# sway at all. It takes the layout from the desktop's input sources.
#
# It runs only while it is on screen, so hiding it is ending it. That is not
# thrift. Kept running and hidden it would be a surface created at the start of
# the session, under the application grid that opens after it, and one whose
# frame clock stands still off the screen: measured in a seat, the first press
# showed nothing for up to two seconds, or until something else redrew it.
# Started by the press it comes up on the screen and on top of what is there,
# every time, and with the language the seat has now.

: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

# The kernel keeps fifteen characters of a process name.
NAME=phosh-osk-stevi

running() {
    pgrep -x "$NAME" >/dev/null 2>&1
}

# Tell the keyboard the language of this seat.
#
# Read from the seat's own sway config, which is where the host's keyboard
# already went, so there is one answer rather than two that can disagree. The
# layout and not the variant: Stevia has one set of keys per language, and a
# variant it does not know would leave nothing rather than the nearest thing.
# Several layouts become several entries behind the keyboard's globe key. A seat
# with no layout set is left alone and gets the keyboard's own default, US.
language() {
    layouts=$(awk '$1 == "xkb_layout" { print $2; exit }' \
        "$HOME/.config/sway/config" 2>/dev/null)
    [ -n "$layouts" ] || return 0

    sources=
    old_ifs=$IFS
    IFS=,
    for layout in $layouts; do
        sources="$sources${sources:+, }('xkb', '$layout')"
    done
    IFS=$old_ifs

    gsettings set org.gnome.desktop.input-sources sources "[$sources]"
}

show() {
    running && return 0

    language

    # GTK_THEME on this one program, because the alternative is the seat's
    # colour scheme, and that would turn every application somebody starts dark
    # as well.
    #
    # Detached, because this is called from the pointer helper and the bar, and
    # neither should be waiting on a keyboard.
    GTK_THEME=Adwaita:dark setsid phosh-osk-stevia >/dev/null 2>&1 </dev/null &
}

hide() {
    pkill -x "$NAME" >/dev/null 2>&1
}

case "$1" in
    show) show ;;
    hide) hide ;;
    *)
        if running; then hide; else show; fi
        ;;
esac

exit 0
