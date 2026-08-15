package gateway

import (
	"encoding/json"
	"fmt"
	"os"
)

// FileConfig is the on-disk JSON shape. Listen is used by the binary only.
type FileConfig struct {
	Listen  string                 `json:"listen,omitempty"`
	Tenants map[string]TenantRoute `json:"tenants"`
}

// LoadConfigFile reads a static tenant table. Missing file or invalid JSON
// is an error — there is no implicit default tenant.
func LoadConfigFile(path string) (FileConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return FileConfig{}, err
	}
	var cfg FileConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		return FileConfig{}, fmt.Errorf("parse tenant gateway config: %w", err)
	}
	if cfg.Tenants == nil {
		cfg.Tenants = map[string]TenantRoute{}
	}
	return cfg, nil
}

// Config converts a file payload into the library Config.
func (f FileConfig) Config() Config {
	return Config{Tenants: f.Tenants}
}
