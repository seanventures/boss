package lint

import "os"

// LoadDoc reads the YAML file the caller names.
func LoadDoc(path string) ([]byte, error) {
	return os.ReadFile(path)
}
