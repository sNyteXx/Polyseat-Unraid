package api

import (
	"io"
	"log/slog"
	"testing"

	"github.com/superuser404notfound/Polyseat/internal/update"
)

// The router checks its patterns against each other when they are registered,
// and two that overlap without either being the more specific one are a panic
// at startup. Nothing else builds the full router in a test, so without this
// a clashing route ships and the daemon never comes up, which v0.41.0 did.
func TestTheRouterBuilds(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("building the router panicked: %v", r)
		}
	}()
	New(nil, nil, update.New("v0.0.0", false, logger), nil, logger)
}
