// Command polyseat-passwd writes polyseatd's credentials before its first start.
//
// Unraid port only (WIP). Upstream's daemon lets whoever opens the interface
// first choose the password. On Unraid it listens on the LAN from its first
// start, so that window must never open: the credentials exist before the
// daemon does, written by upstream's own auth package, so the format is
// exactly the one the daemon reads.
//
//	polyseat-passwd STATE_DIR USERNAME < password
package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/superuser404notfound/Polyseat/internal/auth"
)

func main() {
	if err := run(os.Args[1:], os.Stdin); err != nil {
		fmt.Fprintln(os.Stderr, "polyseat-passwd:", err)
		os.Exit(1)
	}
}

func run(args []string, in io.Reader) error {
	if len(args) != 2 || args[0] == "" || args[1] == "" {
		return fmt.Errorf("usage: polyseat-passwd STATE_DIR USERNAME < password")
	}

	line, err := bufio.NewReader(in).ReadString('\n')
	if err != nil && err != io.EOF {
		return err
	}

	password := strings.TrimRight(line, "\r\n")

	store, err := auth.Open(args[0])
	if err != nil {
		return err
	}

	return store.SetPassword(args[1], password)
}
