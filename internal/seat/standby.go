package seat

import (
	"context"
	"time"
)

// Standby stops a running seat that nobody is using, when the configuration
// asks for it, see config.IdleStopMinutes.
//
// Nothing here looks at a seat that the sweep does not already look at. Whether
// somebody is streaming is observeStream's, grace period included: a client
// that dropped a few seconds ago is still streaming as far as the clock below
// is concerned, which is why this needs no grace of its own. Whether a game is
// running is the library's idleProbe, the question the daemon asks before it
// replaces files a game might be reading, and it is asked here only when the
// clock has run out, not on every sweep.
//
// A seat that is stopped this way is stopped the way Stop stops it, broker and
// session first, and is started again the way a stopped seat always is. It
// cannot be woken from Moonlight: Sunshine is not running.

// standbyHooks are the three things standby asks of the world, so that a test
// can answer them. Nil in the daemon, where they are the real ones.
type standbyHooks struct {
	// streaming asks fresh whether somebody is connected, see Manager.streaming.
	streaming func(ctx context.Context, name string) bool

	// inUse asks whether anything in the seat has the shared library open or
	// mapped, see nothingUsing.
	inUse func(name string) bool

	// stop is Manager.Stop.
	stop func(name string) error
}

func (m *Manager) standbyOps() standbyHooks {
	h := standbyHooks{
		streaming: m.streaming,
		inUse:     func(name string) bool { return !m.nothingUsing(name, idleProbe) },
		stop:      m.Stop,
	}

	if m.standbyHooks == nil {
		return h
	}

	if m.standbyHooks.streaming != nil {
		h.streaming = m.standbyHooks.streaming
	}

	if m.standbyHooks.inUse != nil {
		h.inUse = m.standbyHooks.inUse
	}

	if m.standbyHooks.stop != nil {
		h.stop = m.standbyHooks.stop
	}

	return h
}

// standbyDue advances a seat's idle clock by one sweep and reports whether it
// has run for limit.
//
// idle is whether this sweep found nothing going on: the seat is up, nobody is
// streaming or may be, and no operation owns it. Anything else puts the clock
// back to zero, which is also how a seat that has just started, or has just
// been left by its last client, begins counting from that moment and not from
// when the daemon last looked.
//
// A limit of zero is off, and keeps the clock at zero.
//
// Called with the lock held.
func (rt *runtime) standbyDue(now time.Time, limit time.Duration, idle bool) bool {
	if limit <= 0 || !idle {
		rt.idleSince = time.Time{}

		return false
	}

	if rt.idleSince.IsZero() {
		rt.idleSince = now

		return false
	}

	return now.Sub(rt.idleSince) >= limit
}

// standby is the sweep's one call into this file. busy is the sweep's reading
// of the stream: somebody is streaming, or the seat did not say.
//
// It stops the seat through Stop, which refuses a seat an operation owns, so
// the worst a race with one costs is a log line.
func (m *Manager) standby(ctx context.Context, name string, up, busy bool) {
	limit := m.cfg.IdleStop()
	rt := m.runtimeOf(name)
	now := time.Now()

	m.mu.Lock()

	// A reading in the background lane is the daemon's own and over in seconds:
	// the clock waits for it rather than starting over, so that it cannot keep
	// a seat awake by being unlucky.
	if rt.apps.cancel != nil || rt.asking.cancel != nil {
		m.mu.Unlock()

		return
	}

	operated := rt.busy != "" || rt.state == StateStopping
	due := rt.standbyDue(now, limit, up && !busy && !operated)
	m.mu.Unlock()

	if !due || ctx.Err() != nil {
		return
	}

	ops := m.standbyOps()

	// The sweep decided some time ago and a client may have connected since.
	// Asked fresh, as every step that ends a stream is.
	if ops.streaming(ctx, name) || ctx.Err() != nil {
		m.standbyRestart(rt, now)

		return
	}

	// And the game, which is not a stream: Steam downloading, or a game left
	// open in the seat. Looked at once per interval and only now.
	if ops.inUse(name) {
		m.logf(name, "standby: something in the seat is using the game library, waiting another %d minutes",
			m.cfg.IdleStopMinutes)
		m.standbyRestart(rt, now)

		return
	}

	if ctx.Err() != nil {
		return
	}

	m.logf(name, "no client and no game for %d minutes, stopping (standby)", m.cfg.IdleStopMinutes)

	if err := ops.stop(name); err != nil {
		m.logf(name, "! standby could not stop the seat: %v", err)
	}

	m.standbyRestart(rt, now)
}

// standbyRestart starts a seat's idle clock over, from now.
func (m *Manager) standbyRestart(rt *runtime, now time.Time) {
	m.mu.Lock()
	rt.idleSince = now
	m.mu.Unlock()
}
