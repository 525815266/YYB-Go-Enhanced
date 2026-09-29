// Package resource contains the Web console assets embedded in standalone
// binaries. Standalone builds restore them under a versioned runtime directory;
// deployments with an explicit resource root can still provide writable files.
package resource

import "embed"

// WebAssets contains the static files and HTML templates required by the UI.
//
//go:embed static templates
var WebAssets embed.FS
