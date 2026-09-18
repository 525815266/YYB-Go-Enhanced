// Package version exposes build metadata without coupling the HTTP layer to
// the container or release tooling that produced the binary.
package version

import (
	"runtime/debug"
	"strings"
)

// These values are replaced by -ldflags in release builds. Keeping useful
// defaults makes source builds and locally copied binaries identifiable too.
var (
	Version   = "dev"
	Commit    = "unknown"
	BuildDate = "unknown"
)

// Info returns build metadata and uses Go's embedded VCS information when a
// binary was built directly from a checkout without explicit ldflags.
func Info() (string, string, string) {
	version, commit, buildDate := Version, Commit, BuildDate
	if build, ok := debug.ReadBuildInfo(); ok {
		for _, setting := range build.Settings {
			switch setting.Key {
			case "vcs.revision":
				if commit == "unknown" && setting.Value != "" {
					commit = setting.Value
				}
			case "vcs.time":
				if buildDate == "unknown" && setting.Value != "" {
					buildDate = setting.Value
				}
			}
		}
	}
	if strings.TrimSpace(version) == "" {
		version = "dev"
	}
	return version, commit, buildDate
}
