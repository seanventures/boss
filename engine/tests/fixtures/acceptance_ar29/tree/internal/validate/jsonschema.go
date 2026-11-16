// Package validate implements the small subset of JSON Schema (draft-07)
// that a tenant route's request_schema fragment is allowed to use: object
// type, required, and properties with a "type" constraint. It exists so a
// route can reject a malformed request body before it ever reaches the
// upstream, without pulling in a general-purpose schema library.
package validate

import (
	"encoding/json"
	"fmt"
)

// Against validates decoded JSON body against the JSON-Schema-shaped
// fragment schema, returning a human-readable error naming the first
// violation found.
func Against(schema map[string]any, body []byte) error {
	var decoded any
	if err := json.Unmarshal(body, &decoded); err != nil {
		return fmt.Errorf("request body is not valid JSON: %w", err)
	}
	return validateValue(schema, decoded, "$")
}

func validateValue(schema map[string]any, value any, path string) error {
	if t, ok := schema["type"].(string); ok {
		if !typeMatches(t, value) {
			return fmt.Errorf("%s: expected type %q", path, t)
		}
	}

	obj, isObj := value.(map[string]any)
	if required, ok := schema["required"].([]any); ok {
		if !isObj {
			return fmt.Errorf("%s: expected an object to check required fields", path)
		}
		for _, req := range required {
			name, _ := req.(string)
			if _, present := obj[name]; !present {
				return fmt.Errorf("%s.%s: required field missing", path, name)
			}
		}
	}

	if props, ok := schema["properties"].(map[string]any); ok && isObj {
		for name, propSchemaRaw := range props {
			propSchema, ok := propSchemaRaw.(map[string]any)
			if !ok {
				continue
			}
			propValue, present := obj[name]
			if !present {
				continue
			}
			if err := validateValue(propSchema, propValue, path+"."+name); err != nil {
				return err
			}
		}
	}

	return nil
}

func typeMatches(t string, v any) bool {
	switch t {
	case "object":
		_, ok := v.(map[string]any)
		return ok
	case "array":
		_, ok := v.([]any)
		return ok
	case "string":
		_, ok := v.(string)
		return ok
	case "number":
		_, ok := v.(float64)
		return ok
	case "integer":
		f, ok := v.(float64)
		return ok && f == float64(int64(f))
	case "boolean":
		_, ok := v.(bool)
		return ok
	case "null":
		return v == nil
	default:
		return true
	}
}
