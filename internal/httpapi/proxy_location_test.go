package httpapi

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestProxyLocationRecommendFromPublicIP(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/json/203.0.113.9":
			_, _ = w.Write([]byte(`{"status":"success","regionName":"山东","city":"潍坊市","query":"203.0.113.9"}`))
		case "/provinces":
			_, _ = w.Write([]byte(`{"data":[{"code":"370000","name":"山东省"}]}`))
		case "/cities":
			if r.URL.Query().Get("province") != "370000" {
				t.Fatalf("province query = %q", r.URL.Query().Get("province"))
			}
			_, _ = w.Write([]byte(`{"data":[{"code":"370700","name":"潍坊市"}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	setLocationEndpoints(t, upstream.URL+"/json/", upstream.URL+"/reverse", upstream.URL+"/provinces", upstream.URL+"/cities?province=")

	app := newLocationTestApp(t)
	defer app.Close()
	req := httptest.NewRequest(http.MethodPost, "/api/proxy-location/recommend", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-For", "203.0.113.9")
	recorder := httptest.NewRecorder()
	app.Handler().ServeHTTP(recorder, req)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		Data proxyLocation `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if response.Data.Source != "client_public_ip" || response.Data.ProvinceCode != "370000" || response.Data.CityCode != "370700" || !response.Data.Matched {
		t.Fatalf("location = %#v", response.Data)
	}
}

func TestProxyLocationRecommendFromBrowserCoordinatesFallsBackToProvince(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/reverse":
			if r.URL.Query().Get("lat") != "36.670000" || r.URL.Query().Get("lon") != "117.120000" {
				t.Fatalf("coordinates = %s,%s", r.URL.Query().Get("lat"), r.URL.Query().Get("lon"))
			}
			_, _ = w.Write([]byte(`{"address":{"state":"山东省","city":"不存在市"}}`))
		case "/provinces":
			_, _ = w.Write([]byte(`{"data":[{"code":"370000","name":"山东省"}]}`))
		case "/cities":
			_, _ = w.Write([]byte(`{"data":[{"code":"370100","name":"济南市"}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	setLocationEndpoints(t, upstream.URL+"/json/", upstream.URL+"/reverse", upstream.URL+"/provinces", upstream.URL+"/cities?province=")

	app := newLocationTestApp(t)
	defer app.Close()
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/proxy-location/recommend", strings.NewReader(`{"latitude":36.67,"longitude":117.12}`))
	request.Header.Set("Content-Type", "application/json")
	app.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		Data proxyLocation `json:"data"`
	}
	_ = json.Unmarshal(recorder.Body.Bytes(), &response)
	if response.Data.Source != "browser_geolocation" || response.Data.ProvinceCode != "370000" || response.Data.City != "" || response.Data.CityCode != "" {
		t.Fatalf("location = %#v", response.Data)
	}
}

func TestUnsupportedLoginProductDoesNotCreateSession(t *testing.T) {
	app := newLocationTestApp(t)
	defer app.Close()
	for _, path := range []string{"/qr", "/quick-login"} {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(`{"product":"pcmanager"}`))
		request.Header.Set("Content-Type", "application/json")
		app.Handler().ServeHTTP(recorder, request)
		if recorder.Code != http.StatusUnprocessableEntity || !strings.Contains(recorder.Body.String(), "尚未完成凭据兑换验证") {
			t.Fatalf("POST %s = %d %s", path, recorder.Code, recorder.Body.String())
		}
	}
}

func newLocationTestApp(t *testing.T) *App {
	t.Helper()
	t.Setenv("GIN_MODE", "test")
	app, err := NewApp(Config{ResourceRoot: t.TempDir(), RequestTimeout: time.Second, EnablePCLogin: true})
	if err != nil {
		t.Fatalf("NewApp() error = %v", err)
	}
	return app
}

func setLocationEndpoints(t *testing.T, ip, reverse, provinces, cities string) {
	t.Helper()
	oldIP, oldReverse := publicIPLocationEndpoint, reverseGeocodeEndpoint
	oldProvinces, oldCities := ipzanProvinceAreasEndpoint, ipzanCityAreasEndpoint
	publicIPLocationEndpoint, reverseGeocodeEndpoint = ip, reverse
	ipzanProvinceAreasEndpoint, ipzanCityAreasEndpoint = provinces, cities
	t.Cleanup(func() {
		publicIPLocationEndpoint, reverseGeocodeEndpoint = oldIP, oldReverse
		ipzanProvinceAreasEndpoint, ipzanCityAreasEndpoint = oldProvinces, oldCities
	})
}
