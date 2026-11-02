package seat

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// Named in three places that nothing but this ties together, the same way the
// other two packages are: the version and build here, the recipe, and the
// checksum of what the recipe produced.
func TestTheSteviaPackageIsTheOneTheRecipeBuilds(t *testing.T) {
	recipe, err := os.ReadFile("../../packaging/stevia/PKGBUILD")
	if err != nil {
		t.Fatalf("the recipe is not where the source says it is: %v", err)
	}

	field := func(name string) string {
		m := regexp.MustCompile(`(?m)^` + name + `=(\S+)`).FindSubmatch(recipe)
		if m == nil {
			t.Fatalf("the recipe sets no %s", name)
		}

		return string(m[1])
	}

	if got := field("pkgname"); got != "polyseat-stevia" {
		t.Errorf("the recipe builds %q, and stepStevia asks pacman for polyseat-stevia", got)
	}

	if got := field("pkgver"); got != SteviaVersion {
		t.Errorf("the recipe builds %q and seats ask for %q", got, SteviaVersion)
	}

	if got := field("pkgrel"); got != SteviaBuild {
		t.Errorf("the recipe builds pkgrel %q and seats ask for %q", got, SteviaBuild)
	}

	if _, err := os.Stat("../../packaging/stevia/sway.patch"); err != nil {
		t.Errorf("the patch the recipe names is missing: %v", err)
	}

	// A seat built before this has squeekboard, and pacman -U --noconfirm
	// answers no to a conflict, so the install would fail on every one of them.
	if regexp.MustCompile(`(?m)^(conflicts|replaces)=`).Match(recipe) {
		t.Error("the recipe conflicts with something, which stops it installing on an existing seat")
	}
}

func TestTheSteviaPackageHasAnAddressAndAChecksum(t *testing.T) {
	v := SteviaVersion + "-" + SteviaBuild
	want := "https://github.com/superuser404notfound/Polyseat/releases/download/stevia-" +
		v + "/polyseat-stevia-" + v + "-x86_64.pkg.tar.zst"

	if got := steviaPackageURL(); got != want {
		t.Errorf("the package is looked for at %s, want %s", got, want)
	}

	if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(steviaPackageSHA256) {
		t.Errorf("%q is not a sha256", steviaPackageSHA256)
	}
}

func TestSteviaIsCurrentOnlyAtThisVersionAndBuild(t *testing.T) {
	want := SteviaVersion + "-" + SteviaBuild

	for out, current := range map[string]bool{
		"polyseat-stevia " + want + "\n": true,
		"polyseat-stevia 0.1.0-1\n":      false,
		"stevia " + want + "\n":          false,
		"":                               false,
	} {
		if got := steviaIsCurrent(out); got != current {
			t.Errorf("steviaIsCurrent(%q) = %v, want %v", out, got, current)
		}
	}
}

// The keyboard takes its language from the seat, and the seat's language is
// what the sway config says. Run rather than read, with stubs for what the
// script talks to, because what has to hold is what gsettings is really handed.
func TestTheKeyboardIsToldTheLanguageOfTheSeat(t *testing.T) {
	for name, tc := range map[string]struct {
		config string
		want   string
	}{
		"one layout":   {"input * {\n    xkb_layout de\n}\n", "[('xkb', 'de')]"},
		"two layouts":  {"input * {\n    xkb_layout de,us\n}\n", "[('xkb', 'de'), ('xkb', 'us')]"},
		"with variant": {"input * {\n    xkb_layout fr\n    xkb_variant azerty\n}\n", "[('xkb', 'fr')]"},
		"no layout":    {"# no keyboard here\n", ""},
	} {
		t.Run(name, func(t *testing.T) {
			home := t.TempDir()
			bin := filepath.Join(home, "bin")
			asked := filepath.Join(home, "asked")

			for _, dir := range []string{bin, filepath.Join(home, ".config/sway")} {
				if err := os.MkdirAll(dir, 0o755); err != nil {
					t.Fatal(err)
				}
			}

			if err := os.WriteFile(filepath.Join(home, ".config/sway/config"), []byte(tc.config), 0o644); err != nil {
				t.Fatal(err)
			}

			started := filepath.Join(home, "started")

			stubs := map[string]string{
				"gsettings": "#!/bin/sh\necho \"$4\" > " + asked + "\n",
				// Not running, so show has something to do.
				"pgrep":            "#!/bin/sh\nexit 1\n",
				"phosh-osk-stevia": "#!/bin/sh\nenv > " + started + "\n",
			}

			for name, body := range stubs {
				if err := os.WriteFile(filepath.Join(bin, name), []byte(body), 0o755); err != nil {
					t.Fatal(err)
				}
			}

			script := filepath.Join(home, "keyboard.sh")
			if err := os.WriteFile(script, asset("assets/keyboard.sh"), 0o755); err != nil {
				t.Fatal(err)
			}

			cmd := exec.Command("/bin/sh", script)
			cmd.Env = []string{"HOME=" + home, "PATH=" + bin + ":/usr/bin:/bin", "XDG_RUNTIME_DIR=" + home}

			if out, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("the script failed: %v\n%s", err, out)
			}

			environment := string(waitFor(t, started))
			if !strings.Contains(environment, "GTK_THEME=Adwaita:dark") {
				t.Error("the keyboard was started in the light style")
			}

			got, err := os.ReadFile(asked)

			switch {
			case tc.want == "" && err == nil:
				t.Errorf("the keyboard was told %q by a seat with no layout", strings.TrimSpace(string(got)))
			case tc.want != "" && err != nil:
				t.Error("the keyboard was never told the language, so it stays on US")
			case tc.want != "" && strings.TrimSpace(string(got)) != tc.want:
				t.Errorf("the keyboard was told %q, want %q", strings.TrimSpace(string(got)), tc.want)
			}
		})
	}
}

// Pressing it again is what closes it, because a gamepad has the one button.
func TestTheKeyboardIsEndedWhenItIsAlreadyOpen(t *testing.T) {
	home := t.TempDir()
	bin := filepath.Join(home, "bin")
	ended := filepath.Join(home, "ended")

	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}

	stubs := map[string]string{
		"pgrep":            "#!/bin/sh\nexit 0\n",
		"pkill":            "#!/bin/sh\necho \"$2\" > " + ended + "\n",
		"phosh-osk-stevia": "#!/bin/sh\necho started >> " + ended + ".start\n",
	}

	for name, body := range stubs {
		if err := os.WriteFile(filepath.Join(bin, name), []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}

	script := filepath.Join(home, "keyboard.sh")
	if err := os.WriteFile(script, asset("assets/keyboard.sh"), 0o755); err != nil {
		t.Fatal(err)
	}

	cmd := exec.Command("/bin/sh", script)
	cmd.Env = []string{"HOME=" + home, "PATH=" + bin + ":/usr/bin:/bin", "XDG_RUNTIME_DIR=" + home}

	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("the script failed: %v\n%s", err, out)
	}

	got, err := os.ReadFile(ended)
	if err != nil {
		t.Fatal("the keyboard was left running, so a second press does not close it")
	}

	// The kernel keeps fifteen characters, and -x against the whole name of
	// this program matches nothing, in a way a stub cannot show. This does.
	if name := strings.TrimSpace(string(got)); name != "phosh-osk-stevi" {
		t.Errorf("the keyboard is looked for as %q, which is not what the kernel calls it", name)
	}

	if _, err := os.Stat(ended + ".start"); err == nil {
		t.Error("a second press started another keyboard")
	}
}
