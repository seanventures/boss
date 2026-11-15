package gateway

import (
	"net/http"
	gopath "path"
	"strings"

	"github.com/seanheiney/boss/services/sandbox-service/internal/guest"
	"github.com/seanheiney/boss/services/sandbox-service/internal/hostapi"
)

// Route kinds (guest_calls.route).
const (
	RouteModel    = "model"
	RouteGit      = "git"
	RouteAPI      = "api"
	RouteMCP      = "mcp"
	RouteRunPlane = "run_plane"
	RouteConnect  = "connect"
)

// Rule is one route-table entry. First match wins; the table ends in a
// terminal deny (LLD §3.8 step 3).
type Rule struct {
	ID         string
	Port       int
	Methods    []string // empty = any
	PathPrefix string
	Route      string
	Handle     string // which handle kind the call must present
	Deny       bool
	// Endpoint is the desired-state endpoint key this route forwards to.
	Endpoint string
	// HostAuth forwards with the HOST's identity (BossHost proof + host
	// certificate) instead of the VM's SVID: the run plane authenticates the
	// host and learns the replica from X-Boss-Replica, which the gateway sets.
	HostAuth bool
	// SocketIdentity lets a replica call with no handle: the socket that
	// accepted the connection is the replica (P8a run plane).
	SocketIdentity bool
}

func (r Rule) matches(port int, method, path string) bool {
	if r.Port != 0 && r.Port != port {
		return false
	}
	if len(r.Methods) > 0 {
		ok := false
		for _, m := range r.Methods {
			if m == method {
				ok = true
				break
			}
		}
		if !ok {
			return false
		}
	}
	return prefixMatch(path, r.PathPrefix)
}

// prefixMatch matches on a segment boundary: "/v1/messages" matches itself
// and "/v1/messages/…", never "/v1/messagesX". A prefix ending in "/" is a
// directory prefix.
func prefixMatch(path, prefix string) bool {
	if prefix == "" {
		return true
	}
	if strings.HasSuffix(prefix, "/") {
		return strings.HasPrefix(path, prefix)
	}
	return path == prefix || strings.HasPrefix(path, prefix+"/")
}

// CleanPath refuses paths a route table could be confused by: dot segments,
// empty segments, and encoded slashes, backslashes or dots.
func CleanPath(decoded, escaped string) bool {
	if decoded == "" || decoded[0] != '/' || strings.Contains(decoded, "\\") {
		return false
	}
	if gopath.Clean(decoded) != strings.TrimSuffix(decoded, "/") && gopath.Clean(decoded) != decoded {
		return false
	}
	low := strings.ToLower(escaped)
	for _, bad := range []string{"%2f", "%5c", "%2e", "%00"} {
		if strings.Contains(low, bad) {
			return false
		}
	}
	return true
}

// Handle kinds.
const (
	HandleModel   = "model"
	HandleEgress  = "egress"
	HandleMCP     = "mcp"
	HandleRun     = "run"
	HandleReplica = "replica"
)

const (
	modelUp = hostapi.EndpointModelGateway
	egrUp   = hostapi.EndpointEgress
	mcpUp   = hostapi.EndpointMCPGateway
	runUp   = hostapi.EndpointRunPlane
)

// DefaultRules is the P8a route table. Model routes allow only the
// providers' inference paths.
func DefaultRules() []Rule {
	post := []string{http.MethodPost}
	git := []string{http.MethodGet, http.MethodPost}
	rest := []string{http.MethodGet, http.MethodPost, http.MethodPatch, http.MethodPut}
	return []Rule{
		{ID: "model-anthropic-messages", Port: guest.PortModel, Methods: post, PathPrefix: "/anthropic/v1/messages", Route: RouteModel, Handle: HandleModel, Endpoint: modelUp},
		{ID: "model-openai-chat", Port: guest.PortModel, Methods: post, PathPrefix: "/openai/v1/chat/completions", Route: RouteModel, Handle: HandleModel, Endpoint: modelUp},
		{ID: "model-openai-responses", Port: guest.PortModel, Methods: post, PathPrefix: "/openai/v1/responses", Route: RouteModel, Handle: HandleModel, Endpoint: modelUp},
		{ID: "git-smart-http", Port: guest.PortGit, Methods: git, PathPrefix: "/git/", Route: RouteGit, Handle: HandleEgress, Endpoint: egrUp},
		{ID: "git-rest-api", Port: guest.PortGit, Methods: rest, PathPrefix: "/api/", Route: RouteAPI, Handle: HandleEgress, Endpoint: egrUp},
		{ID: "mcp", Port: guest.PortMCP, Methods: []string{http.MethodGet, http.MethodPost, http.MethodDelete}, PathPrefix: "/mcp/", Route: RouteMCP, Handle: HandleMCP, Endpoint: mcpUp},
		// VMs never poll for work: the host holds the one hosts/next (§3.13.0).
		{ID: "run-plane-next-denied", Port: guest.PortRunPlane, PathPrefix: "/v1/run-plane/next", Route: RouteRunPlane, Deny: true},
		{ID: "run-plane-hosts-denied", Port: guest.PortRunPlane, PathPrefix: "/v1/run-plane/hosts/", Route: RouteRunPlane, Deny: true},
		{ID: "run-plane", Port: guest.PortRunPlane, PathPrefix: "/v1/run-plane/", Route: RouteRunPlane, Handle: HandleRun, Endpoint: runUp, HostAuth: true, SocketIdentity: true},
		{ID: "connect-p8b", Port: guest.PortConnect, Route: RouteConnect, Deny: true},
		{ID: "default-deny", Deny: true, Route: "deny"},
	}
}

func match(rules []Rule, port int, method, path string) Rule {
	for _, r := range rules {
		if r.matches(port, method, path) {
			return r
		}
	}
	return Rule{ID: "default-deny", Deny: true, Route: "deny"}
}

// strippedHeaders never survive from the guest (E2B's fail-closed rule):
// credentials, cookies, forwarding claims, and anything claiming a BOSS
// identity. Matched case-insensitively; prefixes end in "-".
var strippedHeaders = []string{
	"Authorization", "Proxy-Authorization", "X-Api-Key", "Api-Key", "Cookie",
	"X-Auth-Token", "Private-Token", "X-Amz-Security-Token",
	"Forwarded", "X-Forwarded-", "X-Real-Ip", "X-Boss-", "Boss-",
}

// LeaseHeaders are the lease data a guest runtime may carry on run-plane
// calls (HOST_API.md §4.5): checked against Postgres, not credentials. The
// run token is never the guest's: the gateway sets X-Boss-Run-Token from the
// host's run map, and X-Boss-Replica from the socket.
var LeaseHeaders = []string{"X-Boss-Job", "X-Boss-Fence-Epoch"}

// StripGuestHeaders removes every guest-supplied credential or identity
// header from h, in place, except the names in keep (canonical form).
func StripGuestHeaders(h http.Header, keep ...string) {
	for name := range h {
		if contains(keep, http.CanonicalHeaderKey(name)) {
			continue
		}
		lower := strings.ToLower(name)
		for _, s := range strippedHeaders {
			ls := strings.ToLower(s)
			if lower == ls || (strings.HasSuffix(ls, "-") && strings.HasPrefix(lower, ls)) {
				delete(h, name) // the raw key: Del would canonicalise and miss "authorization"
				break
			}
		}
	}
}

// handleFrom finds a boss_ handle in the guest's auth headers.
func handleFrom(h http.Header) string {
	for _, name := range []string{"Authorization", "X-Api-Key", "Proxy-Authorization", "Api-Key"} {
		for _, v := range h.Values(name) {
			v = strings.TrimSpace(v)
			if i := strings.IndexByte(v, ' '); i > 0 {
				scheme := strings.ToLower(v[:i])
				if scheme == "bearer" || scheme == "basic" || scheme == "token" {
					v = strings.TrimSpace(v[i+1:])
				}
			}
			if strings.HasPrefix(v, "boss_") {
				return v
			}
		}
	}
	return ""
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
