package resource

import (
	"io/fs"
	"strings"
	"testing"
)

// Every page that loads the platform shell must also load the clean skin and
// its early switch, otherwise the skin button would only restyle some pages.
func TestShellPagesLoadTheSkin(t *testing.T) {
	entries, err := fs.ReadDir(WebAssets, "templates")
	if err != nil {
		t.Fatal(err)
	}
	checked := 0
	for _, entry := range entries {
		data, err := fs.ReadFile(WebAssets, "templates/"+entry.Name())
		if err != nil {
			t.Fatal(err)
		}
		page := string(data)
		if !strings.Contains(page, "/static/js/platform.js") {
			continue
		}
		checked++
		for _, want := range []string{"/static/css/skin-clean.css", `localStorage.getItem("yyb-skin")`} {
			if !strings.Contains(page, want) {
				t.Errorf("%s loads the platform shell but not %s", entry.Name(), want)
			}
		}
	}
	if checked == 0 {
		t.Fatal("no page loads the platform shell; the test lost its subject")
	}
	if _, err := fs.Stat(WebAssets, "static/css/skin-clean.css"); err != nil {
		t.Fatalf("the skin stylesheet is not embedded: %v", err)
	}
}
