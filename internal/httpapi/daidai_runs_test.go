package httpapi

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestDaidaiAccountHistoryAndLogOwnership(t *testing.T) {
	requests := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/open-api/token":
			fmt.Fprint(w, `{"data":{"access_token":"test"}}`)
		case "/api/v1/tasks":
			fmt.Fprint(w, `{"data":[{"id":10,"name":"[YYB:1] task","status":"idle","last_run_at":"2026-09-26T08:00:00Z"},{"id":20,"name":"other"}]}`)
		case "/api/v1/tasks/10/latest-log":
			requests++
			fmt.Fprint(w, `{"data":{"content":"finished task output"}}`)
		default:
			t.Errorf("unexpected request %s", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	app, handler, ref := newRunsTestApp(t, srv.URL)
	app.qinglong = newQingLongClient(PanelTypeDaidai, srv.URL, "key", "secret", time.Second)
	if _, err := app.db.UpsertAccountScriptJob(context.Background(), 1, "test.py", 10, "0 8 * * *"); err != nil {
		t.Fatal(err)
	}
	runs, err := app.accountRunHistory(context.Background(), 1)
	if err != nil || len(runs) != 1 || runs[0].LogKey != "daidai/10" || runs[0].StartedAt == 0 {
		t.Fatalf("%+v %v", runs, err)
	}
	result := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs/log?ref="+ref+"&log_key=daidai/10", nil)
	if result.Code != 200 || !strings.Contains(result.Body.String(), "finished task output") {
		t.Fatal(result.Body.String())
	}
	denied := apiRequest(t, handler, http.MethodGet, "/api/qinglong/runs/log?ref="+ref+"&log_key=daidai/20", nil)
	if denied.Code != 404 || requests != 1 {
		t.Fatal("foreign log was not rejected", denied.Code, requests)
	}
}
