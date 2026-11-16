// Package schema loads schema/tenant.schema.json and validates a parsed
// tenant YAML document against it structurally (type/required/items), as
// distinct from gwlint's style/security rule checks.
package schema

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

//go:embed tenant.schema.json
var schemaJSON []byte

func loadSchema() (map[string]any, error) {
	var m map[string]any
	if err := json.Unmarshal(schemaJSON, &m); err != nil {
		return nil, fmt.Errorf("parse schema: %w", err)
	}
	return m, nil
}

// Validate checks doc against schema/tenant.schema.json and returns a
// descriptive error naming the offending field on the first violation
// found, or nil if doc is structurally valid.
func Validate(doc map[string]any) error {
	root, err := loadSchema()
	if err != nil {
		return err
	}
	return validateNode(doc, root, root, "")
}

func resolveRef(ref string, root map[string]any) (map[string]any, error) {
	if !strings.HasPrefix(ref, "#/") {
		return nil, fmt.Errorf("unsupported $ref %q", ref)
	}
	var cur any = root
	for _, part := range strings.Split(ref[2:], "/") {
		m, ok := cur.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("bad $ref %q", ref)
		}
		cur = m[part]
	}
	m, ok := cur.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("$ref %q does not resolve to an object schema", ref)
	}
	return m, nil
}

func displayPath(path string) string {
	if path == "" {
		return "(root)"
	}
	return strings.TrimPrefix(path, ".")
}

func checkType(value any, t string) bool {
	switch t {
	case "object":
		_, ok := value.(map[string]any)
		return ok
	case "array":
		_, ok := value.([]any)
		return ok
	case "string":
		_, ok := value.(string)
		return ok
	case "boolean":
		_, ok := value.(bool)
		return ok
	case "integer":
		switch n := value.(type) {
		case int:
			return true
		case float64:
			return n == float64(int64(n))
		}
		return false
	case "number":
		switch value.(type) {
		case int, float64:
			return true
		}
		return false
	}
	return true
}

func validateNode(value any, node map[string]any, root map[string]any, path string) error {
	if ref, ok := node["$ref"].(string); ok {
		resolved, err := resolveRef(ref, root)
		if err != nil {
			return err
		}
		return validateNode(value, resolved, root, path)
	}

	if t, ok := node["type"].(string); ok {
		if !checkType(value, t) {
			return fmt.Errorf("%s: expected type %s, got %T", displayPath(path), t, value)
		}
	}

	if enumVals, ok := node["enum"].([]any); ok {
		match := false
		for _, e := range enumVals {
			if e == value {
				match = true
				break
			}
		}
		if !match {
			return fmt.Errorf("%s: value %v is not one of the allowed values", displayPath(path), value)
		}
	}

	if pattern, ok := node["pattern"].(string); ok {
		if s, ok2 := value.(string); ok2 {
			if re, err := regexp.Compile(pattern); err == nil && !re.MatchString(s) {
				return fmt.Errorf("%s: value %q does not match required pattern", displayPath(path), s)
			}
		}
	}

	switch val := value.(type) {
	case map[string]any:
		if required, ok := node["required"].([]any); ok {
			for _, r := range required {
				name, _ := r.(string)
				if _, exists := val[name]; !exists {
					return fmt.Errorf("%s: missing required field %q", displayPath(path), name)
				}
			}
		}
		props, _ := node["properties"].(map[string]any)
		addl, hasAddl := node["additionalProperties"].(map[string]any)
		keys := make([]string, 0, len(val))
		for k := range val {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			v := val[k]
			if propSchema, ok := props[k].(map[string]any); ok {
				if err := validateNode(v, propSchema, root, path+"."+k); err != nil {
					return err
				}
			} else if hasAddl {
				if err := validateNode(v, addl, root, path+"."+k); err != nil {
					return err
				}
			}
		}
	case []any:
		if items, ok := node["items"].(map[string]any); ok {
			for i, item := range val {
				if err := validateNode(item, items, root, fmt.Sprintf("%s[%d]", path, i)); err != nil {
					return err
				}
			}
		}
	}

	return nil
}
