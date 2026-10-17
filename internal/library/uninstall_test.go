package library

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// TestUninstallTakesATitleFromEverywhere is the reason Uninstall exists: a
// title removed only from the pool comes back from the first seat that still
// has it, so getting rid of one meant visiting every seat by hand.
func TestUninstallTakesATitleFromEverywhere(t *testing.T) {
	pool := openPool(t)
	seats := updatable("seat1", "seat2", "seat3")
	host := external(t, true)
	all := append(seats, host)

	install(t, pool.SeatApps("seat1"), "440", "Team Fortress 2", "Team Fortress 2", "1000", StateInstalled)
	install(t, pool.SeatApps("seat1"), "570", "Dota 2", "dota 2 beta", "1000", StateInstalled)

	if _, err := pool.Sync(all, nil); err != nil {
		t.Fatal(err)
	}

	// seat3 uninstalled it itself, which leaves a declined mark behind.
	removeAll(t, filepath.Join(pool.SeatApps("seat3"), commonDir, "Team Fortress 2"))
	removeAll(t, filepath.Join(pool.SeatApps("seat3"), ManifestName("440")))

	if _, err := pool.Sync(all, nil); err != nil {
		t.Fatal(err)
	}

	// What Steam keeps beside a game in each seat, the prefix with the saves
	// among it.
	for _, seat := range []string{"seat1", "seat2"} {
		mkdirs(t, filepath.Join(pool.SeatApps(seat), "compatdata", "440"), filepath.Join(pool.SeatApps(seat), "shadercache", "440"))
		write(t, filepath.Join(pool.SeatApps(seat), "compatdata", "440", "save.dat"), "save", 0o644)
		write(t, filepath.Join(pool.SeatApps(seat), "shadercache", "440", "cache.bin"), "cache", 0o644)
	}

	gone, err := pool.Uninstall("440", all, nil)
	if err != nil {
		t.Fatalf("Uninstall: %v", err)
	}

	if len(gone) != 3 {
		t.Errorf("uninstalled from %+v, want seat1, seat2 and host", gone)
	}

	for _, apps := range []string{pool.PoolApps(), pool.SeatApps("seat1"), pool.SeatApps("seat2"), host.Apps} {
		for _, path := range []string{
			filepath.Join(apps, ManifestName("440")),
			filepath.Join(apps, commonDir, "Team Fortress 2"),
			filepath.Join(apps, "shadercache", "440"),
		} {
			if _, err := os.Lstat(path); err == nil {
				t.Errorf("%s is still there", path)
			}
		}

		if _, err := os.Stat(filepath.Join(apps, commonDir, "dota 2 beta")); err != nil {
			t.Errorf("uninstalling one title took another from %s: %v", apps, err)
		}
	}

	for _, seat := range []string{"seat1", "seat2"} {
		if _, err := os.Stat(filepath.Join(pool.SeatApps(seat), "compatdata", "440", "save.dat")); err != nil {
			t.Errorf("%s lost its saves: %v", seat, err)
		}
	}

	if _, err := pool.Sync(all, nil); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Lstat(filepath.Join(pool.PoolApps(), ManifestName("440"))); err == nil {
		t.Error("the next pass brought the title back")
	}

	// A fresh install later is shared like any other, including with the seat
	// that had declined the old one.
	install(t, pool.SeatApps("seat2"), "440", "Team Fortress 2", "Team Fortress 2", "1001", StateInstalled)

	if _, err := pool.Sync(all, nil); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(filepath.Join(pool.SeatApps("seat3"), commonDir, "Team Fortress 2")); err != nil {
		t.Errorf("a reinstall did not reach the seat that had declined the old copy: %v", err)
	}
}

// A seat playing the game keeps every copy where it is, its own and everybody
// else's, because a partial uninstall would be undone by that seat.
func TestUninstallWaitsForEveryMember(t *testing.T) {
	pool := openPool(t)
	seats := updatable("seat1", "seat2")

	install(t, pool.SeatApps("seat1"), "440", "Team Fortress 2", "Team Fortress 2", "1000", StateInstalled)

	if _, err := pool.Sync(seats, nil); err != nil {
		t.Fatal(err)
	}

	seats[1].Updatable = false

	_, err := pool.Uninstall("440", seats, nil)

	var busy *BusyError
	if !errors.As(err, &busy) || len(busy.Members) != 1 || busy.Members[0] != "seat2" {
		t.Fatalf("Uninstall = %v, want seat2 named as busy", err)
	}

	for _, apps := range []string{pool.PoolApps(), pool.SeatApps("seat1"), pool.SeatApps("seat2")} {
		if _, err := os.Stat(filepath.Join(apps, commonDir, "Team Fortress 2")); err != nil {
			t.Errorf("a refused uninstall deleted %s: %v", apps, err)
		}
	}
}

// A watched library that is not a member is only ever read, so a copy there
// would be taken straight back into the pool.
func TestUninstallRefusesWhatAReadOnlySourceHas(t *testing.T) {
	pool := openPool(t)

	source := filepath.Join(reflinkDir(t), "hostlib")
	mkdirs(t, source)
	install(t, source, "570", "Dota 2", "dota 2 beta", "1000", StateInstalled)

	if _, err := pool.AddSource(source, nil); err != nil {
		t.Fatal(err)
	}

	if _, err := pool.Uninstall("570", updatable("seat1"), nil); err == nil {
		t.Fatal("Uninstall went ahead while a library it may not write into has the title")
	}

	if _, err := os.Stat(filepath.Join(pool.PoolApps(), commonDir, "dota 2 beta")); err != nil {
		t.Errorf("the refused uninstall deleted the pool's copy: %v", err)
	}
}

func TestUninstallTakesAFolderFromEverywhere(t *testing.T) {
	pool := openPool(t)
	seats := updatable("seat1", "seat2")

	putFolder(t, pool.SeatFolders("seat1"), "Hollow Knight", "hk")

	if _, err := pool.Sync(seats, nil); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Stat(filepath.Join(pool.SeatFolders("seat2"), "Hollow Knight")); err != nil {
		t.Fatalf("the folder never reached seat2: %v", err)
	}

	gone, err := pool.Uninstall(folderKey("Hollow Knight"), seats, nil)
	if err != nil {
		t.Fatalf("Uninstall: %v", err)
	}

	if len(gone) != 2 {
		t.Errorf("uninstalled from %+v, want both seats", gone)
	}

	for _, dir := range []string{pool.PoolFolders(), pool.SeatFolders("seat1"), pool.SeatFolders("seat2")} {
		if _, err := os.Lstat(filepath.Join(dir, "Hollow Knight")); err == nil {
			t.Errorf("the folder is still in %s", dir)
		}
	}

	if _, err := pool.Sync(seats, nil); err != nil {
		t.Fatal(err)
	}

	if _, err := os.Lstat(filepath.Join(pool.PoolFolders(), "Hollow Knight")); err == nil {
		t.Error("the next pass brought the folder back")
	}
}

func TestUninstallRefusesABadName(t *testing.T) {
	pool := openPool(t)

	for _, id := range []string{"../x", folderKey(".."), folderKey("a/b"), folderKey("")} {
		if _, err := pool.Uninstall(id, updatable("seat1"), nil); err == nil {
			t.Errorf("Uninstall(%q) was accepted", id)
		}
	}
}

func removeAll(t *testing.T, path string) {
	t.Helper()

	if err := os.RemoveAll(path); err != nil {
		t.Fatal(err)
	}
}
