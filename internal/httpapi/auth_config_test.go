package httpapi

import (
	"path/filepath"
	"testing"
)

func TestAuthDatabaseConfig(t *testing.T) {
	for _, tc := range []struct {
		name        string
		cfg         Config
		driver, dsn string
		wantErr     bool
	}{
		{"default path", Config{AuthDriver: "sqlite"}, "sqlite", filepath.Join("resource", "db", "auth.db"), false},
		{"custom root", Config{AuthDriver: "sqlite", ResourceRoot: "data"}, "sqlite", filepath.Join("data", "db", "auth.db"), false},
		{"explicit DSN", Config{AuthDriver: "sqlite", AuthDSN: "custom.db"}, "sqlite", "custom.db", false},
		{"legacy mysql", Config{AuthMySQLDSN: "legacy"}, "mysql", "legacy", false},
		{"mysql fallback", Config{AuthDriver: "mysql", AuthMySQLDSN: "legacy"}, "mysql", "legacy", false},
		{"mysql priority", Config{AuthDriver: "mysql", AuthDSN: "current", AuthMySQLDSN: "legacy"}, "mysql", "current", false},
		{"mysql missing", Config{AuthDriver: "mysql"}, "", "", true},
		{"none", Config{AuthDriver: "none", AuthMySQLDSN: "legacy"}, "none", "", false},
		{"library disabled", Config{}, "", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			driver, dsn, err := tc.cfg.AuthDatabase()
			if (err != nil) != tc.wantErr || driver != tc.driver || dsn != tc.dsn {
				t.Fatalf("AuthDatabase() = %q, %q, %v", driver, dsn, err)
			}
		})
	}
}
