package httpapi

import (
	"fmt"
	"path/filepath"
	"strings"
)

// AuthDatabase resolves the same database for the server and local recovery.
// An empty driver keeps the library's existing authentication-disabled behavior.
func (cfg Config) AuthDatabase() (driver, dsn string, err error) {
	driver = strings.ToLower(strings.TrimSpace(cfg.AuthDriver))
	dsn = strings.TrimSpace(cfg.AuthDSN)
	if driver == "" && cfg.AuthMySQLDSN != "" {
		driver, dsn = "mysql", cfg.AuthMySQLDSN
	}
	if driver == "" || driver == "none" {
		return driver, "", nil
	}
	if driver == "mysql" && dsn == "" {
		dsn = cfg.AuthMySQLDSN
	}
	if driver == "sqlite" && dsn == "" {
		root := cfg.ResourceRoot
		if root == "" {
			root = "resource"
		}
		dsn = filepath.Join(root, "db", "auth.db")
	}
	if dsn == "" {
		return "", "", fmt.Errorf("auth %s DSN is empty", driver)
	}
	return driver, dsn, nil
}
