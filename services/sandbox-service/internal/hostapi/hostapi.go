// Package hostapi is the Go side of the boss-hostd ⇄ control-plane contract
// (docs/infra/HOST_API.md, from the P8 LLD §3.2). Every type here is a JSON
// wire shape; the Python control plane (apps/api/app/routers/fleet.py,
// engine/boss_engine/services/fleet.py) implements the other side, and the
// field names below are exactly the names it reads and writes.
//
// Changes are additive. A field the other side does not know is ignored
// (pydantic's default; Go's encoding/json).
package hostapi

import (
	"strconv"
	"time"
)

// Version is the host API contract version boss-hostd advertises.
const Version = "1.1"

// Paths (host → control plane). %s is the host id from enrolment.
const (
	PathEnrol      = "/v1/hosts/enrol"
	PathRenew      = "/v1/hosts/%s/renew"
	PathHeartbeat  = "/v1/hosts/%s/heartbeat"
	PathDesired    = "/v1/hosts/%s/desired"
	PathObserved   = "/v1/hosts/%s/observed"
	PathGuestCalls = "/v1/hosts/%s/guest-calls"
	PathEvents     = "/v1/hosts/%s/events" // optional on the control plane: 404 disables it
	PathReplicas   = "/v1/hosts/%s/replicas"
	PathHostsNext  = "/v1/run-plane/hosts/next"
	PathRunLease   = "/v1/run-plane/runs/%s/lease"
	// PathRunTokens (P8b S13): the run's delegated, per-destination tokens
	// for the HOST's DPoP key (HOST_API.md §4.8).
	PathRunTokens = "/v1/run-plane/runs/%s/tokens"
)

// Headers the gateway sets on a forward to a central gateway (P8b S13,
// HOST_API.md §4.8): the run's delegated token for THAT destination and the
// DPoP proof the host's per-run key signed for this very request.
const (
	HeaderDelegation = "X-Boss-Delegation"
	HeaderDPoP       = "DPoP"
)

// Per-destination token audiences (RFC 8707; engine identity/delegation.py).
const (
	AudModel    = "boss:model"
	AudMCP      = "boss:mcp"
	AudEgress   = "boss:egress"
	AudRunPlane = "boss:run_plane"
)

// CapHostTokens is the capability a host advertises at enrol and heartbeat
// when it holds per-run DPoP keys (P8b S13). The control plane records it
// stickily and from then on REQUIRES the run's delegated token on every
// forward from this host (HOST_API.md §4.8).
const CapHostTokens = "host_tokens"

// HostAudiences is what the host asks for per run, in table order.
var HostAudiences = []string{AudModel, AudMCP, AudEgress, AudRunPlane}

// The host proof: every call after enrolment carries
// "Authorization: BossHost <compact JWS>" signed by the host certificate's key
// (pki.SignHostProof). A TLS terminator may additionally verify the client
// certificate the host always presents.
const (
	AuthScheme = "BossHost"
	ProofTyp   = "boss-host+jwt"
)

// Named refusal codes (the control plane's error.code / detail.error).
const (
	ReasonJoinTokenInvalid = "join_token_invalid" // 401 on enrol
	ReasonHostRevoked      = "host_revoked"       // 403 on any host call
	ReasonNotFound         = "not_found"          // 404
	ReasonCertUnknown      = "host_certificate_unknown"
	ReasonProofReplay      = "proof_replay"
)

// VM modes (LLD §2.6).
const (
	ModeReplica = "replica"
	ModePerRun  = "per_run"
)

// Desired VM states.
const (
	DesiredRunning = "running"
	DesiredStopped = "stopped"
)

// Observed VM states (LLD §3.3 state machine; P8a subset plus the P8b names).
const (
	StateProvisioning = "provisioning"
	StateBooting      = "booting"
	StateRestoring    = "restoring" // P8b (snapshots)
	StateReady        = "ready"
	StateServing      = "serving"
	StateDraining     = "draining"
	StateUnhealthy    = "unhealthy"
	StateCrashloop    = "crashloop"
	StateStopped      = "stopped"
	StateRefused      = "refused"     // desired but refused by host policy (named reason)
	StateQuarantined  = "quarantined" // the replacement after a crashloop also failed
	StateHeadless     = "headless_expired"
)

// ---- Enrol / renew -------------------------------------------------------

// EnrolRequest: POST /v1/hosts/enrol (public; the join token is the
// credential). The host generates both private keys; only CSRs cross.
type EnrolRequest struct {
	JoinToken string            `json:"join_token"`
	CSR       string            `json:"csr_pem"`
	SVIDCSR   string            `json:"svid_csr_pem,omitempty"`
	Hostname  string            `json:"hostname"`
	Backend   string            `json:"backend"` // firecracker | process
	Facts     Attestation       `json:"facts"`
	Capacity  Capacity          `json:"capacity"`
	Labels    map[string]string `json:"labels,omitempty"`
	// Capabilities the host has (P8b S13: CapHostTokens).
	Capabilities []string `json:"capabilities,omitempty"`
}

// EnrolResponse (201) — also the renew response (a subset of fields).
type EnrolResponse struct {
	HostID              string            `json:"host_id"`
	OrgID               string            `json:"org_id,omitempty"`
	Certificate         string            `json:"certificate_pem"` // 24 h, clientAuth
	CertificateNotAfter string            `json:"certificate_not_after"`
	RenewAfter          string            `json:"renew_after"`
	SVIDIntermediate    string            `json:"svid_intermediate_pem"` // CA:true pathlen 0, URI-constrained
	CA                  string            `json:"ca_pem"`                // the fleet CA
	SpiffeID            string            `json:"spiffe_id,omitempty"`
	TrustDomain         string            `json:"trust_domain,omitempty"`
	VMPrefix            string            `json:"vm_prefix,omitempty"`
	HeartbeatSeconds    int               `json:"heartbeat_seconds,omitempty"`
	Endpoints           map[string]string `json:"endpoints,omitempty"`
}

// RenewRequest: POST /v1/hosts/{id}/renew.
type RenewRequest struct {
	CSR     string `json:"csr_pem"`
	SVIDCSR string `json:"svid_csr_pem,omitempty"`
}

// ---- Heartbeat and observed ----------------------------------------------

// Attestation is the hardening facts block (agent_hosts columns). Facts are
// measured, never configured: a false is reported as false, with a reason.
type Attestation struct {
	KVM             bool `json:"kvm_live"`
	FirecrackerLive bool `json:"firecracker_live"`
	Jailer          bool `json:"jailer_on"` // true only when every VM runs under the jailer (P8b)
	Seccomp         bool `json:"seccomp_default"`
	SMTOff          bool `json:"smt_off"`
	KSMOff          bool `json:"ksm_off"`
	SwapOff         bool `json:"swap_off"`
	StrictSecrets   bool `json:"strict_secrets"`
	NetPolicyOK     bool `json:"net_policy_ok"`
	// Reasons names each false fact (stored in the facts JSON, not a column).
	Reasons map[string]string `json:"reasons,omitempty"`
}

type Capacity struct {
	VCPU    int `json:"vcpu"`
	MemMiB  int `json:"mem_mib"`
	MaxVMs  int `json:"max_vms"`
	UsedVMs int `json:"used_vms"`
}

// Allocatable is what the scheduler may still place (0 while not ready).
type Allocatable struct {
	VMs int `json:"vms"`
}

type Pressure struct {
	MemUsedPct  float64 `json:"mem_used_pct"`
	DiskUsedPct float64 `json:"disk_used_pct"`
	Load1       float64 `json:"load1"`
}

// ReplicaStatus is one entry of heartbeat/observed "replicas".
type ReplicaStatus struct {
	ReplicaID       string `json:"replica_id"`
	VMID            string `json:"vm_id"`
	DeploymentID    string `json:"deployment_id,omitempty"`
	Mode            string `json:"vm_mode,omitempty"`
	State           string `json:"state"`
	Reason          string `json:"reason,omitempty"`
	VMIP            string `json:"vm_ip,omitempty"`
	ImageDigest     string `json:"image_digest,omitempty"`
	Slots           int    `json:"slots"`
	Busy            int    `json:"busy"`
	FreeSlots       int    `json:"free_slots"`        // from the 500 ms ping; 0 unless serving
	VsockPingAgeMS  int64  `json:"vsock_ping_age_ms"` // -1 = never answered
	Restarts        int    `json:"restarts"`
	DropsOriginated uint64 `json:"drops_originated"` // nftables: packets the VM tried to originate
}

// PoolDepth is one warm pool: ready warm VMs (depth) against the wanted
// minimum (per_run min_warm, default 2; replica surge spares).
type PoolDepth struct {
	Depth   int `json:"depth"`
	MinWarm int `json:"min_warm"`
}

// HeartbeatRequest: POST /v1/hosts/{id}/heartbeat every heartbeat_seconds (2).
type HeartbeatRequest struct {
	Facts              Attestation          `json:"facts"`
	Capacity           Capacity             `json:"capacity"`
	Allocatable        Allocatable          `json:"allocatable"`
	Pressure           Pressure             `json:"pressure"`
	Replicas           []ReplicaStatus      `json:"replicas"`
	WarmPools          map[string]PoolDepth `json:"warm_pools"`
	BGPState           string               `json:"bgp_state"` // "none" until P8e
	ObservedGeneration int64                `json:"observed_generation"`
	// Informational extras (ignored by a control plane that does not read them).
	Status          string `json:"status,omitempty"` // ready | headless | revoked
	Backend         string `json:"backend,omitempty"`
	ClaimsFrozen    bool   `json:"claims_frozen"`
	ContractVersion string `json:"contract_version,omitempty"`
	// Capabilities, re-sent every heartbeat (sticky on the control plane).
	Capabilities []string `json:"capabilities,omitempty"`
}

type HeartbeatResponse struct {
	HostID            string `json:"host_id"`
	DesiredGeneration int64  `json:"desired_generation"`
	Status            string `json:"status,omitempty"`
	ReplicasSeen      int    `json:"replicas_seen"`
	Renew             bool   `json:"renew"` // the control plane asks for an early renewal
	HeartbeatSeconds  int    `json:"heartbeat_seconds,omitempty"`
}

// ObservedRequest: POST /v1/hosts/{id}/observed — pushed at once on any VM
// state change, so the control plane does not wait for the next heartbeat.
type ObservedRequest struct {
	Replicas           []ReplicaStatus `json:"replicas"`
	ObservedGeneration int64           `json:"observed_generation"`
	// StateGen is the host's replica-state generation after these changes;
	// the control plane stores it so the next hosts/next needs no resync.
	StateGen int64 `json:"state_gen"`
}

// ---- Work: one long-poll per HOST (LLD §3.13.0, REVISION 2026-11-14) -----

// HostsNextRequest: POST /v1/run-plane/hosts/next. No replica list: BOSS
// tracks slots itself (total from the deployment version, busy = active
// leases). StateGen is the host's replica-state generation.
type HostsNextRequest struct {
	WaitMS   int   `json:"wait_ms"`
	StateGen int64 `json:"state_gen"`
}

// HostsNextResponse: either Resync (PUT the full replica map once, then poll
// again) or the runs claimed for this host's replicas, leased in BOSS.
type HostsNextResponse struct {
	Resync bool         `json:"resync"`
	Runs   []Assignment `json:"runs"`
}

// Assignment is one claimed run for one of this host's replicas.
type Assignment struct {
	RunID     string `json:"run_id"`
	JobID     string `json:"job_id"`
	ReplicaID string `json:"replica_id"`
	Epoch     int64  `json:"epoch"`
	// RunToken is the HOST's: kept in memory, attached by the gateway to the
	// run's run-plane calls, never given to the VM (HOST_API.md §4.5).
	RunToken       string `json:"run_token,omitempty"`
	Kind           string `json:"kind,omitempty"`
	LeaseExpiresAt string `json:"lease_expires_at,omitempty"`
	// Handles are the GUEST's placeholders for this run (P8b S10/S12): "mdl"
	// (boss_mdl_…), "mcpgw" (boss_mcpgw_…), …, plus "mcp_servers", a JSON
	// object of {name: guest URL} the host passes through untouched. The host
	// binds each handle to (run, epoch, this VM) in the gateway and hands them
	// to the VM with the work item; it never writes them.
	Handles map[string]any `json:"handles,omitempty"`
}

// Handle returns the string handle of one kind ("" when absent).
func (a Assignment) Handle(kind string) string {
	if a.Handles == nil {
		return ""
	}
	s, _ := a.Handles[kind].(string)
	return s
}

// RunTokensRequest: POST /v1/run-plane/runs/{run}/tokens with the lease
// headers (P8b S13). The jkt is the RFC 7638 thumbprint of the Ed25519 key
// boss-hostd generated for this run; the key itself never leaves the host.
type RunTokensRequest struct {
	JKT       string   `json:"jkt"`
	Audiences []string `json:"audiences,omitempty"`
}

// IssuedToken is one delegated token: usable only with a DPoP proof signed
// by the key whose thumbprint it was minted for, only at its audience.
type IssuedToken struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type,omitempty"`
	ExpiresIn   int    `json:"expires_in"`
	JTI         string `json:"jti,omitempty"`
}

// RunTokensResponse: one token per audience, and when to rotate.
type RunTokensResponse struct {
	RunID              string                 `json:"run_id"`
	JKT                string                 `json:"jkt"`
	Tokens             map[string]IssuedToken `json:"tokens"`
	ExpiresIn          int                    `json:"expires_in"`
	RotateAfterSeconds int                    `json:"rotate_after_seconds"`
}

// ReplicaMap: PUT /v1/hosts/{id}/replicas — the full map, only on resync.
type ReplicaMap struct {
	StateGen int64           `json:"state_gen"`
	Replicas []ReplicaStatus `json:"replicas"`
}

// LeaseAction: POST /v1/run-plane/runs/{run}/lease with the lease headers
// X-Boss-Replica, X-Boss-Job, X-Boss-Run-Token, X-Boss-Fence-Epoch.
type LeaseAction struct {
	HandBack bool   `json:"hand_back"`
	Action   string `json:"action,omitempty"` // "hand_back" too, for an older reader
}

// ---- Desired state -------------------------------------------------------

// DesiredState: GET /v1/hosts/{id}/desired?since=<gen>&wait=<seconds≤30>.
// Always 200. Changed=false means the wait elapsed with no newer generation.
// Changed=true carries the COMPLETE state (level-based); since=0 is a full
// resync. A VM absent from VMs, or with state "stopped", is to be stopped.
type DesiredState struct {
	Changed    bool              `json:"changed"`
	HostID     string            `json:"host_id"`
	Generation int64             `json:"generation"`
	Status     string            `json:"status,omitempty"`
	VMPrefix   string            `json:"vm_prefix,omitempty"`
	VMs        []DesiredVM       `json:"vms"`
	Pools      []Pool            `json:"pools"`
	Endpoints  map[string]string `json:"endpoints"` // relative paths resolve against the CP URL
	Fences     []Fence           `json:"fences"`
	// Revocations are handle fingerprints the gateway must refuse.
	Revocations []string `json:"revocations"`
	// Extensions the Go host honours when present (absent in P8a = defaults).
	Directors    []string `json:"directors,omitempty"`     // IPv4 allowed to send IPIP (P8b)
	ClaimsFrozen bool     `json:"claims_frozen,omitempty"` // per_run gate, ORed with the host's switch
}

// Endpoint keys in DesiredState.Endpoints.
const (
	EndpointRunPlane     = "run_plane"
	EndpointModelGateway = "model_gateway"
	EndpointEgress       = "egress"
	EndpointMCPGateway   = "mcp_gateway"
)

type DesiredVM struct {
	ReplicaID     string    `json:"replica_id"`
	VMID          string    `json:"vm_id"`
	Mode          string    `json:"vm_mode"`
	State         string    `json:"state"` // running | stopped
	DeploymentID  string    `json:"deployment_id"`
	ProjectID     string    `json:"project_id,omitempty"`
	AgentKey      string    `json:"agent_key,omitempty"`
	Environment   string    `json:"environment,omitempty"`
	AgentVersion  string    `json:"agent_version,omitempty"`
	ImageDigest   string    `json:"image_digest,omitempty"`
	Resources     Resources `json:"resources,omitempty"`
	Slots         int       `json:"slots"`
	Weight        int       `json:"weight"`
	VMIP          string    `json:"vm_ip,omitempty"`
	VIP           string    `json:"vip,omitempty"`
	VIPPorts      []int     `json:"vip_ports,omitempty"`
	Hostname      string    `json:"hostname,omitempty"`
	Routing       string    `json:"routing,omitempty"`
	StrictSecrets bool      `json:"strict_secrets"`
	SpiffeID      string    `json:"spiffe_id,omitempty"`
	Generation    int64     `json:"generation"`
	// Extensions (P8b: handles arrive with runs; absent in P8a).
	ReplicaHandle string    `json:"replica_handle,omitempty"` // opaque; memory only on the host
	Runs          []RunBind `json:"runs,omitempty"`
}

// Running reports whether the VM should be running.
func (v DesiredVM) Running() bool { return v.State == "" || v.State == DesiredRunning }

// Resources is the deployment's free-form resources JSON.
type Resources map[string]any

func (r Resources) num(keys ...string) int {
	for _, k := range keys {
		switch v := r[k].(type) {
		case float64:
			return int(v)
		case int:
			return v
		case string:
			if n, err := strconv.Atoi(v); err == nil {
				return n
			}
		}
	}
	return 0
}

// VCPU is resources.vcpu (or cpus).
func (r Resources) VCPU() int { return r.num("vcpu", "vcpus", "cpus") }

// MemMiB is resources.mem_mib (or memory_mib).
func (r Resources) MemMiB() int { return r.num("mem_mib", "memory_mib") }

// RunBind binds a run's opaque handles to (run, epoch, vm). P8b.
type RunBind struct {
	RunID   string  `json:"run_id"`
	Epoch   int64   `json:"epoch"`
	Handles Handles `json:"handles"`
}

type Handles struct {
	Model  string `json:"model,omitempty"`  // boss_mdl_…
	Egress string `json:"egress,omitempty"` // boss_egr_…
	MCP    string `json:"mcp,omitempty"`    // boss_mcpgw_…
	Run    string `json:"run,omitempty"`    // run-plane handle
}

type Pool struct {
	DeploymentID string    `json:"deployment_id"`
	ImageDigest  string    `json:"image_digest,omitempty"`
	Resources    Resources `json:"resources,omitempty"`
	MinWarm      int       `json:"min_warm"`
}

// Fence: any call for RunID with an epoch below MinEpoch gets 409 fenced.
type Fence struct {
	RunID    string `json:"run_id"`
	MinEpoch int64  `json:"min_epoch"`
}

// ---- Audit and events ----------------------------------------------------

// GuestCall is one guest_calls row (LLD §3.19). Never a body, a query or a
// handle (the control plane refuses a row containing anything handle-shaped).
type GuestCall struct {
	CallID       string `json:"call_id"` // idempotency key per host
	At           string `json:"at"`
	VMID         string `json:"vm_id"`
	RunID        string `json:"run_id,omitempty"`
	Epoch        *int64 `json:"epoch,omitempty"`
	DeploymentID string `json:"deployment_id,omitempty"`
	Route        string `json:"route"` // model | git | api | mcp | run_plane | connect
	DestHost     string `json:"dest_host,omitempty"`
	Method       string `json:"method"`
	PathTemplate string `json:"path_template"`
	Status       int    `json:"status"`
	BytesIn      int64  `json:"bytes_in"`
	BytesOut     int64  `json:"bytes_out"`
	DurationMS   int64  `json:"duration_ms"`
	HandleFP     string `json:"handle_fp,omitempty"`
	Verdict      string `json:"verdict"` // allow | deny
	RuleID       string `json:"rule_id,omitempty"`
	RejectedBy   string `json:"rejected_by,omitempty"`
	TraceID      string `json:"trace_id,omitempty"`
}

// GuestCallBatch: POST /v1/hosts/{id}/guest-calls (≤ 1000 rows).
type GuestCallBatch struct {
	Calls []GuestCall `json:"calls"`
}

// HostEvent is one watchdog/gateway event (host_events row).
type HostEvent struct {
	At     string            `json:"at"`
	VMID   string            `json:"vm_id,omitempty"`
	Kind   string            `json:"kind"` // vm_state | restart | crashloop | handle_replay | refused | adopted | orphan_tap_removed
	From   string            `json:"from,omitempty"`
	To     string            `json:"to,omitempty"`
	Detail map[string]string `json:"detail,omitempty"`
	Actor  string            `json:"actor"` // watchdog | gateway | reconciler
}

// HostEventBatch: POST /v1/hosts/{id}/events (optional endpoint).
type HostEventBatch struct {
	Events []HostEvent `json:"events"`
}

// Now formats a timestamp the way every At field is written.
func Now() string { return time.Now().UTC().Format(time.RFC3339Nano) }
