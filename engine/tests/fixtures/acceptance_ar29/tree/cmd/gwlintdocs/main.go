// Command gwlintdocs regenerates the rulebook table in README.md from
// rules/api-guidelines.yaml (via internal/lint.Rules), run by `make docs`.
package main

import (
	"log"
	"os"

	"northgate/internal/lint"
)

func main() {
	path := "README.md"
	if len(os.Args) > 1 {
		path = os.Args[1]
	}
	b, err := os.ReadFile(path)
	if err != nil {
		log.Fatalf("read %s: %v", path, err)
	}
	updated, err := lint.RenderReadme(string(b))
	if err != nil {
		log.Fatalf("render %s: %v", path, err)
	}
	if err := os.WriteFile(path, []byte(updated), 0o644); err != nil {
		log.Fatalf("write %s: %v", path, err)
	}
}
