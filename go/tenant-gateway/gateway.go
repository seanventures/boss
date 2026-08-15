package gateway

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
)

// ServiceName is the health.service value. Not a production hostname.
const ServiceName = "boss-tenant-gateway"

// Gateway is a tenant/project reverse proxy. It is optional scale
// infrastructure: the Python engine must run without it.
type Gateway struct {
	tenants map[string]TenantRoute
}

// New builds a gateway from a static config. An empty tenant table is valid
// and fail-closed (only /health succeeds).
func New(cfg Config) (*Gateway, error) {
	tenants := make(map[string]TenantRoute, len(cfg.Tenants))
	for key, tenant := range cfg.Tenants {
		id := normalizeID(firstNonEmpty(tenant.ID, key))
		if id == "" {
			continue
		}
		tenant.ID = id
		if len(tenant.Projects) > 0 {
			projects := make(map[string]ProjectRoute, len(tenant.Projects))
			for pkey, project := range tenant.Projects {
				pid := normalizeID(firstNonEmpty(project.ID, pkey))
				if pid == "" {
					continue
				}
				project.ID = pid
				projects[pid] = project
			}
			tenant.Projects = projects
		}
		tenants[id] = tenant
	}
	return &Gateway{tenants: tenants}, nil
}

// ServeHTTP routes a request or fail-closes with a typed JSON error.
func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if isHealthPath(r.URL.Path) {
		writeJSON(w, http.StatusOK, HealthBody{Status: "ok", Service: ServiceName})
		return
	}

	resolved := g.resolve(r)
	if resolved.code != "" {
		writeError(w, statusFor(resolved.code), resolved.code, messageFor(resolved.code), resolved.tenant, resolved.project)
		return
	}

	tenant, ok := g.tenants[resolved.tenant]
	if !ok {
		writeError(w, http.StatusNotFound, "unknown_tenant", messageFor("unknown_tenant"), resolved.tenant, resolved.project)
		return
	}
	if tenant.Disabled {
		writeError(w, http.StatusForbidden, "tenant_forbidden", messageFor("tenant_forbidden"), tenant.ID, resolved.project)
		return
	}

	upstream := tenant.Upstream
	if resolved.project != "" {
		if len(tenant.Projects) > 0 {
			project, ok := tenant.Projects[resolved.project]
			if !ok {
				writeError(w, http.StatusNotFound, "unknown_project", messageFor("unknown_project"), tenant.ID, resolved.project)
				return
			}
			if project.Upstream != "" {
				upstream = project.Upstream
			}
		}
	}
	target, err := url.Parse(upstream)
	if err != nil || target.Scheme == "" || target.Host == "" {
		writeError(w, http.StatusBadGateway, "bad_upstream", messageFor("bad_upstream"), tenant.ID, resolved.project)
		return
	}

	proxy := httputil.NewSingleHostReverseProxy(target)
	originalDirector := proxy.Director
	strip := resolved.stripPrefix
	tenantID := tenant.ID
	projectID := resolved.project
	proxy.Director = func(req *http.Request) {
		originalDirector(req)
		if strip != "" {
			req.URL.Path = stripPathPrefix(req.URL.Path, strip)
			req.URL.RawPath = ""
		}
		req.Host = target.Host
		req.Header.Set("X-Boss-Tenant", tenantID)
		if projectID != "" {
			req.Header.Set("X-Boss-Project", projectID)
		}
	}
	proxy.ErrorHandler = func(rw http.ResponseWriter, _ *http.Request, _ error) {
		// Static message only — never interpolate the request or credentials.
		writeError(rw, http.StatusBadGateway, "upstream_unreachable", messageFor("upstream_unreachable"), tenantID, projectID)
	}
	proxy.ServeHTTP(w, r)
}

type resolvedRoute struct {
	tenant      string
	project     string
	stripPrefix string
	code        string
}

func (g *Gateway) resolve(r *http.Request) resolvedRoute {
	if route, ok := parsePathRoute(r.URL.Path); ok {
		if route.tenant == "" {
			return resolvedRoute{code: "missing_tenant"}
		}
		return route
	}

	tenant := normalizeID(r.Header.Get("X-Boss-Tenant"))
	project := normalizeID(r.Header.Get("X-Boss-Project"))
	if tenant != "" {
		return resolvedRoute{tenant: tenant, project: project}
	}

	return g.resolveHost(r.Host)
}

func parsePathRoute(path string) (resolvedRoute, bool) {
	if !strings.HasPrefix(path, "/t/") {
		return resolvedRoute{}, false
	}
	rest := strings.TrimPrefix(path, "/t/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 {
		return resolvedRoute{code: "missing_tenant"}, true
	}
	tenant := normalizeID(parts[0])
	if tenant == "" {
		return resolvedRoute{code: "missing_tenant"}, true
	}
	route := resolvedRoute{tenant: tenant, stripPrefix: "/t/" + parts[0]}
	if len(parts) >= 3 && parts[1] == "p" {
		route.project = normalizeID(parts[2])
		route.stripPrefix = "/t/" + parts[0] + "/p/" + parts[2]
	}
	return route, true
}

func (g *Gateway) resolveHost(host string) resolvedRoute {
	hostname := host
	if h, _, err := net.SplitHostPort(host); err == nil {
		hostname = h
	}
	hostname = strings.ToLower(strings.TrimSpace(hostname))
	if hostname == "" || isLocalHost(hostname) {
		return resolvedRoute{code: "missing_tenant"}
	}
	labels := strings.Split(hostname, ".")
	if len(labels) >= 2 {
		if tenant, ok := g.tenants[labels[1]]; ok {
			if _, found := tenant.Projects[labels[0]]; found {
				return resolvedRoute{tenant: labels[1], project: labels[0]}
			}
		}
	}
	if _, ok := g.tenants[labels[0]]; ok {
		return resolvedRoute{tenant: labels[0]}
	}
	// Claimed host tenant that is not in the table — fail closed as unknown.
	if labels[0] != "" && !isLocalHost(labels[0]) {
		return resolvedRoute{tenant: labels[0], code: "unknown_tenant"}
	}
	return resolvedRoute{code: "missing_tenant"}
}

func isHealthPath(path string) bool {
	return path == "/health" || path == "/health/"
}

func isLocalHost(host string) bool {
	switch host {
	case "localhost", "127.0.0.1", "::1", "0.0.0.0":
		return true
	default:
		return false
	}
}

func stripPathPrefix(path, prefix string) string {
	trimmed := strings.TrimPrefix(path, prefix)
	if trimmed == "" {
		return "/"
	}
	if !strings.HasPrefix(trimmed, "/") {
		return "/" + trimmed
	}
	return trimmed
}

func normalizeID(value string) string {
	return strings.ToLower(strings.TrimSpace(value))
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func statusFor(code string) int {
	switch code {
	case "missing_tenant":
		return http.StatusBadRequest
	case "tenant_forbidden":
		return http.StatusForbidden
	case "unknown_tenant", "unknown_project":
		return http.StatusNotFound
	case "bad_upstream", "upstream_unreachable":
		return http.StatusBadGateway
	default:
		return http.StatusNotFound
	}
}

func messageFor(code string) string {
	switch code {
	case "missing_tenant":
		return "tenant is required"
	case "unknown_tenant":
		return "tenant is not registered"
	case "tenant_forbidden":
		return "tenant is disabled"
	case "unknown_project":
		return "project is not registered for this tenant"
	case "bad_upstream":
		return "tenant upstream is not configured"
	case "upstream_unreachable":
		return "upstream request failed"
	default:
		return "request rejected"
	}
}

func writeError(w http.ResponseWriter, status int, code, message, tenant, project string) {
	writeJSON(w, status, ErrorBody{Error: ErrorDetail{
		Code:    code,
		Message: message,
		Tenant:  tenant,
		Project: project,
	}})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	// Never copy inbound Authorization / API keys onto the response.
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
