package gateway_test

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	gateway "github.com/seanheiney/boss/go/tenant-gateway"
)

func TestLoadConfigFileFailClosedUnknownTenant(t *testing.T) {
	path := filepath.Join("testdata", "tenants.example.json")
	file, err := gateway.LoadConfigFile(path)
	if err != nil {
		t.Fatalf("LoadConfigFile: %v", err)
	}
	gw, err := gateway.New(file.Config())
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/tasks", nil)
	req.Header.Set("X-Boss-Tenant", "not-in-file")
	req.Header.Set("Authorization", "Bearer "+secretToken)
	gw.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	body := decodeError(t, rec)
	if body.Error.Code != "unknown_tenant" {
		t.Fatalf("code = %q", body.Error.Code)
	}
	if strings.Contains(rec.Body.String(), secretToken) {
		t.Fatal("credential echoed from file-backed config")
	}
}
