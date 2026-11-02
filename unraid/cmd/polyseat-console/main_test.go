package main

import (
	"os"
	"path/filepath"
	"testing"
)

type fakeKeyboard struct{ mode map[string]int }

func (f *fakeKeyboard) Mode(tty string) (int, error)       { return f.mode[tty], nil }
func (f *fakeKeyboard) SetMode(tty string, mode int) error { f.mode[tty] = mode; return nil }

func TestOffThenRestoreGivesTheOldModeBack(t *testing.T) {
	kb := &fakeKeyboard{mode: map[string]int{"/dev/tty1": 3}} // K_UNICODE
	state := filepath.Join(t.TempDir(), "console.json")

	if err := run([]string{"off", "/dev/tty1", state}, kb); err != nil {
		t.Fatal(err)
	}

	if kb.mode["/dev/tty1"] != kOff {
		t.Fatalf("mode %d, want K_OFF", kb.mode["/dev/tty1"])
	}

	// A second off (a crashed session) must keep the first saved mode.
	if err := run([]string{"off", "/dev/tty1", state}, kb); err != nil {
		t.Fatal(err)
	}

	if err := run([]string{"restore", state}, kb); err != nil {
		t.Fatal(err)
	}

	if kb.mode["/dev/tty1"] != 3 {
		t.Fatalf("restored %d, want 3", kb.mode["/dev/tty1"])
	}

	if _, err := os.Stat(state); !os.IsNotExist(err) {
		t.Fatal("state file left after restore")
	}
}

func TestNoGuessingAndNoBadState(t *testing.T) {
	kb := &fakeKeyboard{mode: map[string]int{"/dev/tty1": kOff}}
	dir := t.TempDir()

	if err := run([]string{"off", "/dev/tty1", filepath.Join(dir, "s.json")}, kb); err == nil {
		t.Fatal("K_OFF without a saved state was accepted")
	}

	bad := filepath.Join(dir, "bad.json")
	_ = os.WriteFile(bad, []byte(`{"tty":"/dev/tty1","mode":4}`), 0o600)

	if err := run([]string{"restore", bad}, kb); err == nil {
		t.Fatal("restoring K_OFF was accepted")
	}

	if err := run([]string{"restore"}, kb); err == nil {
		t.Fatal("missing arguments were accepted")
	}
}
