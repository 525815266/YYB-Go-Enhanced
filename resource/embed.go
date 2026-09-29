// Package resource contains the Web console assets embedded in standalone
// binaries. Runtime copies under resource/ remain writable and may override
// these defaults.
package resource

import "embed"

// WebAssets contains the static files and HTML templates required by the UI.
//
//go:embed static templates
var WebAssets embed.FS
