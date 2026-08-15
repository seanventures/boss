package gateway

// Config is a static tenant table. Unknown tenants never fall through to a
// default backend — the gateway is fail-closed.
type Config struct {
	Tenants map[string]TenantRoute `json:"tenants"`
}

// TenantRoute maps one tenant id to an upstream origin.
type TenantRoute struct {
	ID       string                  `json:"id,omitempty"`
	Upstream string                  `json:"upstream"`
	Disabled bool                    `json:"disabled,omitempty"`
	Projects map[string]ProjectRoute `json:"projects,omitempty"`
}

// ProjectRoute optionally overrides the tenant upstream for one project.
type ProjectRoute struct {
	ID       string `json:"id,omitempty"`
	Upstream string `json:"upstream"`
}

// ErrorBody is the typed JSON error envelope. No silent drops.
type ErrorBody struct {
	Error ErrorDetail `json:"error"`
}

// ErrorDetail is a machine-readable failure. Codes are stable.
type ErrorDetail struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Tenant  string `json:"tenant,omitempty"`
	Project string `json:"project,omitempty"`
}

// HealthBody is returned by GET /health. It does not require a tenant.
type HealthBody struct {
	Status  string `json:"status"`
	Service string `json:"service"`
}
