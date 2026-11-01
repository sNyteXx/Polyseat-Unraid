package seat

import (
	"os"
	"strings"
	"testing"
)

// Both files are real Steam configurations out of a seat, with every value
// replaced by a placeholder and the account anonymised. The structure is what
// this code has to be right about, and a handwritten sample would only have the
// structure I imagined: four blocks deep, tab indented, with the mapping
// sitting among two hundred other keys.
func steamConfig(t *testing.T, name string) []byte {
	t.Helper()

	body, err := os.ReadFile("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}

	return body
}

// mappedTool reads the setting back out the way Steam would.
func mappedTool(t *testing.T, data []byte) string {
	t.Helper()

	root, err := parseVDF(string(data))
	if err != nil {
		t.Fatalf("the result does not parse: %v", err)
	}

	steam := steamBlock(root)
	if steam == nil {
		t.Fatal("the result has no Steam block")
	}

	mapping := steam.child(compatMappingKey)
	if mapping == nil {
		return ""
	}

	global := mapping.child(compatGlobalKey)
	if global == nil {
		return ""
	}

	name := global.child("name")
	if name == nil {
		return ""
	}

	return strings.Trim(string(data)[name.valueStart:name.valueEnd], `"`)
}

// A seat that has never had the setting is the case this exists for.
func TestCompatToolIsAddedWhereThereIsNone(t *testing.T) {
	before := steamConfig(t, "config-without-mapping.vdf")

	if got := mappedTool(t, before); got != "" {
		t.Fatalf("the sample already has a mapping (%q), so this test proves nothing", got)
	}

	after, changed, err := SetCompatTool(before, protonName)
	if err != nil {
		t.Fatal(err)
	}

	if !changed {
		t.Fatal("nothing was changed")
	}

	if got := mappedTool(t, after); got != protonName {
		t.Errorf("the setting reads %q", got)
	}
}

// The file holds the account this seat is signed in as. Everything this code
// does not understand has to come out exactly as it went in, or a seat gets
// provisioned and somebody has to sign in to Steam again.
func TestCompatToolLeavesTheRestOfTheFileAlone(t *testing.T) {
	before := steamConfig(t, "config-without-mapping.vdf")

	after, _, err := SetCompatTool(before, protonName)
	if err != nil {
		t.Fatal(err)
	}

	// Everything before the insertion and everything after it, byte for byte.
	cut := strings.Index(string(after), compatMappingKey)
	if cut < 0 {
		t.Fatal("the mapping is not in the result")
	}

	head := string(after)[:cut]
	if !strings.HasPrefix(string(before), strings.TrimRight(head, "\t\n\"")) {
		t.Error("the text before the insertion was modified")
	}

	for _, keep := range []string{`"Accounts"`, `"someone"`, `"CMWebSocket"`, `"SDL_GamepadBind"`} {
		if strings.Count(string(before), keep) != strings.Count(string(after), keep) {
			t.Errorf("%s no longer appears the same number of times", keep)
		}
	}

	if len(after) <= len(before) {
		t.Errorf("the file did not grow: %d then %d", len(before), len(after))
	}
}

// The name used to carry the version, which is how it was set by hand before
// this existed, and that setting stops meaning anything at the next update. It
// names our tool either way, so it is the same decision and gets rewritten.
func TestCompatToolIsMovedOffAVersionedName(t *testing.T) {
	before := steamConfig(t, "config-with-old-mapping.vdf")

	if got := mappedTool(t, before); !strings.HasPrefix(got, protonName+"-") {
		t.Fatalf("the sample reads %q, so it is not the case this test is about", got)
	}

	after, changed, err := SetCompatTool(before, protonName)
	if err != nil {
		t.Fatal(err)
	}

	if !changed {
		t.Fatal("the versioned name was left in place and will point at nothing after an update")
	}

	if got := mappedTool(t, after); got != protonName {
		t.Errorf("the setting reads %q", got)
	}
}

// Somebody who chose Proton Experimental on purpose keeps it. Provisioning a
// seat again is not the moment to take that decision back.
func TestCompatToolKeepsSomebodyElsesChoice(t *testing.T) {
	before := steamConfig(t, "config-with-old-mapping.vdf")

	theirs, _, err := SetCompatTool(before, "proton_experimental")
	if err != nil {
		t.Fatal(err)
	}

	if got := mappedTool(t, theirs); got != "proton_experimental" {
		t.Fatalf("the sample could not be put into the state this test needs: %q", got)
	}

	after, changed, err := SetCompatTool(theirs, protonName)
	if err != nil {
		t.Fatal(err)
	}

	if changed {
		t.Error("their choice was overwritten")
	}

	if got := mappedTool(t, after); got != "proton_experimental" {
		t.Errorf("the setting now reads %q", got)
	}
}

// Writing it twice has to be the same as writing it once, because this runs on
// every provisioning and every six hours after that.
func TestCompatToolSettlesAfterOnePass(t *testing.T) {
	for _, name := range []string{"config-without-mapping.vdf", "config-with-old-mapping.vdf"} {
		once, _, err := SetCompatTool(steamConfig(t, name), protonName)
		if err != nil {
			t.Fatal(err)
		}

		twice, changed, err := SetCompatTool(once, protonName)
		if err != nil {
			t.Fatal(err)
		}

		if changed {
			t.Errorf("%s: the second pass changed it again", name)
		}

		if string(once) != string(twice) {
			t.Errorf("%s: the second pass rewrote the file", name)
		}
	}
}

// A seat whose Steam has never run has no file at all.
func TestCompatToolWritesAFileForASeatWithNoSteamYet(t *testing.T) {
	after, changed, err := SetCompatTool(nil, protonName)
	if err != nil {
		t.Fatal(err)
	}

	if !changed {
		t.Fatal("nothing was written")
	}

	if got := mappedTool(t, after); got != protonName {
		t.Errorf("the setting reads %q", got)
	}
}

// What must never happen is a file that Steam cannot read, because the thing
// it holds is the login. Anything unrecognisable is refused rather than fixed.
func TestCompatToolRefusesWhatItCannotUnderstand(t *testing.T) {
	for name, body := range map[string]string{
		"an unbalanced brace":      `"InstallConfigStore"` + "\n{\n\t\"Software\"\n\t{\n",
		"a string that never ends": `"InstallConfigStore"` + "\n{\n\t\"Softwar",
		"something else entirely":  `{"json": true}`,
		"another program's file":   `"UserLocalConfigStore"` + "\n{\n}\n",
	} {
		after, changed, err := SetCompatTool([]byte(body), protonName)

		if err == nil {
			t.Errorf("%s: accepted", name)
		}

		if changed || string(after) != body {
			t.Errorf("%s: the file was written anyway", name)
		}
	}
}

// The bug this is about: `install -d -o uid -g uid` applies the ownership to
// the last component only, so the parents it had to create on a seat being
// built for the first time came out as root, and the player could then add
// nothing of their own to their own home. Flathub and the shared library both
// stopped on it in the same run. Nothing on the daemon's side may create a
// directory under the player's home as root; the file API is what does it, and
// that carries the uid down every component it makes.
func TestNothingInstallsDirectoriesIntoThePlayersHome(t *testing.T) {
	source, err := os.ReadFile("provision.go")
	if err != nil {
		t.Fatal(err)
	}

	for _, line := range strings.Split(string(source), "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "//") {
			continue
		}

		if strings.Contains(line, `"install", "-d"`) && strings.Contains(line, "steam") {
			t.Errorf("a directory under %s is created with install -d: %s",
				playerHome, strings.TrimSpace(line))
		}
	}
}

// What the repair covers. Every directory between the player's home and the
// file, and not the file, and not the home itself, which useradd made and which
// belongs to them already.
func TestTheRepairCoversEveryDirectoryOnTheWayToSteamsConfiguration(t *testing.T) {
	want := []string{
		playerHome + "/.local",
		playerHome + "/.local/share",
		steamRoot,
		steamRoot + "/config",
	}

	got := playerDirs()

	if len(got) != len(want) {
		t.Fatalf("the repair covers %v, wanted %v", got, want)
	}

	for i := range want {
		if got[i] != want[i] {
			t.Errorf("the repair covers %s where %s was wanted", got[i], want[i])
		}
	}
}

// The mount point of the shared library is not repaired, because it is not the
// seat's to give away: those files are on the host and every other seat sharing
// the pool sees the same ones.
func TestTheRepairLeavesTheSharedLibraryAlone(t *testing.T) {
	for _, dir := range playerDirs() {
		if dir == steamApps || dir == LibraryMount {
			t.Errorf("the repair reaches %s, which is a mount, not the seat's own", dir)
		}
	}
}

// inGameScreensaver reads the switch back out the way Steam would, and says
// whether it was there at all.
func inGameScreensaver(t *testing.T, data []byte) (string, bool) {
	t.Helper()

	root, err := parseVDF(string(data))
	if err != nil {
		t.Fatalf("the result does not parse: %v", err)
	}

	node := root

	for _, key := range []string{"InstallConfigStore", customizationKey, screensaverKey, whileInGameKey} {
		if node = node.child(key); node == nil {
			return "", false
		}
	}

	return strings.Trim(string(data)[node.valueStart:node.valueEnd], `"`), true
}

func TestScreensaverIsTurnedOffAtEveryDepth(t *testing.T) {
	const head = `"InstallConfigStore"` + "\n{\n\t\"Software\"\n\t{\n\t\t\"Valve\"\n\t\t{\n" +
		"\t\t\t\"Steam\"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n"

	for name, body := range map[string]string{
		"no customization": head + "}\n",
		"no screensaver":   head + "\t\"Customization\"\n\t{\n\t\t\"StartupMovie\"\n\t\t{\n\t\t}\n\t}\n}\n",
		"no switch":        head + "\t\"Customization\"\n\t{\n\t\t\"Screensaver\"\n\t\t{\n\t\t}\n\t}\n}\n",
		"switched on":      head + "\t\"Customization\"\n\t{\n\t\t\"Screensaver\"\n\t\t{\n\t\t\t\"WhileInGame\"\t\t\"1\"\n\t\t}\n\t}\n}\n",
	} {
		t.Run(name, func(t *testing.T) {
			after, changed, err := SetScreensaverOff([]byte(body))
			if err != nil {
				t.Fatal(err)
			}

			if !changed {
				t.Error("it did not report a change")
			}

			if got, ok := inGameScreensaver(t, after); !ok || got != "0" {
				t.Errorf("the switch reads %q, present %v", got, ok)
			}

			again, changed, err := SetScreensaverOff(after)
			if err != nil {
				t.Fatal(err)
			}

			if changed || string(again) != string(after) {
				t.Error("a second pass changed something")
			}
		})
	}
}

// The file Steam actually writes, four blocks of account state and all.
func TestScreensaverLeavesTheRestOfTheFileAlone(t *testing.T) {
	before := steamConfig(t, "config-with-old-mapping.vdf")

	after, _, err := SetScreensaverOff(before)
	if err != nil {
		t.Fatal(err)
	}

	if got, ok := inGameScreensaver(t, after); !ok || got != "0" {
		t.Fatalf("the switch reads %q, present %v", got, ok)
	}

	if len(after) <= len(before) {
		t.Fatal("nothing was added")
	}

	at := strings.Index(string(after), `"Customization"`)
	if at < 0 {
		t.Fatal("no block was added")
	}

	end := strings.Index(string(after)[at:], "\n\t}") + at + len("\n\t}")

	// Taking the inserted block back out has to give the original file, byte
	// for byte, apart from the line break it started with.
	if got := string(after)[:at-2] + string(after)[end:]; got != string(before) {
		t.Error("something outside the new block changed")
	}
}

// A seat being built has no file and may have no Proton yet, and the next pass
// has to be able to add the default to what this wrote.
func TestScreensaverWritesAFileTheCompatToolCanStillEdit(t *testing.T) {
	after, changed, err := SetScreensaverOff(nil)
	if err != nil {
		t.Fatal(err)
	}

	if !changed {
		t.Error("it did not report a change")
	}

	both, _, err := SetCompatTool(after, protonName)
	if err != nil {
		t.Fatal(err)
	}

	if got := mappedTool(t, both); got != protonName {
		t.Errorf("the default is %q", got)
	}

	if got, ok := inGameScreensaver(t, both); !ok || got != "0" {
		t.Errorf("the switch reads %q, present %v", got, ok)
	}
}

func TestScreensaverRefusesWhatItCannotUnderstand(t *testing.T) {
	for name, body := range map[string]string{
		"an unbalanced brace": `"InstallConfigStore"` + "\n{\n",
		"not a configuration": `"Something"` + "\n{\n}\n",
		"a pair in the path":  `"InstallConfigStore"` + "\n{\n\t\"Customization\"\t\t\"1\"\n}\n",
	} {
		t.Run(name, func(t *testing.T) {
			after, changed, err := SetScreensaverOff([]byte(body))
			if err == nil {
				t.Error("it did not refuse")
			}

			if changed || string(after) != body {
				t.Error("the input did not come back untouched")
			}
		})
	}
}
