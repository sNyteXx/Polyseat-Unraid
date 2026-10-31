package seat

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
)

// edenEntry is what Eden 0.2.1 wrote in seat vince, with sh for the AppImage so
// that the test can tell it exists.
const edenEntry = `[Desktop Entry]
Type=Application
Version=1.0
Name=Pokémon Strahlender Diamant
Comment=Start Pokémon Strahlender Diamant with the Eden Emulator
Icon=ICON
TryExec=/bin/sh
Exec=/bin/sh -f -g "/home/player/Downloads/Spiele/Pokemon Brilliant Diamond [0100000011D90000][v0](axekin.fr).xci"
Categories=Game;Emulator;Qt;
Keywords=Switch;Nintendo;
`

// boostSplit is build_args from boost/process/v1/detail/posix/basic_cmd.hpp,
// which is what Sunshine hands every command to.
func boostSplit(data string) []string {
	var st []string

	entry := func(s string) string {
		if len(s) > 0 && s[0] == '"' && s[len(s)-1] == '"' {
			s = s[1 : len(s)-1]
		}

		return strings.ReplaceAll(s, `\"`, `"`)
	}

	inQuote := false
	begin := 0

	for i := 0; i < len(data); i++ {
		if data[i] == '"' {
			inQuote = !inQuote
		}

		if !inQuote && data[i] == ' ' {
			if i != 0 && data[i-1] != ' ' {
				st = append(st, entry(data[begin:i]))
			}

			begin = i + 1
		}
	}

	if begin != len(data) {
		st = append(st, entry(data[begin:]))
	}

	return st
}

func runDesktopScan(t *testing.T, home string) string {
	t.Helper()

	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("SKIPPED: no python3 to run the scan with, so its behaviour is unverified here")
	}

	cmd := exec.Command(python, "-c", desktopScan)
	cmd.Env = append(os.Environ(), "POLYSEAT_HOME="+home)

	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("the scan failed: %v", err)
	}

	return string(out)
}

func writeEntry(t *testing.T, home, name, body string) {
	t.Helper()

	dir := filepath.Join(home, ".local/share/applications")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}

	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

// The bug as it was found: a shortcut in the grid that Moonlight never heard of,
// and the words that reach the process being the ones the entry meant.
func TestAnEmulatorsShortcutBecomesACard(t *testing.T) {
	home := t.TempDir()

	icon := filepath.Join(home, "eden.png")
	if err := os.WriteFile(icon, []byte("png"), 0o644); err != nil {
		t.Fatal(err)
	}

	writeEntry(t, home, "Pokémon Strahlender Diamant.desktop",
		strings.ReplaceAll(edenEntry, "ICON", icon))

	games, err := parseDesktopGames(runDesktopScan(t, home), nil)
	if err != nil {
		t.Fatal(err)
	}

	if len(games) != 1 {
		t.Fatalf("got %d games, want the shortcut: %+v", len(games), games)
	}

	g := games[0]
	if g.Name != "Pokémon Strahlender Diamant" || g.Image != icon || g.Source != desktopSource {
		t.Errorf("wrong game: %+v", g)
	}

	want := []string{"/bin/sh", "-f", "-g",
		"/home/player/Downloads/Spiele/Pokemon Brilliant Diamond [0100000011D90000][v0](axekin.fr).xci"}

	if got := boostSplit(g.Launch); !reflect.DeepEqual(got, want) {
		t.Errorf("Sunshine would run %q, want %q", got, want)
	}
}

func TestWhatIsNotAShortcutToAGameStaysOutOfTheList(t *testing.T) {
	game := func(extra string) string {
		return "[Desktop Entry]\nType=Application\nName=X\nExec=/bin/sh\nCategories=Game;\n" + extra
	}

	entries := map[string]string{
		"steam-shortcut": "[Desktop Entry]\nType=Application\nName=DREDGE\n" +
			"Exec=/bin/sh steam://rungameid/1562430\nCategories=Game;\n",
		"lutris-shortcut": "[Desktop Entry]\nType=Application\nName=L\n" +
			"Exec=/bin/sh lutris:rungameid/2\nCategories=Game;\n",
		"not-a-game": "[Desktop Entry]\nType=Application\nName=Y\nExec=/bin/sh\nCategories=Utility;\n",
		"hidden":     game("Hidden=true\n"),
		"nodisplay":  game("NoDisplay=true\n"),
		"terminal":   game("Terminal=true\n"),
		"ours":       game("X-Polyseat=steam\n"),
		"gone":       "[Desktop Entry]\nType=Application\nName=Z\nExec=/no/such/emulator a\nCategories=Game;\n",
		"tryexec":    game("TryExec=/no/such/emulator\n"),
		"unbalanced": "[Desktop Entry]\nType=Application\nName=U\nExec=/bin/sh \"a\nCategories=Game;\n",
		"action": "[Desktop Entry]\nType=Link\nName=A\n[Desktop Action x]\nType=Application\n" +
			"Exec=/bin/sh\nCategories=Game;\n",
	}

	home := t.TempDir()

	for name, body := range entries {
		writeEntry(t, home, name+".desktop", body)
	}

	writeEntry(t, home, entryPrefix+"abc.desktop", game(""))

	if err := syscall.Mkfifo(filepath.Join(home, ".local/share/applications/fifo.desktop"), 0o644); err != nil {
		t.Fatal(err)
	}

	if got := strings.TrimSpace(runDesktopScan(t, home)); got != "[]" {
		t.Errorf("the scan found %s", got)
	}
}

func TestAShortcutToTheEmulatorAloneIsNotASecondCard(t *testing.T) {
	out, err := json.Marshal([]map[string]any{{"name": "Eden Emulator", "argv": []string{"/a/Eden"}}})
	if err != nil {
		t.Fatal(err)
	}

	games, err := parseDesktopGames(string(out), []Game{{Name: "Eden", Launch: "/a/Eden"}})
	if err != nil {
		t.Fatal(err)
	}

	if len(games) != 0 {
		t.Errorf("got %+v", games)
	}
}

func TestSunshineReadsBackWhatItIsGiven(t *testing.T) {
	for _, argv := range [][]string{
		{"/a/b", "-g", "/c d/e [f](g).xci"},
		{"/a/b", ""},
		{"/a b/c", "it's", `back\slash`},
	} {
		line, ok := sunshineCommand(argv)
		if !ok {
			t.Fatalf("%q was refused", argv)
		}

		if got := boostSplit(line); !reflect.DeepEqual(got, argv) {
			t.Errorf("%q came back as %q through %q", argv, got, line)
		}
	}

	if line, _ := sunshineCommand([]string{"/a", "$(HOME)"}); line != "/a $$(HOME)" {
		t.Errorf("a dollar sign is left for Sunshine to expand: %q", line)
	}

	if _, ok := sunshineCommand([]string{"/a", `b "c" d`}); ok {
		t.Error("a word Boost cannot be told about was accepted")
	}
}

// One entry in the grid, not the one Eden wrote and one of ours beside it.
func TestAShortcutIsNotWrittenBackIntoTheLauncher(t *testing.T) {
	games := []Game{
		{Name: "From a shortcut", Launch: `/a/Eden -g "/b c.xci"`, Source: desktopSource},
		{Name: "From Lutris", Launch: "lutris lutris:rungameid/2", Source: "lutris"},
	}

	want := wantedEntries(games, "")

	if len(want) != 1 {
		t.Fatalf("got %d entries, want only the Lutris game's", len(want))
	}

	for _, body := range want {
		if !strings.Contains(string(body), "From Lutris") {
			t.Errorf("wrote %s", body)
		}
	}
}
