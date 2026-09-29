package httpapi

import (
	"net/http"
	"os"
	"path/filepath"
)

// The experimental SPA is isolated from legacy pages and protocol routes.
// Its public shell contains no account data; all management APIs retain auth.
func (a *App) handleConsole(w http.ResponseWriter, r *http.Request) {
	path := filepath.Join(a.resources.Static, "console", "index.html")
	if _, err := os.Stat(path); err != nil {
		writeError(w, http.StatusServiceUnavailable, "测试控制台未构建，请在 frontend 执行 npm ci && npm run build")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	http.ServeFile(w, r, path)
}
