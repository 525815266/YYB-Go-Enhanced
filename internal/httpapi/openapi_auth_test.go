package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestOpenAPIProtocolBearerAuthorization(t *testing.T) {
	const secret = "synthetic-secret-not-for-documentation"
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), ProtocolToken: secret})
	if err != nil {
		t.Fatal(err)
	}
	defer app.Close()
	recorder := httptest.NewRecorder()
	app.Handler().ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if recorder.Code != http.StatusOK || strings.Contains(recorder.Body.String(), secret) {
		t.Fatal("OpenAPI must remain readable without exposing the configured token")
	}
	var spec struct {
		Security   json.RawMessage `json:"security"`
		Components struct {
			SecuritySchemes map[string]struct {
				Type   string `json:"type"`
				Scheme string `json:"scheme"`
			} `json:"securitySchemes"`
		} `json:"components"`
		Paths map[string]map[string]struct {
			Security  []map[string][]string `json:"security"`
			Responses map[string]any        `json:"responses"`
		} `json:"paths"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &spec); err != nil {
		t.Fatal(err)
	}
	scheme, exists := spec.Components.SecuritySchemes["ProtocolBearer"]
	if !exists || scheme.Type != "http" || scheme.Scheme != "bearer" {
		t.Error("Swagger Authorize needs an HTTP Bearer security scheme")
	}
	if len(spec.Security) != 0 {
		t.Error("protocol authorization must not apply globally to browser management and public routes")
	}
	protocolCount := 0
	for path, methods := range spec.Paths {
		for method, operation := range methods {
			if strings.HasPrefix(path, "/wx/") || strings.HasPrefix(path, "/wxapp/") {
				protocolCount++
				if len(operation.Security) != 1 {
					t.Errorf("%s %s: protocol Bearer requirement missing", method, path)
					continue
				}
				if scopes, ok := operation.Security[0]["ProtocolBearer"]; !ok || len(scopes) != 0 {
					t.Errorf("%s %s: invalid Bearer requirement", method, path)
				}
				if _, ok := operation.Responses["401"]; !ok {
					t.Errorf("%s %s: missing unauthorized response", method, path)
				}
			} else if len(operation.Security) != 0 {
				t.Errorf("%s %s: protocol token must not be sent to non-protocol operations", method, path)
			}
		}
	}
	if protocolCount < 18 {
		t.Errorf("protocol operation coverage unexpectedly small: %d", protocolCount)
	}
}
