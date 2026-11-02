package seat

import "golang.org/x/sys/unix"

// landlockAvailable says whether the running kernel offers Landlock, which
// pacman 7's download sandbox needs. It asks the kernel for the ABI version,
// the documented way to probe without creating anything.
func landlockAvailable() bool {
	version, _, errno := unix.Syscall(unix.SYS_LANDLOCK_CREATE_RULESET, 0, 0, unix.LANDLOCK_CREATE_RULESET_VERSION)

	return errno == 0 && int(version) > 0
}
