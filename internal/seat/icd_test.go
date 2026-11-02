package seat

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadFirstTakesTheFirstManifestThatExists(t *testing.T) {
	dir := t.TempDir()
	etc := filepath.Join(dir, "etc.json")

	if err := os.WriteFile(etc, []byte(`{"from":"etc"}`), 0o644); err != nil {
		t.Fatal(err)
	}

	data, err := readFirst([]string{filepath.Join(dir, "usr.json"), etc})
	if err != nil || string(data) != `{"from":"etc"}` {
		t.Fatalf("got %q, %v", data, err)
	}

	if _, err := readFirst([]string{filepath.Join(dir, "a"), filepath.Join(dir, "b")}); err == nil {
		t.Fatal("no manifest at all was not reported")
	}
}
