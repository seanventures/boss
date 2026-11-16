package gateway

import (
	"encoding/base64"
	"net/http"
	gopath "path"
	"strings"

	"github.com/seanventures/boss/services/sandbox-service/internal/guest"
	"github.com/seanventures/boss/services/sandbox-service/internal/hostapi"
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
	// HostProof adds the host's BossHost proof and the binding headers
	// (X-Boss-Handle, X-Boss-Vm, X-Boss-Run, X-Boss-Fence-Epoch,
	// X-Boss-Deployment) to a forward that still presents the VM's SVID
	// (P8b S10: the model gateway, HOST_API.md §4.6; S12: the MCP gateway, §4.7).
	HostProof bool
	// PathMatch, when set, must also accept the path (a rule that a prefix
	// alone cannot express, e.g. /v1/run-plane/runs/<any run>/tokens).
	PathMatch func(path string) bool
	// Audience is the destination's token audience (P8b S13, HOST_API.md §4.8):
	// when the host holds the run's token for it, the forward carries
	// X-Boss-Delegation (that token only) and a DPoP proof the host's per-run
	// key signed. A token for another audience is never attached here, and
	// would be refused there.
	Audience string
	// Credential answers locally, never forwarded: git's credential helper
	// asks here and gets the VM's own boss_egr_ handle (P8b S11, HOST_API.md
	// §4.9). The socket is the VM, so no VM can learn another's handle.
	Credential bool
	// Challenge answers a call that presents no handle with 401 and
	// `WWW-Authenticate: Basic`: git sends its credential only after a
	// challenge that names a scheme, and only then asks the helper.
	Challenge bool
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
	if !prefixMatch(path, r.PathPrefix) {
		return false
	}
	return r.PathMatch == nil || r.PathMatch(path)
}

// TokenMintingPath reports whether a path mints or exchanges credentials:
// /v1/run-plane/runs/<run>/tokens (and anything below it). A guest may never
// reach one: it would get the run's tokens bound to a key IT chose (P8b S13;
// HOST_API.md §4.8). The WHOLE path is case-folded (prefix included) and
// trailing slashes are ignored, so no spelling slips past; the rule that uses
// it matches on this alone, never on a case-sensitive prefix.
func TokenMintingPath(p string) bool {
	segs := strings.Split(strings.Trim(strings.ToLower(p), "/"), "/")
	// v1 / run-plane / runs / <run> / tokens …
	return len(segs) >= 5 && segs[0] == "v1" && segs[1] == "run-plane" && segs[2] == "runs" &&
		segs[4] == "tokens"
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
	// Path parameters (";") and whitespace, raw or decoded: an upstream may
	// read "tokens;a" or "tokens " as "tokens" where the route table did not.
	if strings.ContainsAny(decoded, "; \t\r\n\v\f") || strings.ContainsAny(escaped, "; \t\r\n\v\f") {
		return false
	}
	if gopath.Clean(decoded) != strings.TrimSuffix(decoded, "/") && gopath.Clean(decoded) != decoded {
		return false
	}
	low := strings.ToLower(escaped)
	for _, bad := range []string{"%2f", "%5c", "%2e", "%00", "%3b", "%20", "%09", "%0a", "%0d"} {
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
		{ID: "model-anthropic-messages", Port: guest.PortModel, Methods: post, HostProof: true, Audience: hostapi.AudModel, PathPrefix: "/anthropic/v1/messages", Route: RouteModel, Handle: HandleModel, Endpoint: modelUp},
		{ID: "model-openai-chat", Port: guest.PortModel, Methods: post, HostProof: true, Audience: hostapi.AudModel, PathPrefix: "/openai/v1/chat/completions", Route: RouteModel, Handle: HandleModel, Endpoint: modelUp},
		{ID: "model-openai-responses", Port: guest.PortModel, Methods: post, HostProof: true, Audience: hostapi.AudModel, PathPrefix: "/openai/v1/responses", Route: RouteModel, Handle: HandleModel, Endpoint: modelUp},
		// Git and REST go to boss-egress with the HOST's proof, the handle moved
		// into X-Boss-Handle, the host's word on VM/run/epoch and the run's
		// boss:egress token (P8b S11, HOST_API.md §4.9). Egress names the host
		// from the proof and requires the token per host, as every gateway does.
		{ID: "git-credential", Port: guest.PortGit, Methods: []string{http.MethodGet}, PathPrefix: GitCredentialPath, Route: RouteGit, Credential: true},
		{ID: "git-smart-http", Port: guest.PortGit, Methods: git, HostProof: true, Challenge: true, Audience: hostapi.AudEgress, PathPrefix: "/git/", Route: RouteGit, Handle: HandleEgress, Endpoint: egrUp},
		{ID: "git-rest-api", Port: guest.PortGit, Methods: rest, HostProof: true, Challenge: true, Audience: hostapi.AudEgress, PathPrefix: "/api/", Route: RouteAPI, Handle: HandleEgress, Endpoint: egrUp},
		// The MCP gateway (P8b S12, HOST_API.md §4.7) takes the model gateway's
		// contract: the host's proof, the handle MOVED into X-Boss-Handle and the
		// host's word on VM, run and epoch. POST only: MCP streamable HTTP here has
		// no server-initiated stream and no session to DELETE (the control plane
		// answers 405 to both, so they are refused here without a forward).
		{ID: "mcp", Port: guest.PortMCP, Methods: post, HostProof: true, Audience: hostapi.AudMCP, PathPrefix: "/mcp/", Route: RouteMCP, Handle: HandleMCP, Endpoint: mcpUp},
		{ID: "mcp-other-methods-denied", Port: guest.PortMCP, PathPrefix: "/mcp/", Route: RouteMCP, Deny: true},
		// VMs never poll for work: the host holds the one hosts/next (§3.13.0).
		{ID: "run-plane-next-denied", Port: guest.PortRunPlane, PathPrefix: "/v1/run-plane/next", Route: RouteRunPlane, Deny: true},
		{ID: "run-plane-hosts-denied", Port: guest.PortRunPlane, PathPrefix: "/v1/run-plane/hosts/", Route: RouteRunPlane, Deny: true},
		// Token minting is the HOST's own call (cpclient.RunTokens): a VM that
		// reached it would get the run's tokens for its own key (S13).
		{ID: "run-plane-tokens-denied", Port: guest.PortRunPlane, PathMatch: TokenMintingPath, Route: RouteRunPlane, Deny: true},
		{ID: "run-plane", Port: guest.PortRunPlane, PathPrefix: "/v1/run-plane/", Route: RouteRunPlane, Handle: HandleRun, Endpoint: runUp, HostAuth: true, SocketIdentity: true, Audience: hostapi.AudRunPlane},
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
	"DPoP", // a guest-made proof never stands in for the host's (S13)
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

// GitCredentialPath is where the guest's git credential helper asks for the
// VM's egress handle (P8b S11). Answered by the gateway itself.
const GitCredentialPath = "/_boss/git-credential"

// handleFrom finds a boss_ handle in the guest's auth headers. Git presents
// it as HTTP Basic (`<user>:<handle>`, from the credential helper).
func handleFrom(h http.Header) string {
	for _, name := range []string{"Authorization", "X-Api-Key", "Proxy-Authorization", "Api-Key"} {
		for _, v := range h.Values(name) {
			v = strings.TrimSpace(v)
			if i := strings.IndexByte(v, ' '); i > 0 {
				scheme := strings.ToLower(v[:i])
				if scheme == "basic" {
					if raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(v[i+1:])); err == nil {
						user, pass, _ := strings.Cut(string(raw), ":")
						if strings.HasPrefix(pass, "boss_") {
							return pass
						}
						if strings.HasPrefix(user, "boss_") {
							return user
						}
					}
					continue
				}
				if scheme == "bearer" || scheme == "token" {
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
