// Package rules embeds and parses api-guidelines.yaml, the human-readable
// rulebook, so gwlint's executable checks load their id/severity/title/
// why/fix text from the YAML file at runtime instead of merely mirroring
// it in separately-maintained Go literals.
package rules

import (
	_ "embed"

	"gopkg.in/yaml.v3"
)

//go:embed api-guidelines.yaml
var guidelinesYAML []byte

// Entry is one rule's human-facing metadata as declared in api-guidelines.yaml.
type Entry struct {
	ID       string `yaml:"id"`
	Severity string `yaml:"severity"`
	Title    string `yaml:"title"`
	Why      string `yaml:"why"`
	Fix      string `yaml:"fix"`
}

type document struct {
	Rules []Entry `yaml:"rules"`
}

// Load parses the embedded rulebook and returns its entries keyed by rule id.
func Load() (map[string]Entry, error) {
	var d document
	if err := yaml.Unmarshal(guidelinesYAML, &d); err != nil {
		return nil, err
	}
	out := make(map[string]Entry, len(d.Rules))
	for _, e := range d.Rules {
		out[e.ID] = e
	}
	return out, nil
}
