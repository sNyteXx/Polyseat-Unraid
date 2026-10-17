package seat

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

// Named in three places that nothing but this ties together, the same way the
// Sunshine package is: the version and build here, the recipe, and the
// checksum of what the recipe produced.
func TestTheMangoHudShimPackageIsTheOneTheRecipeBuilds(t *testing.T) {
	recipe, err := os.ReadFile("../../packaging/mangohud-shim/PKGBUILD")
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

	if got := field("pkgname"); got != "polyseat-mangohud-shim" {
		t.Errorf("the recipe builds %q, and stepMangoHudShim asks pacman for polyseat-mangohud-shim", got)
	}

	if got := field("pkgver"); got != MangoHudShimVersion {
		t.Errorf("the recipe builds %q and seats ask for %q", got, MangoHudShimVersion)
	}

	if got := field("pkgrel"); got != MangoHudShimBuild {
		t.Errorf("the recipe builds pkgrel %q and seats ask for %q", got, MangoHudShimBuild)
	}

	if !strings.Contains(string(recipe), "shim-rtld-next.patch") {
		t.Error("the recipe does not apply the patch, which is the whole reason it exists")
	}

	if _, err := os.Stat("../../packaging/mangohud-shim/shim-rtld-next.patch"); err != nil {
		t.Errorf("the patch the recipe names is missing: %v", err)
	}

	// Where the recipe puts the shim is where every game is told to load it
	// from, with $LIB standing for lib and lib32.
	for _, lib := range []string{"lib", "lib32"} {
		path := strings.ReplaceAll(mangoHudPreload, "$LIB", lib)
		if !strings.Contains(string(recipe), `"$pkgdir`+path+`"`) {
			t.Errorf("the recipe installs no %s, which is what games preload", path)
		}
	}

	// An absolute link resolves inside the Steam Linux Runtime's container,
	// where the seat's /usr is /run/host/usr, and a native game from Steam then
	// runs with the shim and without MangoHud.
	if regexp.MustCompile(`(?m)^\s*ln -s /`).Match(recipe) {
		t.Error("the recipe links libMangoHud_opengl.so absolutely")
	}
}

func TestTheMangoHudShimPackageHasAnAddressAndAChecksum(t *testing.T) {
	v := MangoHudShimVersion + "-" + MangoHudShimBuild
	want := "https://github.com/superuser404notfound/Polyseat/releases/download/mangohud-shim-" +
		v + "/polyseat-mangohud-shim-" + v + "-x86_64.pkg.tar.zst"

	if got := mangoHudShimPackageURL(); got != want {
		t.Errorf("the package is fetched from %q, want %q", got, want)
	}

	if !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(mangoHudShimPackageSHA256) {
		t.Errorf("%q is not a sha256 the way installOwnPackage writes one", mangoHudShimPackageSHA256)
	}
}

func TestOnlyThisMangoHudShimBuildIsCurrent(t *testing.T) {
	v := MangoHudShimVersion + "-" + MangoHudShimBuild

	for out, current := range map[string]bool{
		"polyseat-mangohud-shim " + v:                          true,
		"polyseat-mangohud-shim " + MangoHudShimVersion + "-0": false,
		"mangohud " + v: false,
		"error: package 'polyseat-mangohud-shim' was not found": false,
	} {
		if got := mangoHudShimIsCurrent(out); got != current {
			t.Errorf("%q read as current=%v", out, got)
		}
	}
}
