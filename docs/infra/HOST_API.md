# boss-hostd ⇄ control plane: the host API (P8a)

The contract between `boss-hostd` (Go, `services/sandbox-service`, BOSSAPP-1062) and the BOSS
control plane (Python, `apps/api/app/routers/fleet.py` and `engine/boss_engine/services/fleet*.py`,
BOSSAPP-1063). It comes from the P8 low-level design, §3.2 (calls and auth), §3.3 (lifecycle),
§3.7 (networking), §3.8 (guest gateway), §3.12.1 (SVIDs), §3.16.1 (watchdog) and §3.19 (audit).

Where the LLD left a choice open, this document records the choice. Every choice is listed in
[Decisions](#decisions). Both sides implement exactly what is written here:

- **Go wire types:** `internal/hostapi/hostapi.go`.
- **Go fake control plane:** `internal/fakecp`. It enforces the same rules as the Python side, and
  the Go tests run against it.
- **Python executable statement of the host side:** `engine/tests/_fleet_support.py::FakeHost`.

**Contract version:** `1.0`. Changes are additive, and each side ignores fields it does not know.

---

## 1. Transport and authentication

### 1.1 TLS to the control plane

- The control plane is reached over `https`.
- The host verifies the server certificate against **one pinned CA bundle**
  (`BOSS_HOSTD_CP_CA`), never the system roots.
- Plain `http` is accepted only to a loopback address, for dev and CI.

### 1.2 The host's identity

At enrolment the host generates two P-256 keys and sends only CSRs:

- **Host certificate:** 24 h, `clientAuth`.
- **SVID intermediate:** `CA:true`, pathlen 0, URI name constraint set to the trust domain.

The response carries the **fleet CA** (`ca_pem`):

- The host **pins it**.
- A later renewal answered under a different fleet CA is refused.
- Both issued certificates must chain to the fleet CA and match the host's own keys.

Key files are `0600` under `<state>/pki/`. No key or token is ever logged or printed.

### 1.3 Every call after enrolment

```
Authorization: BossHost <compact JWS>
header {"alg":"ES256","typ":"boss-host+jwt","x5t#S256":"<b64url sha256(cert DER)>"}
claims {"htm":"POST","htu":"/v1/hosts/<id>/heartbeat","iat":<unix s>,"jti":"<16..128 chars>"}
signature: raw r||s (64 bytes), b64url
```

- `htu` is the request path without the query string.
- The control plane:
  - pins the certificate by fingerprint;
  - checks `htm`, `htu`, and `iat` (60 s, plus 30 s of skew);
  - spends the `jti` once.
- The host also presents its client certificate whenever TLS asks for one. A TLS terminator may
  verify it and forward it as `X-Boss-Client-Cert`; that is the control plane's second accepted
  form.
- Proof interop is verified both ways: a Go-signed proof passes the Python checks, and a
  Python-signed proof passes Go's `pki.VerifyHostProof`.

### 1.4 Renewal

- The host renews at **two thirds** of the certificate's lifetime.
- It renews immediately when a heartbeat answers `"renew": true`.
- After a renewal the HTTP connection pool is replaced, so no request rides an old certificate.

### 1.5 Revocation

- Every host call from a revoked host gets **`403 {"detail":{"error":"host_revoked"}}`**. That
  includes heartbeat, renew and desired.
- The host then enters the `revoked` state:
  - it admits no new VM (named reason `host_revoked`);
  - the guest gateway answers `503 host_not_serving`;
  - running VMs are left for the operator to drain.

### 1.6 Errors

Every non-2xx body is FastAPI-shaped: `{"detail": {"error": "<code>", "message": "..."}}`.

- The Go client also accepts `{"error"|"reason"|"code": ...}` and a string `detail`.
- Codes it acts on: `join_token_invalid`, `host_revoked`, `not_found`.

---

## 2. Endpoints (host → control plane)

| Call | Path | Auth | Notes |
|---|---|---|---|
| Enrol | `POST /v1/hosts/enrol` | the join token, in the body | `201`. One-time token. |
| Renew | `POST /v1/hosts/{id}/renew` | BossHost | Refused for a revoked host. |
| Heartbeat | `POST /v1/hosts/{id}/heartbeat` | BossHost | Every `heartbeat_seconds` (2 s). |
| Observed | `POST /v1/hosts/{id}/observed` | BossHost | Pushed at once (debounced 100 ms) on any VM state change. |
| Desired | `GET /v1/hosts/{id}/desired?since=<gen>&wait=<s≤30>` | BossHost | Long-poll. Level-based. |
| Guest calls | `POST /v1/hosts/{id}/guest-calls` | BossHost | Batched audit, ≤ 1000 rows. |
| Events | `POST /v1/hosts/{id}/events` | BossHost | **Optional.** A `404` makes the host stop posting events. |
| Run plane | `/v1/run-plane/*` | BossHost | The guest gateway's forward target (§4). |

### 2.1 Enrol

Request:

```json
{"join_token": "…", "csr_pem": "…", "svid_csr_pem": "…", "hostname": "boss-1",
 "backend": "firecracker", "facts": {…§2.3…}, "capacity": {"vcpu": 8, "mem_mib": 15990,
 "max_vms": 20, "used_vms": 0}, "labels": {"pool": "default"}}
```

`backend` is `firecracker` or `process`.

Response (`201`); renew returns the same certificate fields:

```json
{"host_id": "hst_…", "org_id": "…", "certificate_pem": "…", "certificate_not_after": "…",
 "renew_after": "…", "svid_intermediate_pem": "…", "ca_pem": "…", "spiffe_id": "spiffe://<td>/host/<id>",
 "trust_domain": "<install>.boss", "vm_prefix": "10.200.4.0/23", "heartbeat_seconds": 2,
 "endpoints": {"heartbeat": "/v1/hosts/<id>/heartbeat", …, "run_plane": "/v1/run-plane"}}
```

The host requires `svid_intermediate_pem`. An enrolment without it is refused on the host side.

### 2.2 Renew

```json
{"csr_pem": "…", "svid_csr_pem": "…"}
```

Fresh keys every time.

### 2.3 Heartbeat

```json
{"facts": {"kvm_live": true, "firecracker_live": true, "jailer_on": false,
           "seccomp_default": true, "smt_off": true, "ksm_off": true, "swap_off": true,
           "strict_secrets": true, "net_policy_ok": true,
           "reasons": {"jailer_on": "jailer lands in P8b"}},
 "capacity": {"vcpu": 8, "mem_mib": 15990, "max_vms": 20, "used_vms": 21},
 "allocatable": {"vms": 0},
 "pressure": {"mem_used_pct": 31.2, "disk_used_pct": 79.0, "load1": 0.4},
 "replicas": [{"replica_id": "rep_…", "vm_id": "vm_…", "deployment_id": "…", "vm_mode": "replica",
               "state": "serving", "reason": "", "vm_ip": "10.200.4.11", "image_digest": "…",
               "slots": 4, "busy": 0, "vsock_ping_age_ms": 212, "restarts": 0,
               "drops_originated": 0}],
 "warm_pools": {}, "bgp_state": "none", "observed_generation": 7,
 "status": "ready", "backend": "firecracker", "claims_frozen": true, "contract_version": "1.0"}
```

**Facts:**

- Facts are **measured**, never configured. A false fact carries its reason in `facts.reasons`.
- `net_policy_ok` is true only on the Firecracker backend, and only while the nftables base
  table's dispatch maps and fail-closed rules are intact.
- `seccomp_default` is true when Firecracker is live, because the host never passes
  `--no-seccomp` or a custom filter.
- `jailer_on` is false until P8b.

**Replica entries:**

- A desired VM that the host refuses still appears, with `state: "refused"` and a named
  `reason`.
- Reasons: `microvm_claims_frozen`, `capacity`, `host_revoked`, `vm_ip_outside_prefix`,
  `invalid_vm_id`, `invalid_vm_mode`.

**Response:**

```json
{"host_id": …, "desired_generation": 7, "status": "ready", "replicas_seen": 21, "renew": false,
 "heartbeat_seconds": 2}
```

### 2.4 Observed

```json
{"replicas": [ …the same entries as the heartbeat… ], "observed_generation": 7}
```

This is how watchdog transitions reach the control plane within about 100 ms: `unhealthy`,
`crashloop`, `serving` and the restart counts.

### 2.5 Desired state

The response is always `200`.

- `{"changed": false, "host_id": …, "generation": g}` means the wait elapsed with no newer
  generation.
- `{"changed": true, …}` carries the **complete** state:

```json
{"changed": true, "host_id": "hst_…", "generation": 8, "status": "ready", "vm_prefix": "10.200.4.0/23",
 "vms": [{"replica_id": "rep_…", "vm_id": "vm_…", "vm_mode": "replica", "state": "running",
          "deployment_id": "…", "agent_version": "3", "image_digest": "sha256:…",
          "resources": {"vcpu": 1, "mem_mib": 512, "slots": 4}, "slots": 4, "weight": 1,
          "vm_ip": "10.200.4.11", "vip": null, "vip_ports": [], "strict_secrets": true,
          "spiffe_id": "spiffe://<td>/host/<id>/vm/<vm>", "generation": 3}],
 "pools": [], "endpoints": {"run_plane": "/v1/run-plane", …}, "fences": [], "revocations": []}
```

**Watch rules:**

- **Level-based.** A VM absent from `vms`, or present with `state: "stopped"`, is stopped.
  Everything else converges toward the listed spec.
- **`since=0` is a full resync.** The host uses it at start and after any watch error.

**Reconciler safety:**

- The host **never stops anything** before its first live desired state.
- With only the cached copy (bbolt, with handles stripped), it keeps VMs and stops none.

**VM fields:**

- **`vm_id`** must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$`. The control plane mints
  `vm_<16 hex>`.
  - `vm_id` names the VM directory and the process marker.
  - It is mapped to Firecracker's `--id` alphabet by `_` → `-`.
- **`resources`:** the host reads `vcpu` (or `vcpus`, `cpus`) and `mem_mib` (or `memory_mib`).
  The defaults are 1 vCPU and 256 MiB.
- **A change of `image_digest`, `vm_mode`, `vm_ip`, `vcpu` or `mem_mib`** replaces the VM. P8a
  stops and starts it; P8b rolls with surge 1.
- **`vm_ip`** is required for `replica` VMs on the Firecracker backend, inside the host's
  `vm_prefix`.

**Extensions the host honours when present.** The Python side omits them in P8a:

- `directors: ["192.0.2.10"]`: the IPv4 addresses allowed to send IPIP to replicas (P8b).
- `claims_frozen: true`: ORed with the host's own `BOSS_MICROVM_CLAIMS_FROZEN` for `per_run`.
- Per VM:
  - `replica_handle`;
  - `runs: [{run_id, epoch, handles: {model, egress, mcp, run}}]`.
  These are opaque handles, kept in host memory only and never written to bbolt.
- `fences: [{run_id, min_epoch}]` and `revocations: [<handle fingerprint>]`: enforced by the
  guest gateway.

### 2.6 Guest calls (audit)

```json
{"calls": [{"call_id": "<24 hex>", "at": "…", "vm_id": "vm_…", "run_id": "…", "epoch": 3,
            "deployment_id": "…", "route": "run_plane", "dest_host": "cp:8443", "method": "POST",
            "path_template": "/v1/run-plane/runs/*", "status": 200, "bytes_in": 812,
            "bytes_out": 64, "duration_ms": 4, "handle_fp": "<16 hex>", "verdict": "allow",
            "rule_id": "run-plane", "rejected_by": "", "trace_id": "…"}]}
```

**Fields:**

- `route` is one of `model`, `git`, `api`, `mcp`, `run_plane`, `connect`.
- `verdict` is `allow` or `deny`.
- `call_id` is the per-host idempotency key, so a retried batch is safe.

**What a row never contains:**

- Rows never carry a body, a query string or a handle; `handle_fp` is a fingerprint.
- `path_template` keeps at most three path segments.
- The control plane refuses any row containing a handle-shaped string.

**Buffering:** rows batch every second, at most 200 per post. While the control plane is down,
up to 10 000 rows are kept, and older ones are dropped and counted.

### 2.7 Events (optional)

```json
{"events": [{"at": "…", "vm_id": "vm_…", "kind": "crashloop", "from": "unhealthy",
             "to": "crashloop", "detail": {"reason": "process_exit", "restarts": "3"},
             "actor": "watchdog"}]}
```

`kind` is one of: `vm_state`, `restart`, `crashloop`, `handle_replay`, `refused`, `adopted`,
`orphan_tap_removed`.

The control plane may answer `404`. The host then stops posting events, and state still arrives
through heartbeat and observed.

---

## 3. Lifecycle, watchdog and crash recovery (LLD §3.3, §3.16.1, §3.21)

### 3.1 Reconcile

The reconcile loop runs every 500 ms, and immediately on a desired change:

- it creates missing VMs;
- it stops VMs that are no longer desired;
- it replaces VMs whose spec changed;
- it sweeps taps that no VM owns.

### 3.2 Watchdog

- It sends a vsock ping every **500 ms**.
- A Firecracker or process **exit** is detected at once.
- **3 missed pings** after a 15 s boot grace mean `unhealthy`.
- On `unhealthy` the host withdraws first, then restarts.
- After **3 restarts within 10 min**, the next failure is `crashloop`:
  - the VM is stopped;
  - a fresh attempt is made after the window.
- The first answered control exchange is the **bootstrap**. It carries `vm_id`, the mode, a fresh
  32-byte seed, and for replicas `vm_ip`, `169.254.1.1` as gateway, and the MAC. It carries no
  token.

### 3.3 Crash recovery

- VMs run in their own session, so they survive `kill -9 boss-hostd`.
- On start the host scans:
  - **`/proc`** for VM processes: Firecracker `--api-sock <run dir>/<vm_id>/…`, or boss-vsockd
    `--boss-vm-id=`;
  - **netlink** for `bt*` taps.
- Found VMs are **adopted**:
  - gateway listeners are re-opened;
  - the tap policy is re-applied (counters are kept);
  - the bootstrap is re-sent (it is idempotent).
  They are never restarted.
- Duplicate processes for one `vm_id` are stopped.
- The bbolt cache is optional and can be deleted at any time.

---

## 4. The guest gateway (LLD §3.8)

### 4.1 Channels and routes

There is one set of listeners per VM:

- **Firecracker:** Unix sockets `<dir>/v.sock_<port>`, which Firecracker connects to for
  guest-initiated vsock.
- **Process backend:** `<dir>/g_<port>.sock`.

The accepting socket is the VM's identity. Inside the guest, `boss-vsockd` relays `127.0.0.1:<port>`
to the same vsock port.

| Port | Route | Rule (first match wins; terminal deny) | Upstream |
|---|---|---|---|
| 7101 | model | `POST /anthropic/v1/messages`, `/openai/v1/chat/completions`, `/openai/v1/responses` | `endpoints.model_gateway` (P8b) |
| 7102 | git / api | `GET,POST /git/…`; `GET,POST,PATCH,PUT /api/…` | `endpoints.egress` (P8b) |
| 7103 | mcp | `GET,POST,DELETE /mcp/…` | `endpoints.mcp_gateway` (P8b) |
| 7104 | run_plane | any `/v1/run-plane/…` | `endpoints.run_plane` (the control plane) |
| 7105 | connect | denied until P8b | — |
| 5000 | control (host-initiated) | `bootstrap`, `ping`, `ifaces`, `selfcheck_egress`, `selfcheck_gateway` | — |

### 4.2 Per request

1. **Identify** the VM by its socket.
2. **Match** the request against the route table.
3. **Resolve** the handle from `Authorization`, `X-Api-Key` or `Proxy-Authorization`.
   - The handle maps to `(run, epoch, vm)`.
   - A handle from another VM's socket is refused (`handle_replay`), **revoked**, and reported.
   - A fenced epoch gets `409 fenced`.
   - On the run plane, a **replica needs no handle**: its socket is its identity.
4. **Strip** every guest credential and identity header:
   - `Authorization`, `Proxy-Authorization`, `X-Api-Key`, `Api-Key`, `Cookie`;
   - `Forwarded`, `X-Forwarded-*`, `X-Real-Ip`;
   - every `X-Boss-*` and `Boss-*`.
   On the run-plane route only, the lease headers `X-Boss-Job`, `X-Boss-Run-Token` and
   `X-Boss-Fence-Epoch` pass through. They are data the control plane checks against Postgres.
5. **Forward.**
   - **Run plane:** the host's `Authorization: BossHost` proof (for the upstream method and
     path), plus `X-Boss-Replica` set **by the gateway** from the socket's replica.
   - **Central gateways (P8b):** mTLS with the VM's SVID.
   - **Every forward:** a `Boss-Run-Assertion` (ES256 JWS signed with the VM's SVID key; claims
     `iss` = SPIFFE ID, `aud` = route, `run`, `epoch`, `vm`, `deployment`, `iat`, `exp` 60 s,
     `jti`).
   - Responses stream chunk by chunk.
6. **Audit:** one `guest_calls` row.

### 4.3 Endpoints and failure

- Relative endpoints (`/v1/run-plane`) resolve against the control-plane URL.
- The guest path is not prefixed twice.
- An absent endpoint, or plain `http` to a non-loopback address, gives `503 gateway_unconfigured`.
- An unreachable upstream gives `502 upstream_unreachable`.
- There is never a direct fallback path.
- Each VM is capped at 256 connections.

### 4.4 SVIDs (LLD §3.12.1)

- Format: `spiffe://<trust_domain>/host/<host_id>/vm/<vm_id>`.
- Minted by the host from its intermediate, lifetime 1 h, renewed at two thirds.
- Held in memory only, and never given to the guest.
- Claims are carried as subject OU entries: `deployment:<id>`, `version:<v>`, `image:<digest>`.

---

## 5. Networking (LLD §3.7)

### 5.1 Replica VMs

**The tap:**

- One tap per VM, `bt<12 hex of sha256(vm_id)>`.
- Persistent and owned by the hostd uid.
- MAC `02:fc:<vm_ip bytes>`.
- Route `vm_ip/32 dev <tap>`.
- `proxy_arp=1`, `rp_filter=0`, IPv6 off on the tap.
- The guest configures `vm_ip/32` and `default via 169.254.1.1 onlink` from the bootstrap.

**nftables (table `inet boss_hostd`, default drop, every drop counted):**

- **Unknown taps:** any `bt*` interface without its own chain is dropped (`drop_unknown_tap`).
- **IN** (`in_<tap>`): IPIP from `@directors` to `vm_ip` is accepted. Everything else is
  dropped (`indrop_`).
- **OUT** (`out_<tap>`):
  - `→ 169.254.169.254` is dropped (`meta_`).
  - A reply is accepted only when all hold: src is the VIP, sport ∈ `vip_ports`, it is not a bare
    SYN, and the destination is in `peers_<tap>`.
  - Everything else is dropped (`orig_`): DNS, VM↔VM, internet, host addresses.

**The P8a-T4 evidence** is `orig_` + `meta_` = 0. It is reported per replica as
`drops_originated`.

### 5.2 `per_run` VMs

- No `network-interfaces` entry at all: only `lo` and vsock.
- Off by default behind `BOSS_MICROVM_CLAIMS_FROZEN`.

### 5.3 The privileged helper

- `boss-hostd netadm --socket S --allow-uid U` holds `CAP_NET_ADMIN`. Under systemd it runs with
  `AmbientCapabilities=CAP_NET_ADMIN`.
- It accepts one uid (`SO_PEERCRED`).
- It accepts only these ops, each re-validated: `ensure`, `remove`, `taps`, `counters`,
  `directors`, `policy`.
- Validation:
  - a tap name must match `^bt[0-9a-f]{12}$`;
  - `vm_ip` must be inside `vm_prefix`;
  - the VIP and directors must be IPv4;
  - ports must be 1–65535.
- boss-hostd itself runs unprivileged.

---

## 6. Host-local HTTP API (control plane → host)

- **Legacy coding lane:** `/v1/sandbox/*`, byte-compatible, static token, unchanged until P8d.
- **`GET /v1/vms`:** lists the fleet VMs as the heartbeat entries.
- **`/health`:** reports the VM manager's real capacity (`capacity` = max VMs, `available` = free)
  instead of the old `capacity = 1`, plus a `fleet` block.
- The LLD's `claim`, `forward`, `cancel`, `drain`, `logs` and `fs` VM calls are P8b.

---

## 7. Configuration

| Variable | Meaning |
|---|---|
| `BOSS_HOSTD_CP_URL` | Control plane base URL. It enables the fleet agent. |
| `BOSS_HOSTD_CP_CA` | PEM bundle pinned for the control plane's TLS server certificate. |
| `BOSS_HOSTD_JOIN_TOKEN_FILE` | The one-time join token, read on first start only. |
| `BOSS_HOSTD_STATE_DIR` | `pki/` (0600 keys, pinned fleet CA) and `cache.db`. Default `/var/lib/boss-hostd`. |
| `BOSS_HOSTD_RUN_DIR` | Per-VM directories. Keep the path short, because of the Unix socket limit. Default `/run/boss-hostd/vms`. |
| `BOSS_HOSTD_VM_BACKEND` | `process` (laptop/CI) or `firecracker`. |
| `BOSS_HOSTD_GUEST_BIN` | `boss-vsockd`, for the process backend. |
| `BOSS_HOSTD_FC_ROOTFS` | Read-only base rootfs with `/sbin/boss-vsockd`. See `scripts/sandbox/hostd-guest-bake.sh`. |
| `BOSS_SANDBOX_FC_BIN`, `BOSS_SANDBOX_FC_KERNEL` | Firecracker binary and `vmlinux`, shared with the legacy lane. |
| `BOSS_HOSTD_NETADM_SOCKET` | The helper's socket, or `inprocess` when hostd has `CAP_NET_ADMIN` itself. |
| `BOSS_HOSTD_MAX_VMS`, `BOSS_HOSTD_POOL`, `BOSS_HOSTD_STRICT_SECRETS` | Capacity cap (20), pool label, strict-secrets fact (true). |
| `BOSS_MICROVM_CLAIMS_FROZEN` | Default `true`. Gates `per_run` VMs and the legacy lane. Replicas are not gated. |

---

## Decisions

These are the choices made where the LLD was open or silent. Items 1–5 align the Go host with the
Python control plane as it was built.

1. **Host auth is a proof of possession plus an optional TLS client certificate.** The LLD says
   "mTLS on both directions".
   - Python serves FastAPI behind any TLS terminator, so it authenticates the host by a `BossHost`
     JWS signed with the host-certificate key, pinned by fingerprint, with a single-use `jti`.
   - The host also always presents the client certificate, so a terminator can do real mTLS and
     forward it.
   - The trust anchor is the same: the host certificate issued by the BOSS fleet CA.
2. **Two pins, not one.**
   - `BOSS_HOSTD_CP_CA` pins the control plane's **TLS** certificate.
   - The **fleet CA** (`ca_pem`) is pinned at enrolment and must not change on renewal.
3. **The join token travels in the enrol body** (`join_token`), not in a header, over the pinned
   TLS channel.
4. **Desired is always `200` with `changed`.** It is not `304`. `wait` is capped at 30 s.
5. **Field names follow the Python columns:** `kvm_live`, `jailer_on`, `replicas[].replica_id`,
   `handle_fp`, `dest_host`, `run_plane`, `allow`/`deny`, and `call_id` for idempotency.
6. **`POST /observed` is pushed on every state change.** This is how "report state changes to the
   control plane" is met without waiting for the 2 s heartbeat. `POST /events` is optional and
   detail-only.
7. **The run plane goes through the guest gateway with the host's identity.**
   - The replica's socket is its identity (no handle in P8a).
   - The gateway sets `X-Boss-Replica`, so a guest cannot claim another replica.
   - Lease headers pass through.
   - Guest `Authorization`, cookies and every other `X-Boss-*` header never survive.
8. **Reply-only egress uses a learned peer set, not `ct state established`.**
   - The host only sees the outer IPIP flow, so conntrack has no entry for the inner TCP flow, and
     the LLD's rule would drop every reply.
   - Instead, inbound IPIP-carrying-TCP records the inner source address in `peers_<tap>` (10 min
     timeout), and a reply must be VIP:vip_port → a recorded peer, not a bare SYN.
   - **Residual:** a compromised replica could still send non-SYN TCP to a caller that reached it
     in the last 10 minutes. P8b can tighten this to (peer, port) once nft supports typed
     concatenation of the inner raw payload, or by terminating IPIP on the host.
9. **vm_id is assigned by the control plane.** Tap name, MAC, VM directory and process marker all
   derive from it deterministically. That is what makes crash recovery duplicate-free with no local
   state.
10. **Per-VM disk:** VMs share one **read-only base rootfs**. The init is `boss-vsockd`, and
    writable paths are tmpfs. P8b adds the overlay drive and the signed image pipeline.
11. **The headless start never acts destructively.** The cached desired state (handles stripped)
    can keep or restart VMs, but only a live desired state can stop them or sweep taps.
12. **`per_run` gating.**
    - The operator decision makes replicas the activated mode.
    - `per_run` is refused with `microvm_claims_frozen` while either the host switch or the
      desired flag is frozen.
    - The LLD §3.17.4 rule "frozen ⇒ nothing placed" is applied to `per_run` only.

## Asks for the control-plane side

- **Resources:** use `vcpu` and `mem_mib` in `agent_deployments.resources`. The host reads those
  keys.
- **Events (optional):** add `POST /v1/hosts/{id}/events` if crashloop and handle-replay detail
  should land in `host_events`. Without it the states still arrive through heartbeat and observed.
- **P8b:**
  - send `directors`;
  - send per-run `runs[].handles`;
  - send `fences` and `revocations` in desired state.
  The host already enforces them.

## Left for P8b (seams in place)

- `vmm.Manager.Restore`/`Snapshot` return `ErrNotImplemented`.
- `Firecracker.Launcher` is where the jailer wraps the argv.
- The API socket is already enabled, which snapshots need.
- Warm pools, the claim call and the setup cache.
- Signed images and the overlay drive.
- The VIP on `tunl0` in the guest.
- Director registration and withdraw (weight 0).
- The TLS self-check before `serving`.
- The central model, egress and MCP gateways the routes already point at.
- Per-run DPoP-bound tokens held by the host.
