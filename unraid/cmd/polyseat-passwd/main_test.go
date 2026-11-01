package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/superuser404notfound/Polyseat/internal/auth"
)

func TestTheDaemonNeedsNoSetupAndAcceptsThePassword(t *testing.T) {
	dir := t.TempDir()

	if err := run([]string{dir, "owner"}, strings.NewReader("correct horse battery\n")); err != nil {
		t.Fatal(err)
	}

	info, err := os.Stat(filepath.Join(dir, "credentials.json"))
	if err != nil {
		t.Fatal(err)
	}

	if info.Mode().Perm() != 0o600 {
		t.Fatalf("credentials are %v, want 0600", info.Mode().Perm())
	}

	store, err := auth.Open(dir)
	if err != nil {
		t.Fatal(err)
	}

	if store.NeedsSetup() {
		t.Fatal("the daemon would still let whoever comes first choose the password")
	}

	if !store.Check("owner", "correct horse battery") || store.Check("owner", "wrong password") {
		t.Fatal("the stored password does not check out")
	}
}

func TestShortPasswordsAndMissingArgumentsAreRefused(t *testing.T) {
	dir := t.TempDir()

	if err := run([]string{dir, "owner"}, strings.NewReader("short\n")); err == nil {
		t.Fatal("a password below the daemon's minimum was accepted")
	}

	if err := run([]string{dir}, strings.NewReader("correct horse battery\n")); err == nil {
		t.Fatal("a missing user name was accepted")
	}

	if _, err := os.Stat(filepath.Join(dir, "credentials.json")); !os.IsNotExist(err) {
		t.Fatal("a refused call left credentials behind")
	}
}
