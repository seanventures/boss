package lint

import (
	"net/mail"
	"regexp"
	"strings"

	rulesyaml "northgate/rules"
)

// Rule pairs the human-facing metadata (mirrored in rules/api-guidelines.yaml
// for non-engineers to read) with the executable check, matched by ID.
type Rule struct {
	ID       string
	Severity string
	Title    string
	Why      string
	Fix      string
	Fixable  string
	Check    func(d *Doc) []Violation
}

func v(r Rule, line int) Violation {
	return Violation{RuleID: r.ID, Severity: r.Severity, Title: r.Title, Why: r.Why, Fix: r.Fix, Line: line, Fixable: r.Fixable}
}

func Rules() []Rule {
	var rules []Rule
	rules = append(rules, Rule{ID: "auth-required"})

	// The embedded rules/api-guidelines.yaml rulebook is the human-readable
	// source of truth; overlay its severity/title/why/fix text onto each
	// Go-defined check so the two cannot silently drift apart. The Go
	// literals above remain as a fallback if a rule id is ever missing
	// from the YAML.
	if guidelines, err := rulesyaml.Load(); err == nil {
		for i, r := range rules {
			if e, ok := guidelines[r.ID]; ok {
				rules[i].Severity = e.Severity
				rules[i].Title = e.Title
				rules[i].Why = e.Why
				rules[i].Fix = e.Fix
			}
		}
	}

	allRules = rules
	return rules
}

var allRules []Rule

type Rule struct{ ID, Severity, Title, Why, Fix string }
