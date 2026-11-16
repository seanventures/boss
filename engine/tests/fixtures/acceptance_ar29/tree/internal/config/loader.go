package config

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// Load reads gateway.yaml plus every tenants/*.yaml file under dir and
// validates them as one atomic set. It never returns a partial snapshot.
func Load(dir string) (*Snapshot, error) {
	global := &GlobalConfig{}
	globalPath := filepath.Join(dir, "gateway.yaml")
	if b, err := os.ReadFile(globalPath); err == nil {
		if err := yaml.Unmarshal(b, global); err != nil {
			return nil, fmt.Errorf("parse gateway.yaml: %w", err)
		}
	}

	tenantsDir := filepath.Join(dir, "tenants")
	entries, err := os.ReadDir(tenantsDir)
	if err != nil {
		if os.IsNotExist(err) {
			return &Snapshot{Global: global, Tenants: map[string]*TenantConfig{}}, nil
		}
		return nil, fmt.Errorf("read tenants dir: %w", err)
	}

	tenants := map[string]*TenantConfig{}
	for _, e := range entries {
		if e.IsDir() || !(strings.HasSuffix(e.Name(), ".yaml") || strings.HasSuffix(e.Name(), ".yml")) {
			continue
		}
		path := filepath.Join(tenantsDir, e.Name())
		b, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %s: %w", e.Name(), err)
		}
		var tc TenantConfig
		if err := yaml.Unmarshal(b, &tc); err != nil {
			return nil, fmt.Errorf("parse %s: %w", e.Name(), err)
		}
		if tc.ID == "" {
			return nil, fmt.Errorf("%s: missing tenant id", e.Name())
		}
		if err := validateTenant(&tc); err != nil {
			return nil, fmt.Errorf("%s: %w", e.Name(), err)
		}
		tenants[tc.ID] = &tc
	}

	return &Snapshot{Global: global, Tenants: tenants}, nil
}

func validateTenant(tc *TenantConfig) error {
	for _, r := range tc.Routes {
		if r.Path == "" {
			return fmt.Errorf("route missing path")
		}
		if r.Upstream == "" {
			return fmt.Errorf("route %s missing upstream", r.Path)
		}
		if _, ok := tc.Upstreams[r.Upstream]; !ok {
			return fmt.Errorf("route %s references unknown upstream %q", r.Path, r.Upstream)
		}
		if !r.Public && r.Auth.Type == "" {
			return fmt.Errorf("route %s has no auth and is not marked public", r.Path)
		}
		if r.Public && r.PublicReason == "" {
			logPublicRouteRejected(r.Path)
			return fmt.Errorf("route %s is public but has no public_reason", r.Path)
		}
	}
	return nil
}

// logPublicRouteRejected emits a structured JSON log line (mirroring
// logReloadRejected) when a route is rejected for being public with no
// stated reason -- the same rule gwlint enforces at lint time.
func logPublicRouteRejected(path string) {
	b, err := json.Marshal(map[string]any{
		"event":  "public_route_rejected",
		"path":   path,
		"reason": "public route has no public_reason",
	})
	if err != nil {
		log.Printf("public route %s rejected: no public_reason", path)
		return
	}
	log.Println(string(b))
}
