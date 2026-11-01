package seat

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/superuser404notfound/Polyseat/internal/config"
)

// The clock itself, with the time injected: a step is one sweep.
func TestStandbyClock(t *testing.T) {
	t0 := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	limit := 30 * time.Minute

	type step struct {
		at   time.Duration
		idle bool
		want bool
	}

	cases := []struct {
		name  string
		limit time.Duration
		steps []step
	}{
		{"off never fires", 0, []step{
			{0, true, false}, {24 * time.Hour, true, false},
		}},
		{"the first idle sweep only starts the count", limit, []step{
			{0, true, false},
		}},
		{"fires once the limit has run", limit, []step{
			{0, true, false}, {29 * time.Minute, true, false},
			{30 * time.Minute, true, true}, {31 * time.Minute, true, true},
		}},
		{"activity starts the count over", limit, []step{
			{0, true, false}, {20 * time.Minute, false, false},
			{21 * time.Minute, true, false}, {50 * time.Minute, true, false},
			{51 * time.Minute, true, true},
		}},
		{"a single busy sweep at the end is enough to hold it", limit, []step{
			{0, true, false}, {29 * time.Minute, false, false},
			{30 * time.Minute, true, false}, {59 * time.Minute, true, false},
		}},
	}

	for _, tc := range cases {
		rt := &runtime{}

		for i, st := range tc.steps {
			got := rt.standbyDue(t0.Add(st.at), tc.limit, st.idle)
			if got != st.want {
				t.Errorf("%s: step %d (%v, idle=%v): due is %v, want %v",
					tc.name, i, st.at, st.idle, got, st.want)
			}
		}
	}

	// Off: the count is not kept either, so that turning it on later does not
	// start from a moment when nobody asked.
	rt := &runtime{idleSince: t0}
	rt.standbyDue(t0.Add(time.Hour), 0, true)

	if !rt.idleSince.IsZero() {
		t.Error("a limit of zero left a count running")
	}
}

type standbyRig struct {
	m         *Manager
	rt        *runtime
	stopped   []string
	streaming bool
	inUse     bool
	stopErr   error
	asked     int
}

func newStandbyRig(t *testing.T, minutes int) *standbyRig {
	t.Helper()

	r := &standbyRig{}

	r.m = &Manager{
		cfg: config.Config{IdleStopMinutes: minutes},
		rt:  map[string]*runtime{}, subs: map[int]chan struct{}{},
		log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	r.m.standbyHooks = &standbyHooks{
		streaming: func(context.Context, string) bool { r.asked++; return r.streaming },
		inUse:     func(string) bool { return r.inUse },
		stop:      func(name string) error { r.stopped = append(r.stopped, name); return r.stopErr },
	}
	r.rt = r.m.runtimeOf("vince")

	return r
}

// back dates the count, which is the same as the seat having been idle for
// that long.
func (r *standbyRig) idleFor(d time.Duration) {
	r.m.mu.Lock()
	r.rt.idleSince = time.Now().Add(-d)
	r.m.mu.Unlock()
}

func (r *standbyRig) logged(want string) bool {
	for _, l := range r.rt.log.Lines() {
		if strings.Contains(l, want) {
			return true
		}
	}

	return false
}

func TestStandbyStopsAnIdleSeat(t *testing.T) {
	r := newStandbyRig(t, 30)

	// First sweep starts the count; it does not stop anything.
	r.m.standby(context.Background(), "vince", true, false)

	if len(r.stopped) != 0 || r.rt.idleSince.IsZero() {
		t.Fatalf("the first sweep stopped %v or did not start the count", r.stopped)
	}

	r.idleFor(29 * time.Minute)
	r.m.standby(context.Background(), "vince", true, false)

	if len(r.stopped) != 0 {
		t.Fatalf("stopped after 29 of 30 minutes")
	}

	r.idleFor(31 * time.Minute)
	r.m.standby(context.Background(), "vince", true, false)

	if len(r.stopped) != 1 || r.stopped[0] != "vince" {
		t.Fatalf("stopped %v after 31 of 30 minutes, want vince once", r.stopped)
	}

	if !r.logged("no client and no game for 30 minutes, stopping (standby)") {
		t.Errorf("the seat's log does not say why: %v", r.rt.log.Lines())
	}
}

func TestStandbyLeavesSeatsAlone(t *testing.T) {
	cases := []struct {
		name  string
		setup func(r *standbyRig)
		up    bool
		busy  bool
	}{
		{"somebody is streaming, or may be", nil, true, true},
		{"the seat is not up", nil, false, false},
		{"it is being operated on", func(r *standbyRig) { r.rt.busy = "provisioning" }, true, false},
		{"it is being stopped", func(r *standbyRig) { r.rt.state = StateStopping }, true, false},
		{"a client connected since the sweep decided", func(r *standbyRig) { r.streaming = true }, true, false},
		{"a game or a download is using the library", func(r *standbyRig) { r.inUse = true }, true, false},
		{"the app list is being rebuilt", func(r *standbyRig) { r.rt.apps.cancel = func() {} }, true, false},
		{"it is being asked what it is behind on", func(r *standbyRig) { r.rt.asking.cancel = func() {} }, true, false},
	}

	for _, tc := range cases {
		r := newStandbyRig(t, 30)

		if tc.setup != nil {
			tc.setup(r)
		}

		r.idleFor(2 * time.Hour)
		r.m.standby(context.Background(), "vince", tc.up, tc.busy)

		if len(r.stopped) != 0 {
			t.Errorf("%s: the seat was stopped", tc.name)
		}
	}
}

func TestStandbyCountsAgainAfterActivity(t *testing.T) {
	// A game in use holds it for another full interval, not for another sweep.
	r := newStandbyRig(t, 30)
	r.inUse = true
	r.idleFor(time.Hour)
	r.m.standby(context.Background(), "vince", true, false)

	r.inUse = false
	r.m.standby(context.Background(), "vince", true, false)

	if len(r.stopped) != 0 {
		t.Error("a seat whose game just closed was stopped on the next sweep")
	}

	// A stream that comes and goes puts the count to zero.
	r = newStandbyRig(t, 30)
	r.idleFor(time.Hour)
	r.m.standby(context.Background(), "vince", true, true)
	r.m.standby(context.Background(), "vince", true, false)

	if len(r.stopped) != 0 {
		t.Error("a seat that was streaming a sweep ago was stopped")
	}

	// The fresh question is only asked once the count has run out.
	r = newStandbyRig(t, 30)
	r.m.standby(context.Background(), "vince", true, false)

	if r.asked != 0 {
		t.Error("a seat with minutes left was probed")
	}
}

func TestStandbyOffDoesNothing(t *testing.T) {
	r := newStandbyRig(t, 0)
	r.idleFor(48 * time.Hour)
	r.m.standby(context.Background(), "vince", true, false)

	if len(r.stopped) != 0 || r.asked != 0 {
		t.Error("standby acted while switched off")
	}
}

func TestStandbyReportsAStopThatFailed(t *testing.T) {
	r := newStandbyRig(t, 30)
	r.stopErr = errors.New("seat is busy")
	r.idleFor(time.Hour)
	r.m.standby(context.Background(), "vince", true, false)

	if !r.logged("could not stop") {
		t.Errorf("a failed stop left no line: %v", r.rt.log.Lines())
	}
}

func TestForgetStreamClearsTheCount(t *testing.T) {
	r := newStandbyRig(t, 30)
	r.idleFor(time.Hour)
	r.m.forgetStream("vince")

	if !r.rt.idleSince.IsZero() {
		t.Error("a stopped seat kept its idle count")
	}
}
