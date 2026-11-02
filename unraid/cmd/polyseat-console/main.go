// Command polyseat-console takes the keyboard away from a text console for the
// length of a seat session, and gives exactly the old mode back afterwards.
//
// Unraid port only (WIP). A seat's input devices are created through uinput in
// the host kernel, and the kernel's console keyboard handler listens to every
// keyboard: without this, Ctrl+Alt+Del in a game reboots Unraid (its inittab
// runs shutdown -r) and every key types into its login prompt. K_OFF makes the
// console ignore keys; SysRq is a separate handler the controller turns off.
//
//	polyseat-console off TTY STATEFILE   save TTY's keyboard mode, then K_OFF
//	polyseat-console restore STATEFILE   put the saved mode back
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

// From linux/kd.h.
const (
	kdgkbmode = 0x4B44
	kdskbmode = 0x4B45
	kOff      = 0x04
)

type state struct {
	TTY  string `json:"tty"`
	Mode int    `json:"mode"`
}

// keyboard is the part of a tty this tool touches, so tests need no console.
type keyboard interface {
	Mode(tty string) (int, error)
	SetMode(tty string, mode int) error
}

type ioctlKeyboard struct{}

func (ioctlKeyboard) Mode(tty string) (int, error) {
	f, err := os.OpenFile(tty, os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		return 0, err
	}
	defer f.Close()

	var mode int32
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), kdgkbmode, uintptr(unsafe.Pointer(&mode))); errno != 0 {
		return 0, errno
	}

	return int(mode), nil
}

func (ioctlKeyboard) SetMode(tty string, mode int) error {
	f, err := os.OpenFile(tty, os.O_RDWR|syscall.O_NOCTTY, 0)
	if err != nil {
		return err
	}
	defer f.Close()

	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), kdskbmode, uintptr(mode)); errno != 0 {
		return errno
	}

	return nil
}

func main() {
	if err := run(os.Args[1:], ioctlKeyboard{}); err != nil {
		fmt.Fprintln(os.Stderr, "polyseat-console:", err)
		os.Exit(1)
	}
}

func run(args []string, kb keyboard) error {
	switch {
	case len(args) == 3 && args[0] == "off":
		return off(args[1], args[2], kb)
	case len(args) == 2 && args[0] == "restore":
		return restore(args[1], kb)
	}

	return errors.New("usage: polyseat-console off TTY STATEFILE | restore STATEFILE")
}

func off(tty, statefile string, kb keyboard) error {
	if _, err := os.Lstat(statefile); err == nil {
		// A state file means an earlier session did not restore: keep the
		// mode it saved, which is the one the console really had.
		return kb.SetMode(tty, kOff)
	}

	mode, err := kb.Mode(tty)
	if err != nil {
		return err
	}

	if mode == kOff {
		return errors.New(tty + " is already K_OFF without a saved state; not guessing the mode to restore")
	}

	data, _ := json.Marshal(state{TTY: tty, Mode: mode})
	if err := os.WriteFile(statefile, append(data, '\n'), 0o600); err != nil {
		return err
	}

	return kb.SetMode(tty, kOff)
}

func restore(statefile string, kb keyboard) error {
	data, err := os.ReadFile(statefile)
	if err != nil {
		return err
	}

	var s state
	if err := json.Unmarshal(data, &s); err != nil || s.TTY == "" || s.Mode == kOff || s.Mode < 0 || s.Mode > 4 {
		return errors.New("unusable console state " + statefile)
	}

	if err := kb.SetMode(s.TTY, s.Mode); err != nil {
		return err
	}

	return os.Remove(statefile)
}
