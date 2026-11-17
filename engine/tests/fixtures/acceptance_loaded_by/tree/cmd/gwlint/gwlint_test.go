package main

import "testing"

// nothing here reads config/legacy/old.yaml
func TestBad(t *testing.T) {
	run(t, "validate", "../../examples/lumen-pay.bad.yaml")
}
