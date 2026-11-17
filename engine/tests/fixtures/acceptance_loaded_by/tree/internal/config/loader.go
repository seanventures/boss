package config

import (
	"os"
	"path/filepath"
	"strings"
)

// Load reads every tenants/*.yaml under dir.
func Load(dir string) error {
	tenantsDir := filepath.Join(dir, "tenants")
	entries, err := os.ReadDir(tenantsDir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".yaml") {
			continue
		}
		if _, err := os.ReadFile(filepath.Join(tenantsDir, e.Name())); err != nil {
			return err
		}
	}
	return nil
}
