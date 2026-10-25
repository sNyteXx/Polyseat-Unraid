package seat

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode"

	"github.com/superuser404notfound/Polyseat/internal/library"
)

// Carrying Lutris entries between the members.
//
// The pool moves a folder and nothing else, and for Steam that is enough: the
// appmanifest travels inside the library and Steam reads the library itself.
// Lutris keeps what it knows about a game outside the folder, as a row in
// pga.db and a YAML under ~/.config/lutris/games, so a folder that reached a
// seat was files that seat's Lutris had never heard of. The answer until now
// was that registering it is the folder's own job, which meant a hand written
// polyseat-setup.sh per game, and in practice that a game installed through
// Lutris did not appear anywhere else.
//
// So the daemon carries the entry. The member whose Lutris installed the game
// has one, and from it the daemon keeps a recipe: name, slug, runner, the
// program to start relative to the folder, and the few settings that say how.
// Wherever the folder arrives and nothing in that Lutris points into it, an
// entry is written from the recipe, as that member's own person.
//
// What is carried is a short list, see KEEP in lutrisshare.py. Lutris also
// runs commands around a launch and hands the game an environment, and neither
// travels: a folder can come from any seat, and the entry written here is one
// somebody else will start. The paths are held to the folder, so an entry
// written from a recipe starts the folder's own files and nothing else, which
// is exactly what a Steam game from the pool does. Nothing runs when an entry
// is written; that is why this needs no approval and polyseat-setup.sh does.
//
// A folder that carries a polyseat-setup.sh is left to it. Those exist because
// a game needed more than an entry, and the script writes its own.

// lutrisShareFile is where the recipes and the record of written entries are
// kept, beside the seat records.
const lutrisShareFile = "lutris-share.json"

// lutrisLimit bounds one call of the helper. It reads a small database and a
// few YAML files, or renames one directory, and takes a fraction of a second.
const lutrisLimit = 60 * time.Second

// lutrisSuffix is what the helper names its own entries by; see SUFFIX there.
const lutrisSuffix = "-polyseat"

// lutrisShareScript is the helper, run with python3 -I as the member's person.
var lutrisShareScript = string(asset("assets/lutrisshare.py"))

// lutrisRecipe is how one shared folder is started, as the member that
// installed it had it.
type lutrisRecipe struct {
	Name     string `json:"name"`
	Slug     string `json:"slug"`
	Runner   string `json:"runner"`
	Platform string `json:"platform,omitempty"`

	// Exe, Prefix and WorkingDir are relative to the folder. Prefix and
	// WorkingDir may be "." for the folder itself, and are empty when the
	// original pointed outside it.
	Exe        string `json:"exe"`
	Prefix     string `json:"prefix,omitempty"`
	WorkingDir string `json:"working_dir,omitempty"`

	Args        string `json:"args,omitempty"`
	Arch        string `json:"arch,omitempty"`
	WineVersion string `json:"wine_version,omitempty"`

	// From is the member it was read from, which is never written to from it.
	From string `json:"from"`
}

type lutrisShareState struct {
	// Recipes is keyed by folder name.
	Recipes map[string]lutrisRecipe `json:"recipes"`

	// Registered is keyed by member and then folder, with the configpath of
	// the entry written there. An empty configpath means the member already
	// had an entry of its own, or one by that slug, and was left alone; it is
	// kept so that the question is not asked every round.
	Registered map[string]map[string]string `json:"registered"`
}

// lutrisShares is the daemon's memory of all that, and of which members it
// has to look at again.
type lutrisShares struct {
	path string

	mu    sync.Mutex
	state lutrisShareState

	// everyone is set when every member has to be looked at, which is the case
	// at startup and after any folder moved. stale is the members still owed
	// a look once that has been handed out, which a seat that is switched off
	// stays until it is running.
	everyone bool
	stale    map[string]bool

	// hostStamp is what the host's Lutris looked like at the last round, so
	// that an entry added there by hand is noticed without a folder moving.
	hostStamp string

	// running is set while a round goes, see lutrisSoon.
	running bool

	// host is the last listing of the host's own Lutris for the interface,
	// with the stamp it was taken against.
	host      []HostLutrisGame
	hostShown string
}

func openLutrisShares(path string) (*lutrisShares, error) {
	s := &lutrisShares{path: path, everyone: true, stale: map[string]bool{}}
	s.state.Recipes = map[string]lutrisRecipe{}
	s.state.Registered = map[string]map[string]string{}

	if path == "" {
		return s, nil
	}

	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}

	if err != nil {
		return s, err
	}

	var state lutrisShareState
	if err := json.Unmarshal(data, &state); err != nil {
		return s, fmt.Errorf("%s: %w", path, err)
	}

	if state.Recipes != nil {
		s.state.Recipes = state.Recipes
	}

	if state.Registered != nil {
		s.state.Registered = state.Registered
	}

	return s, nil
}

// save writes the state out. Called with mu held.
func (s *lutrisShares) save() error {
	if s.path == "" {
		return nil
	}

	data, err := json.MarshalIndent(s.state, "", "  ")
	if err != nil {
		return err
	}

	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}

	return os.Rename(tmp, s.path)
}

// markAll says every member is owed a look.
func (s *lutrisShares) markAll() {
	s.mu.Lock()
	s.everyone = true
	s.mu.Unlock()
}

// owed hands out the members to look at this round, out of those given.
func (s *lutrisShares) owed(members []string) map[string]bool {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.everyone {
		for _, name := range members {
			s.stale[name] = true
		}

		s.everyone = false
	}

	known := map[string]bool{}
	for _, name := range members {
		known[name] = true
	}

	out := map[string]bool{}

	for name := range s.stale {
		if !known[name] {
			// A seat that left the pool or was deleted.
			delete(s.stale, name)

			continue
		}

		out[name] = true
	}

	return out
}

// done says one member has been looked at.
func (s *lutrisShares) done(member string) {
	s.mu.Lock()
	delete(s.stale, member)
	s.mu.Unlock()
}

// forgetRecipe drops a folder's recipe, for a folder taken out of the pool, so
// that one put back later is read from whoever installs it then.
func (s *lutrisShares) forgetRecipe(name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	delete(s.state.Recipes, name)
	s.everyone = true

	return s.save()
}

// forgetMember drops what was written into one member, for a seat that is
// deleted.
//
// The recipes read from it are kept, without an author: the folders are still
// in the pool and in the other seats, and a seat built after this one is gone
// should still find them in its Lutris. The next member to offer one of its
// own takes it over, see learn.
func (s *lutrisShares) forgetMember(member string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	delete(s.state.Registered, member)
	delete(s.stale, member)

	for name, recipe := range s.state.Recipes {
		if recipe.From == member {
			recipe.From = ""
			s.state.Recipes[name] = recipe
		}
	}

	s.everyone = true

	return s.save()
}

// learn takes recipes out of one member's scan.
//
// The first member to offer one for a folder keeps it, and only that member
// can change it afterwards. Whoever installed a game first is the one whose
// entry was made by Lutris's own installer; the entries this writes are left
// out of every scan, so the recipe never comes back round from a copy of
// itself.
func (s *lutrisShares) learn(member string, entries []lutrisEntry, pooled func(string) bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	changed := false

	for _, e := range entries {
		if e.Ours || e.Folder == "" || !pooled(e.Folder) {
			continue
		}

		recipe, err := e.recipe(member)
		if err != nil {
			continue
		}

		have, ok := s.state.Recipes[e.Folder]
		if ok && have.From != "" && have.From != member {
			continue
		}

		if ok && have == recipe {
			continue
		}

		s.state.Recipes[e.Folder] = recipe
		changed = true
	}

	if !changed {
		return nil
	}

	return s.save()
}

func (s *lutrisShares) recipes() map[string]lutrisRecipe {
	s.mu.Lock()
	defer s.mu.Unlock()

	out := make(map[string]lutrisRecipe, len(s.state.Recipes))
	for k, v := range s.state.Recipes {
		out[k] = v
	}

	return out
}

func (s *lutrisShares) registered(member string) map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()

	out := map[string]string{}
	for k, v := range s.state.Registered[member] {
		out[k] = v
	}

	return out
}

// note records what a member was given for a folder, or with forget, that it
// no longer has anything.
func (s *lutrisShares) note(member, folder, configpath string, forget bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if forget {
		delete(s.state.Registered[member], folder)

		if len(s.state.Registered[member]) == 0 {
			delete(s.state.Registered, member)
		}

		return s.save()
	}

	if s.state.Registered[member] == nil {
		s.state.Registered[member] = map[string]string{}
	}

	s.state.Registered[member][folder] = configpath

	return s.save()
}

// lutrisEntry is one game in a member's Lutris as the helper reports it.
type lutrisEntry struct {
	Name       string `json:"name"`
	Slug       string `json:"slug"`
	Runner     string `json:"runner"`
	Platform   string `json:"platform"`
	Configpath string `json:"configpath"`
	Ours       bool   `json:"ours"`

	// Folder is the shared folder its files are in, HomeFolder the directory
	// below the games root they are in, either empty when they are not.
	Folder     string `json:"folder"`
	HomeFolder string `json:"home_folder"`

	Exe         string `json:"exe"`
	Prefix      string `json:"prefix"`
	WorkingDir  string `json:"working_dir"`
	Args        string `json:"args"`
	Arch        string `json:"arch"`
	WineVersion string `json:"wine_version"`
}

type lutrisScanResult struct {
	Lutris bool          `json:"lutris"`
	Games  []lutrisEntry `json:"games"`
	Error  string        `json:"error"`
}

// lutrisSlug is what Lutris makes of a name: lower case, digits and dashes.
var lutrisSlug = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,99}$`)

// recipe checks an entry and makes a recipe of it.
//
// Every field is held to what Lutris itself would write, because what is read
// here can come out of any seat and goes into somebody else's Lutris. The paths
// above all: a relative path that stays inside the folder is the only kind
// accepted, so an entry made from it can start nothing but the folder's files.
func (e lutrisEntry) recipe(from string) (lutrisRecipe, error) {
	switch {
	case !lutrisSlug.MatchString(e.Slug):
		return lutrisRecipe{}, fmt.Errorf("slug %q", e.Slug)
	case strings.HasSuffix(e.Slug, lutrisSuffix):
		return lutrisRecipe{}, fmt.Errorf("slug %q is the daemon's own", e.Slug)
	case e.Runner != "wine" && e.Runner != "linux":
		return lutrisRecipe{}, fmt.Errorf("runner %q", e.Runner)
	case !plainText(e.Name, 200) || strings.TrimSpace(e.Name) == "":
		return lutrisRecipe{}, errors.New("name")
	case !insideFolder(e.Exe, false):
		return lutrisRecipe{}, fmt.Errorf("exe %q", e.Exe)
	case e.Prefix != "" && !insideFolder(e.Prefix, true):
		return lutrisRecipe{}, fmt.Errorf("prefix %q", e.Prefix)
	case e.WorkingDir != "" && !insideFolder(e.WorkingDir, true):
		return lutrisRecipe{}, fmt.Errorf("working_dir %q", e.WorkingDir)
	case !plainText(e.Args, 1000), !plainText(e.Platform, 100), !plainText(e.WineVersion, 200):
		return lutrisRecipe{}, errors.New("a field with control characters or too long")
	}

	switch e.Arch {
	case "", "auto", "win32", "win64":
	default:
		return lutrisRecipe{}, fmt.Errorf("arch %q", e.Arch)
	}

	return lutrisRecipe{
		Name: e.Name, Slug: e.Slug, Runner: e.Runner, Platform: e.Platform,
		Exe: e.Exe, Prefix: e.Prefix, WorkingDir: e.WorkingDir,
		Args: e.Args, Arch: e.Arch, WineVersion: e.WineVersion,
		From: from,
	}, nil
}

// insideFolder reports whether a path relative to a folder stays in it. dot
// is whether the folder itself counts, which it does for a directory and not
// for the program to start.
func insideFolder(rel string, dot bool) bool {
	if rel == "." {
		return dot
	}

	return rel != "" && filepath.IsLocal(rel) && plainText(rel, 4096)
}

// plainText reports whether s is short enough and has no control characters,
// which a YAML value or a path in somebody's configuration has no use for.
func plainText(s string, limit int) bool {
	if len(s) > limit {
		return false
	}

	for _, r := range s {
		if unicode.IsControl(r) {
			return false
		}
	}

	return true
}

// ------------------------------------------------------------------- rounds

// lutrisAfter is what a library pass hands on: any folder that moved means
// every member is owed a look, and so does a change to the host's Lutris.
func (m *Manager) lutrisAfter(report library.Report) {
	if m.lutris == nil {
		return
	}

	for _, moves := range [][]library.Move{report.Harvested, report.Delivered, report.Declined} {
		for _, move := range moves {
			if _, ok := library.FolderName(move.App); ok {
				m.lutris.markAll()
			}
		}
	}

	m.lutrisSoon()
}

// lutrisSoon starts a round unless one is going, off the caller's goroutine
// for the reason settleSoon gives.
func (m *Manager) lutrisSoon() {
	if m.lutris == nil || m.pool == nil {
		return
	}

	m.lutris.mu.Lock()

	if m.lutris.running {
		m.lutris.mu.Unlock()

		return
	}

	m.lutris.running = true
	m.lutris.mu.Unlock()

	go func() {
		defer func() {
			m.lutris.mu.Lock()
			m.lutris.running = false
			m.lutris.mu.Unlock()
		}()

		m.lutrisRound(context.Background())
	}()
}

// lutrisPlace is where one member keeps its folders, in the two forms the
// helper and the daemon need: the path as the member sees it and as the host
// does.
type lutrisPlace struct {
	member library.Member

	// root and shared are the games root and the shared directory as the
	// member's own processes name them.
	root, shared string

	// onHost is the shared directory on the host's filesystem, where the
	// daemon can list it without asking the member.
	onHost string
}

func (m *Manager) lutrisPlaces() []lutrisPlace {
	var out []lutrisPlace

	for _, member := range m.members(false) {
		onHost := m.pool.FoldersOf(member)
		if onHost == "" {
			continue
		}

		place := lutrisPlace{member: member, onHost: onHost}

		if member.Name == hostMember {
			place.root, place.shared = filepath.Dir(onHost), onHost
		} else {
			place.root, place.shared = LibraryMount, LibraryMount+"/shared"
		}

		out = append(out, place)
	}

	return out
}

// lutrisRound looks at every member owed a look: first what each one's Lutris
// has, so that a game installed anywhere has its recipe before anybody is
// given one, then what each one is missing.
func (m *Manager) lutrisRound(ctx context.Context) {
	places := m.lutrisPlaces()

	names := make([]string, 0, len(places))
	for _, p := range places {
		names = append(names, p.member.Name)
	}

	for _, p := range places {
		if p.member.Name != hostMember {
			continue
		}

		stamp := lutrisStamp(p.member)

		m.lutris.mu.Lock()
		if stamp != m.lutris.hostStamp {
			m.lutris.hostStamp = stamp
			m.lutris.everyone = true
		}
		m.lutris.mu.Unlock()
	}

	owed := m.lutris.owed(names)
	if len(owed) == 0 {
		return
	}

	pooled := func(name string) bool {
		info, err := os.Lstat(filepath.Join(m.pool.PoolFolders(), name))

		return err == nil && info.IsDir()
	}

	scans := map[string][]lutrisEntry{}

	for _, p := range places {
		if !owed[p.member.Name] || !m.lutrisReachable(p.member) {
			continue
		}

		result, err := m.lutrisScan(ctx, p)
		if err != nil {
			m.log.Warn("library: the Lutris of a member could not be read",
				"member", p.member.Name, "err", err)

			continue
		}

		if !result.Lutris {
			// Never started, so there is nothing to read and nowhere to write
			// yet. Asked again on a later round.
			continue
		}

		scans[p.member.Name] = result.Games

		if err := m.lutris.learn(p.member.Name, result.Games, pooled); err != nil {
			m.log.Warn("the record of carried Lutris entries could not be written", "err", err)
		}
	}

	recipes := m.lutris.recipes()

	for _, p := range places {
		entries, ok := scans[p.member.Name]
		if !ok {
			continue
		}

		if m.lutrisMember(ctx, p, entries, recipes) {
			m.lutris.done(p.member.Name)
		}
	}
}

// lutrisReachable reports whether the member can be asked now: the host
// always, a seat while it is running.
func (m *Manager) lutrisReachable(member library.Member) bool {
	if member.Name == hostMember {
		return member.Owner.UID >= minOwnerUID
	}

	status, err := m.client.Status(member.Name)

	return err == nil && status == "Running"
}

// lutrisMember gives one member the entries it is missing and takes back the
// ones whose folder it no longer has. It reports whether everything owed was
// settled, which a member whose Lutris could not be written to is not.
func (m *Manager) lutrisMember(ctx context.Context, p lutrisPlace, entries []lutrisEntry, recipes map[string]lutrisRecipe) bool {
	present := map[string]bool{}

	list, err := os.ReadDir(p.onHost)
	if err != nil {
		return false
	}

	for _, d := range list {
		// IsDir off the directory entry, which does not follow a link.
		if d.IsDir() {
			present[d.Name()] = true
		}
	}

	covered := map[string]bool{}
	for _, e := range entries {
		if e.Folder != "" {
			covered[e.Folder] = true
		}
	}

	settled := true
	given := m.lutris.registered(p.member.Name)

	for folder, configpath := range given {
		if present[folder] {
			continue
		}

		if configpath != "" {
			res, err := m.lutrisCall(ctx, p, "unregister", p.shared, folder, configpath)
			if err != nil {
				m.moved(p.member.Name, "! the Lutris entry for %s could not be removed: %v", folder, err)

				settled = false

				continue
			}

			if res.Status == "removed" {
				m.moved(p.member.Name, "%s is gone, and so is its Lutris entry", folder)
			}
		}

		if err := m.lutris.note(p.member.Name, folder, "", true); err != nil {
			m.log.Warn("the record of carried Lutris entries could not be written", "err", err)
		}
	}

	folders := make([]string, 0, len(present))
	for name := range present {
		folders = append(folders, name)
	}

	sort.Strings(folders)

	for _, folder := range folders {
		recipe, ok := recipes[folder]
		if !ok || recipe.From == p.member.Name || covered[folder] {
			continue
		}

		if _, ok := given[folder]; ok {
			// Written once already, or left alone once. An entry somebody
			// removed by hand stays removed.
			continue
		}

		if runnable(filepath.Join(p.onHost, folder, folderSetup)) {
			continue
		}

		arg, err := json.Marshal(struct {
			lutrisRecipe
			Folder string `json:"folder"`
		}{recipe, folder})
		if err != nil {
			continue
		}

		res, err := m.lutrisCall(ctx, p, "register", p.shared, string(arg))
		if err != nil {
			m.moved(p.member.Name, "! %s could not be added to Lutris: %v", folder, err)

			settled = false

			continue
		}

		switch res.Status {
		case "added":
			m.moved(p.member.Name, "%s was added to Lutris as %s", folder, recipe.Name)

			if err := m.lutris.note(p.member.Name, folder, res.Configpath, false); err != nil {
				m.log.Warn("the record of carried Lutris entries could not be written", "err", err)
			}

		case "present", "slug-taken":
			if err := m.lutris.note(p.member.Name, folder, "", false); err != nil {
				m.log.Warn("the record of carried Lutris entries could not be written", "err", err)
			}

		case "no-lutris":
			settled = false
		}
	}

	return settled
}

// lutrisResult is what register, unregister and move answer.
type lutrisResult struct {
	Status     string `json:"status"`
	Configpath string `json:"configpath"`
	Folder     string `json:"folder"`
	Error      string `json:"error"`
}

func (m *Manager) lutrisScan(ctx context.Context, p lutrisPlace) (lutrisScanResult, error) {
	var result lutrisScanResult

	out, err := m.lutrisRun(ctx, p, "scan", p.root, p.shared)
	if err != nil {
		return result, err
	}

	if err := json.Unmarshal([]byte(lastLine(out)), &result); err != nil {
		return result, fmt.Errorf("the helper answered with something that is not a scan: %w", err)
	}

	if result.Error != "" {
		return result, errors.New(result.Error)
	}

	return result, nil
}

func (m *Manager) lutrisCall(ctx context.Context, p lutrisPlace, args ...string) (lutrisResult, error) {
	var result lutrisResult

	out, err := m.lutrisRun(ctx, p, args...)
	if err != nil {
		return result, err
	}

	if err := json.Unmarshal([]byte(lastLine(out)), &result); err != nil {
		return result, fmt.Errorf("the helper answered with something that is not an answer: %w", err)
	}

	if result.Error != "" {
		return result, errors.New(result.Error)
	}

	return result, nil
}

// lutrisRun runs the helper as the member's person: in a seat as the player,
// on the host as the owner of the library and never as root.
func (m *Manager) lutrisRun(ctx context.Context, p lutrisPlace, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, lutrisLimit)
	defer cancel()

	if p.member.Name != hostMember {
		out, code, err := look(ctx, m.client, p.member.Name, lutrisLimit,
			pythonScan(lutrisLimit, lutrisShareScript, args...)...)
		if err != nil {
			return out, err
		}

		if code != 0 && lastLine(out) == "" {
			return out, fmt.Errorf("the helper exited %d", code)
		}

		return out, nil
	}

	owner := p.member.Owner
	if owner.UID < minOwnerUID {
		return "", fmt.Errorf("%w: the library belongs to uid %d", errSystemOwner, owner.UID)
	}

	who := ownerOf(owner.UID)
	if who == nil || who.HomeDir == "" {
		return "", fmt.Errorf("uid %d has no passwd entry to take a home from", owner.UID)
	}

	env := []string{
		"HOME=" + who.HomeDir,
		"USER=" + who.Username,
		"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
	}

	argv := append([]string{"python3", "-I", "-c", lutrisShareScript}, args...)

	out, err := runBounded(ctx, who.HomeDir, argv, env, &syscall.Credential{
		Uid: uint32(owner.UID),
		Gid: uint32(owner.GID),
	})

	// The helper prints its own refusal and exits non-zero with it; that is an
	// answer, not a failure to run.
	if err != nil && lastLine(out) == "" {
		return out, err
	}

	return out, nil
}

// lutrisStamp is what changes when the host's Lutris does: its database and
// the directory of game configurations.
func lutrisStamp(member library.Member) string {
	who := ownerOf(member.Owner.UID)
	if who == nil {
		return ""
	}

	var b strings.Builder

	for _, path := range []string{
		filepath.Join(who.HomeDir, ".local/share/lutris/pga.db"),
		filepath.Join(who.HomeDir, ".config/lutris/games"),
	} {
		info, err := os.Stat(path)
		if err != nil {
			b.WriteString("-;")

			continue
		}

		b.WriteString(strconv.FormatInt(info.ModTime().UnixNano(), 10))
		b.WriteString(":")
		b.WriteString(strconv.FormatInt(info.Size(), 10))
		b.WriteString(";")
	}

	return b.String()
}

// ---------------------------------------------------------------- interface

// HostLutrisGame is one game in the host's own Lutris, as the Library section
// shows it.
type HostLutrisGame struct {
	Slug string `json:"slug"`
	Name string `json:"name"`

	// Folder is the directory its files are in: below ~/Games/shared when
	// Shared is set, below ~/Games otherwise.
	Folder string `json:"folder,omitempty"`
	Shared bool   `json:"shared"`

	// Problem says why it cannot be shared by moving it, when it cannot.
	Problem string `json:"problem,omitempty"`
}

// lutrisMoveRefused are the directories below ~/Games that are never moved,
// because they are not one game. umu keeps the default prefix every Proton
// game without one of its own runs in.
var lutrisMoveRefused = map[string]bool{"shared": true, "umu": true}

// hostLutrisGames lists the host's Lutris games for the interface.
//
// The interface reads the library on every change the daemon pushes, so the
// listing is taken again only when the host's Lutris has changed since.
func (m *Manager) hostLutrisGames() []HostLutrisGame {
	out := []HostLutrisGame{}

	if m.pool == nil || m.lutris == nil {
		return out
	}

	var place *lutrisPlace

	for _, p := range m.lutrisPlaces() {
		if p.member.Name == hostMember {
			place = &p

			break
		}
	}

	if place == nil || place.member.Owner.UID < minOwnerUID {
		return out
	}

	stamp := lutrisStamp(place.member) + place.shared

	m.lutris.mu.Lock()
	if stamp == m.lutris.hostShown && m.lutris.host != nil {
		games := m.lutris.host
		m.lutris.mu.Unlock()

		return games
	}
	m.lutris.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	result, err := m.lutrisScan(ctx, *place)
	if err != nil || !result.Lutris {
		return out
	}

	for _, e := range result.Games {
		if e.Ours {
			continue
		}

		game := HostLutrisGame{Slug: e.Slug, Name: e.Name}

		switch {
		case e.Folder != "":
			game.Folder, game.Shared = e.Folder, true
		case e.HomeFolder == "":
			game.Problem = "its files are not in a folder of their own below " + place.root
		case lutrisMoveRefused[e.HomeFolder]:
			game.Folder = e.HomeFolder
			game.Problem = "it lives in " + filepath.Join(place.root, e.HomeFolder) +
				", which is more than this one game"
		default:
			game.Folder = e.HomeFolder
		}

		out = append(out, game)
	}

	sort.Slice(out, func(i, j int) bool { return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name) })

	m.lutris.mu.Lock()
	m.lutris.host, m.lutris.hostShown = out, stamp
	m.lutris.mu.Unlock()

	return out
}

// ErrNoSharedDir is returned when the host has no ~/Games/shared to share
// into, which is the switch for the host's side of the folders.
var ErrNoSharedDir = errors.New("the host takes no part in the shared folders: create ~/Games/shared first")

// ShareHostLutrisGame moves one game of the host's Lutris into ~/Games/shared,
// points its entries at the new place, and runs a pass so it starts on its way
// to the seats.
//
// A rename and never a copy: the directory and ~/Games/shared are on one
// filesystem, the second being inside the first's parent, and a game is only
// ever moved while nothing on the host has its files open.
func (m *Manager) ShareHostLutrisGame(ctx context.Context, slug string) error {
	if m.pool == nil || m.lutris == nil {
		return fmt.Errorf("%w: %s", ErrNoLibrary, m.libraryErr)
	}

	if !lutrisSlug.MatchString(slug) {
		return fmt.Errorf("%q is not a Lutris slug", slug)
	}

	var place *lutrisPlace

	for _, p := range m.lutrisPlaces() {
		if p.member.Name == hostMember {
			place = &p

			break
		}
	}

	if place == nil {
		return ErrNoSharedDir
	}

	m.syncMu.Lock()

	result, err := m.lutrisScan(ctx, *place)
	if err != nil {
		m.syncMu.Unlock()

		return err
	}

	var game *lutrisEntry

	for i := range result.Games {
		if result.Games[i].Slug == slug && !result.Games[i].Ours {
			game = &result.Games[i]

			break
		}
	}

	switch {
	case game == nil:
		err = fmt.Errorf("%w: Lutris on this host has no game %s", os.ErrNotExist, slug)
	case game.Folder != "":
		err = fmt.Errorf("%s is shared already, from %s", slug, game.Folder)
	case game.HomeFolder == "" || lutrisMoveRefused[game.HomeFolder]:
		err = fmt.Errorf("%s is not in a folder of its own below %s", slug, place.root)
	case !hostIdle(filepath.Join(place.root, game.HomeFolder), place.member.Owner.UID):
		err = fmt.Errorf("%w: something on this host is using %s", ErrBusy, game.HomeFolder)
	}

	if err != nil {
		m.syncMu.Unlock()

		return err
	}

	res, err := m.lutrisCall(ctx, *place, "move", place.root, place.shared, slug)

	m.syncMu.Unlock()

	if err != nil {
		return err
	}

	m.log.Info("library: a Lutris game was moved into the shared folders",
		"game", slug, "folder", res.Folder)

	m.lutris.markAll()
	m.notify()

	go m.syncLibrary(context.Background())

	return nil
}
