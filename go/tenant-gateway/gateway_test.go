package gateway_test

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	gateway "github.com/seanheiney/boss/go/tenant-gateway"
)

const secretToken = "super-secret-token-do-not-echo"

func mustGateway(t *testing.T, cfg gateway.Config) *gateway.Gateway {
	t.Helper()
	gw, err := gateway.New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return gw
}

func decodeError(t *testing.T, rec *httptest.ResponseRecorder) gateway.ErrorBody {
	t.Helper()
	ct := rec.Header().Get("Content-Type")
	if !strings.Contains(ct, "application/json") {
		t.Fatalf("Content-Type = %q, want application/json", ct)
	}
	var body gateway.ErrorBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode error body %q: %v", rec.Body.String(), err)
	}
	if body.Error.Code == "" {
		t.Fatalf("typed error missing code: %s", rec.Body.String())
	}
	return body
}

func TestHealthReturnsOK(t *testing.T) {
	gw := mustGateway(t, gateway.Config{})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var body gateway.HealthBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode health: %v body=%q", err, rec.Body.String())
	}
	if body.Status != "ok" {
		t.Fatalf("status = %q, want ok", body.Status)
	}
	if body.Service == "" {
		t.Fatal("health service field is empty")
	}
}

func TestHealthDoesNotRequireTenant(t *testing.T) {
	gw := mustGateway(t, gateway.Config{})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	req.Host = "unknown.example.test"
	gw.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}

func TestUnknownTenantIsFailClosed(t *testing.T) {
	hits := 0
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("should-not-see"))
	}))
	t.Cleanup(up.Close)

	gw := mustGateway(t, gateway.Config{
		Tenants: map[string]gateway.TenantRoute{
			"acme": {ID: "acme", Upstream: up.URL},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/tasks", nil)
	req.Header.Set("X-Boss-Tenant", "not-a-tenant")
	req.Header.Set("Authorization", "Bearer "+secretToken)
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound && rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 404 or 403 fail-closed", rec.Code)
	}
	if hits != 0 {
		t.Fatalf("unknown tenant was proxied (%d upstream hits)", hits)
	}
	if strings.Contains(rec.Body.String(), "should-not-see") {
		t.Fatal("unknown tenant received upstream body")
	}
	body := decodeError(t, rec)
	if body.Error.Code != "unknown_tenant" {
		t.Fatalf("code = %q, want unknown_tenant", body.Error.Code)
	}
	if body.Error.Tenant != "not-a-tenant" {
		t.Fatalf("tenant = %q, want not-a-tenant", body.Error.Tenant)
	}
}

func TestDisabledTenantIsForbidden(t *testing.T) {
	hits := 0
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(up.Close)

	gw := mustGateway(t, gateway.Config{
		Tenants: map[string]gateway.TenantRoute{
			"acme": {ID: "acme", Upstream: up.URL, Disabled: true},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/tasks", nil)
	req.Header.Set("X-Boss-Tenant", "acme")
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	if hits != 0 {
		t.Fatalf("disabled tenant was proxied (%d hits)", hits)
	}
	body := decodeError(t, rec)
	if body.Error.Code != "tenant_forbidden" {
		t.Fatalf("code = %q, want tenant_forbidden", body.Error.Code)
	}
}

func TestMissingTenantIsFailClosed(t *testing.T) {
	gw := mustGateway(t, gateway.Config{
		Tenants: map[string]gateway.TenantRoute{
			"acme": {ID: "acme", Upstream: "http://127.0.0.1:9"},
		},
	})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/tasks", nil)
	req.Host = "localhost:8787"
	gw.ServeHTTP(rec, req)

	if rec.Code == http.StatusOK {
		t.Fatal("missing tenant was accepted")
	}
	if rec.Code != http.StatusBadRequest && rec.Code != http.StatusNotFound && rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 400/404/403 fail-closed", rec.Code)
	}
	body := decodeError(t, rec)
	if body.Error.Code != "missing_tenant" && body.Error.Code != "unknown_tenant" {
		t.Fatalf("code = %q, want missing_tenant or unknown_tenant", body.Error.Code)
	}
}

func TestUnknownProjectIsFailClosed(t *testing.T) {
	hits := 0
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(up.Close)

	gw := mustGateway(t, gateway.Config{
		Tenants: map[string]gateway.TenantRoute{
			"acme": {
				ID:       "acme",
				Upstream: up.URL,
				Projects: map[string]gateway.ProjectRoute{
					"web": {ID: "web", Upstream: up.URL},
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/t/acme/p/nope/v1/tasks", nil)
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound && rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 404 or 403 fail-closed", rec.Code)
	}
	if hits != 0 {
		t.Fatalf("unknown project was proxied (%d hits)", hits)
	}
	body := decodeError(t, rec)
	if body.Error.Code != "unknown_project" {
		t.Fatalf("code = %q, want unknown_project", body.Error.Code)
	}
}

func TestAuthHeaderNotEchoedOnUnknownTenant(t *testing.T) {
	gw := mustGateway(t, gateway.Config{})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/tasks", nil)
	req.Header.Set("X-Boss-Tenant", "ghost")
	req.Header.Set("Authorization", "Bearer "+secretToken)
	req.Header.Set("X-API-Key", secretToken)
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound && rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want fail-closed", rec.Code)
	}
	raw := rec.Body.String()
	if strings.Contains(raw, secretToken) {
		t.Fatalf("credential echoed in body: %s", raw)
	}
	for k, vs := range rec.Header() {
		for _, v := range vs {
			if strings.Contains(v, secretToken) {
				t.Fatalf("credential echoed in header %s: %s", k, v)
			}
		}
	}
	_ = decodeError(t, rec)
}

func TestAuthHeaderPassedThroughToUpstream(t *testing.T) {
	var gotAuth, gotAPIKey, gotTenant, gotProject, gotPath string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotAPIKey = r.Header.Get("X-API-Key")
		gotTenant = r.Header.Get("X-Boss-Tenant")
		gotProject = r.Header.Get("X-Boss-Project")
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(up.Close)

	gw := mustGateway(t, gateway.Config{
		Tenants: map[string]gateway.TenantRoute{
			"acme": {
				ID:       "acme",
				Upstream: "http://127.0.0.1:9",
				Projects: map[string]gateway.ProjectRoute{
					"web": {ID: "web", Upstream: up.URL},
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/t/acme/p/web/v1/tasks", nil)
	req.Header.Set("Authorization", "Bearer "+secretToken)
	req.Header.Set("X-API-Key", secretToken)
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	if gotAuth != "Bearer "+secretToken {
		t.Fatalf("upstream Authorization = %q", gotAuth)
	}
	if gotAPIKey != secretToken {
		t.Fatalf("upstream X-API-Key = %q", gotAPIKey)
	}
	if gotTenant != "acme" {
		t.Fatalf("upstream X-Boss-Tenant = %q", gotTenant)
	}
	if gotProject != "web" {
		t.Fatalf("upstream X-Boss-Project = %q", gotProject)
	}
	if gotPath != "/v1/tasks" {
		t.Fatalf("upstream path = %q, want /v1/tasks (prefix stripped)", gotPath)
	}
	if strings.Contains(rec.Body.String(), secretToken) {
		t.Fatalf("credential echoed in success body: %s", rec.Body.String())
	}
}

func TestHostTenantRouting(t *testing.T) {
	var gotPath string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = io.WriteString(w, "ok")
	}))
	t.Cleanup(up.Close)

	gw := mustGateway(t, gateway.Config{
		Tenants: map[string]gateway.TenantRoute{
			"acme": {ID: "acme", Upstream: up.URL},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/healthz", nil)
	req.Host = "acme.example.test"
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	if gotPath != "/v1/healthz" {
		t.Fatalf("path = %q", gotPath)
	}
}

func TestEmptyConfigHasNoDefaultBackend(t *testing.T) {
	gw := mustGateway(t, gateway.Config{})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/anything", nil)
	req.Header.Set("X-Boss-Tenant", "acme")
	gw.ServeHTTP(rec, req)
	if rec.Code == http.StatusOK {
		t.Fatal("empty config accepted a tenant request")
	}
	if rec.Code != http.StatusNotFound && rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want fail-closed", rec.Code)
	}
	_ = decodeError(t, rec)
}
