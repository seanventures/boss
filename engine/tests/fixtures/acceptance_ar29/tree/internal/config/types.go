package config

// RouteAuth describes the authentication requirement for a route.
type RouteAuth struct {
	Type     string   `yaml:"type"`
	Issuer   string   `yaml:"issuer"`
	Audience string   `yaml:"audience"`
	Scopes   []string `yaml:"scopes"`
	JWKSURL  string   `yaml:"jwks_url"`
	APIKeyHashed string `yaml:"api_key_hashed"`
}

// RateLimit is a token-bucket configuration.
type RateLimit struct {
	RPS   int `yaml:"rps"`
	Burst int `yaml:"burst"`
}

// CORS is the per-route CORS policy.
type CORS struct {
	AllowOrigins     []string `yaml:"allow_origins"`
	AllowCredentials bool     `yaml:"allow_credentials"`
}

// Headers describes a route's outbound header transforms: add sets a
// header to a fixed value, remove deletes a header by name, and rename
// renames an existing header (old name -> new name). Applied in the order
// rename, then remove, then add.
type Headers struct {
	Add    map[string]string `yaml:"add"`
	Remove []string          `yaml:"remove"`
	Rename map[string]string `yaml:"rename"`
}

// Route is one routable path on a tenant.
type Route struct {
	Host         string    `yaml:"host"`
	Path         string    `yaml:"path"`
	PathPrefix   bool      `yaml:"path_prefix"`
	Rewrite      string    `yaml:"rewrite"`
	Methods      []string  `yaml:"methods"`
	Upstream     string    `yaml:"upstream"`
	Auth         RouteAuth `yaml:"auth"`
	Public       bool      `yaml:"public"`
	// PublicReason is required whenever Public is true: a route with no
	// auth is a configuration error unless it is explicitly public with a
	// stated reason (see internal/config/loader.go:validateTenant).
	PublicReason string    `yaml:"public_reason"`
	RateLimit    RateLimit `yaml:"rate_limit"`
	MaxBodyBytes int64     `yaml:"max_body_bytes"`
	CORS         CORS      `yaml:"cors"`
	Headers      Headers   `yaml:"headers"`
	// RequestSchema is an optional JSON-Schema fragment (object type,
	// required, properties/type only -- see internal/validate) that an
	// inbound JSON request body must satisfy before being proxied.
	RequestSchema map[string]any `yaml:"request_schema"`
}

// Upstream is a named backend target.
type Upstream struct {
	URL       string `yaml:"url"`
	TimeoutMs int    `yaml:"timeout_ms"`
}

// TenantConfig is one tenant's onboarding config.
type TenantConfig struct {
	ID                 string              `yaml:"tenant"`
	Owner              string              `yaml:"owner"`
	Contact            string              `yaml:"contact"`
	Tier               string              `yaml:"tier"`
	DataClassification string              `yaml:"data_classification"`
	// PIIFields names request fields (query parameters) this tenant wants
	// redacted from access logs, in addition to the gateway's own fixed
	// deny-list (auth headers, api keys).
	PIIFields []string            `yaml:"pii_fields"`
	RateLimit RateLimit           `yaml:"rate_limit"`
	Routes    []Route             `yaml:"routes"`
	Upstreams map[string]Upstream `yaml:"upstreams"`
}

// GlobalConfig is the shared gateway.yaml.
type GlobalConfig struct {
	Environment string `yaml:"environment"`
}

// Snapshot is one fully-validated, immutable config set.
type Snapshot struct {
	Global  *GlobalConfig
	Tenants map[string]*TenantConfig
}
