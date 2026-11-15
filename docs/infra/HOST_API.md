# boss-hostd ⇄ control plane: the host API (P8a, P8b)

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

**Contract version:** `1.1`. Changes are additive, and each side ignores fields it does not know.

**1.1 (P8b-9/11, `p8/b-tls-d11`, BOSSAPP-1076/1078): the agent CA, the replica's TLS listener
and AP-D11.** A replica's boss-runtime generates its serving key in the VM and sends only a CSR to
`POST /v1/run-plane/replica/cert` (through the guest gateway, which adds the host proof and
`X-Boss-Replica`); it serves the Agent Protocol and A2A on VIP:443 and asks
`POST /v1/run-plane/admit` (the PDP) for every request. `strong_isolation(deployment)` is now
derived from the facts below; the heartbeat stores the host's verdict as `isolation_ok` (§10).
The guest sets `tcp_syncookies=1` and a 4096 SYN/accept backlog. No new host-side call.

**1.1 (P8b-10, `p8/b-lbd`, BOSSAPP-1077): the IPVS directors.** `boss-lbd` (Go, `cmd/boss-lbd`,
`internal/lbd`) enrols through the host enrol path with a join token issued for `role:
director`, long-polls **its own desired state** (§9.2: every VIP with its SERVING real servers,
the route per host, the VRRP grouping the control plane decides), programs IPVS over generic
netlink in **GRE tunnel mode** (`IPVS_DEST_ATTR_TUN_TYPE = GRE`, decision 8), renders
keepalived's **VRRP-only** configuration, runs the IPVS connection sync daemons, holds the
**director lease** per VRRP group through its heartbeat (§9.3), health-checks every real server
through the real data path (§9.5) and accepts **direct withdraw pushes from hosts over mTLS**
(§9.4). Hosts report their `address` and push `PUT/DELETE /v1/vips/{vip}/members/{replica}` to
every director in `director_push` and to the control plane. The guest puts the VIP on `lo` and
decapsulates GRE on `bgre0` (§9.6). Measured on boss-1 (netns lab): §9.7.

**1.1 (control plane, `p8/a-runplane`, operator decisions 2026-11-14, LLD §3.13.0 and its
REVISION):** work reaches replicas through **ONE long-poll per host**, `POST
/v1/run-plane/hosts/next` (§2.8), whose body carries **no replica list** — BOSS keeps its own
model of every replica's slots, fed by `/observed` with a `state_gen` (§2.4), and asks for the
full map only on a generation mismatch (`PUT /v1/hosts/{id}/replicas`, §2.9). The host hands each
claimed run to the VM's **work inbox** (§4.5). VMs never poll BOSS; the per-replica
`POST /v1/run-plane/next` is gone. The host keeps each run's token in memory and attaches it to
that run's run-plane calls (§4.2 step 5); the guest never holds one.

**1.1 (host, `p8/a-host`, review fixes 2026-11-14):** replica egress is GRE-based with exact
per-flow reply learning and an internal-net drop (§5.1, proven in a netns lab on boss-1); the
privileged helper takes its VM prefix and tap owner from its own flags only (§5.3); adopted VMs are
compared with desired state and replaced on mismatch (§3.3); crashloop → one replacement →
quarantine, and a 24 h headless cap (§3.2); stricter gateway path and header rules (§4.2).

**1.1 (host, `p8/b-snapshots-jailer`, P8b-1 and P8b-8, BOSSAPP-1068/1075):** every Firecracker VM
runs under the **jailer** (§8: chroot, a uid/gid per VM, new PID namespace, a netns per VM, cgroup v2
`cpu.max`/`memory.max`, default seccomp, API-socket mode); `jailer_on` and `seccomp_default` are
measured from the running processes (§2.3). VMs boot from the shared read-only base plus a **per-VM overlay drive**; the first
VM of a shape is **snapshotted** clean and the rest are **restored** with the memory file mapped
`MAP_PRIVATE`; **warm pools** (§3.4) serve `per_run` claims (min_warm, default 2, frozen by default)
and keep one spare per replica shape; snapshot **hygiene** (S15) refuses to snapshot any VM that saw
an identity, handle or run, records `clean_attested`, keeps files 0600 and collects them with their
image digest; the bootstrap reseeds the guest RNG and clock; **rate limiters** on every drive and
tap (§8.3). Measured on boss-1 (KVM, Firecracker v1.10.1 + jailer): see §3.4.

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
| Work | `POST /v1/run-plane/hosts/next` | BossHost | **One** outstanding per host (§2.8). |
| Replica map | `PUT /v1/hosts/{id}/replicas` | BossHost | Only after `{"resync": true}` (§2.9). |
| Run plane | `/v1/run-plane/*` | BossHost | The guest gateway's forward target (§4). |
| Hand back | `POST /v1/run-plane/runs/{run}/lease` | BossHost + lease headers | `{"hand_back": true, "action": "hand_back"}` (both spellings) (§2.8). |
| Serving cert | `POST /v1/run-plane/replica/cert` | BossHost + `X-Boss-Replica` | The guest's CSR, forwarded as-is (§10.1). |
| Trust | `GET /v1/run-plane/replica/trust` | BossHost + `X-Boss-Replica` | JWKS, CA bundles, revoked serials, the signed card (§10.2). |

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
{"facts": {"kvm_live": true, "firecracker_live": true, "jailer_on": true,
           "seccomp_default": true, "smt_off": true, "ksm_off": true, "swap_off": true,
           "strict_secrets": true, "net_policy_ok": true,
           "reasons": {}},
 "capacity": {"vcpu": 8, "mem_mib": 15990, "max_vms": 20, "used_vms": 21},
 "allocatable": {"vms": 0},
 "pressure": {"mem_used_pct": 31.2, "disk_used_pct": 79.0, "load1": 0.4},
 "replicas": [{"replica_id": "rep_…", "vm_id": "vm_…", "deployment_id": "…", "vm_mode": "replica",
               "state": "serving", "reason": "", "vm_ip": "10.200.4.11", "image_digest": "…",
               "slots": 4, "busy": 0, "vsock_ping_age_ms": 212, "restarts": 0,
               "drops_originated": 0}],
 "warm_pools": {"coder": {"depth": 2, "min_warm": 2}, "agent-x": {"depth": 1, "min_warm": 1}},
 "bgp_state": "none", "observed_generation": 7,
 "status": "ready", "backend": "firecracker", "claims_frozen": true, "contract_version": "1.1"}
```

**Facts:**

- Facts are **measured**, never configured. A false fact carries its reason in `facts.reasons`.
- `net_policy_ok` is true only on the Firecracker backend, and only while the nftables base
  table's dispatch maps and fail-closed rules are intact.
- `seccomp_default` is **measured**: true when Firecracker is live and every running Firecracker
  process shows `Seccomp: 2` (filter mode) in `/proc/<pid>/status`; the host never passes
  `--no-seccomp` or a custom filter (to Firecracker or to the jailer). A VM without a filter names
  itself in the reason.
- `jailer_on` is true only when the jailer is configured (`BOSS_HOSTD_JAILER_BIN`), usable
  (binary present and executable, boss-hostd runs as root, cgroup v2 with the `cpu` and `memory`
  controllers) **and every running Firecracker VM of this host is inside a jail** (process scan). A
  false carries its reason: `jailer_binary_missing`, `jailer_needs_root`, `cgroup_v2_missing`,
  `jailer_chroot_base_unusable`, or `VM <id> runs outside the jailer`.
- `warm_pools` is depth (ready warm VMs, no identity yet) against `min_warm` per deployment: the
  `per_run` pools of desired state (`min_warm` default 2; depth 0 while claims are frozen, so the
  control plane sees why) and the replica shapes' surge spares (§3.4). `capacity.used_vms` counts
  warm VMs too.

**`address` (P8b-10):** the host's reachable IPv4, in the heartbeat and the enrol body
(`BOSS_HOSTD_ADDRESS`, default: the source address toward the control plane). The directors route
the host's `vm_prefix` through it (one static route per host, §5.1). A change bumps every
director's desired generation.

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
{"replicas": [ …the same entries as the heartbeat… ], "observed_generation": 7, "state_gen": 42}
```

This is how watchdog transitions reach the control plane within about 100 ms: `unhealthy`,
`crashloop`, `serving` and the restart counts.

**`state_gen` (1.1).** The host's monotonically increasing generation of its replica-state map.
Bump it on **every** replica state change (serving, draining, unhealthy, gone…) and send the
changed entries here with the new value; BOSS stores the generation with them. BOSS's claim uses
only replicas it has recorded as `serving` (§2.8). A replica entry the host does not hold is
refused **403 `replica_not_on_host`**. The heartbeat's `replicas` list stays as it is (a full list
every 2 s; it also lets BOSS mark a stopped replica `gone`) but does **not** store `state_gen`.

**Stopped replicas (host).** When the host stops a VM (no longer desired, spec changed,
crashloop), the replica is reported with `state: "stopped"` (and the reason) in every heartbeat
and `/observed` push until one carrying it succeeds, then dropped. BOSS must see the stop: its
claim must never keep a replica the host no longer runs.

**Host behaviour around `state_gen`:**
- every VM state transition (and a change of the inbox's `total_slots`) bumps it and triggers an
  `/observed` push (debounced 100 ms); detail-only events (a hand-back, a handle replay) do not;
- before each `hosts/next` the host waits up to 300 ms for a pending push to land, so steady state
  never resyncs (P8a-T6 on boss-1: 50 microVMs, 0 resyncs in steady state).

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
- **A change of `image_digest`, `vm_mode`, `vm_ip`, `vcpu` or `mem_mib`** replaces the VM: the
  new one is a warm claim or a restore when the shape has a snapshot (§3.4), a cold boot otherwise.
- **`pools`** (`per_run`): `[{deployment_id, image_digest, resources, min_warm}]`. The host keeps
  `min_warm` (default 2) warm VMs of that shape while claims are not frozen (§3.4). A desired
  `per_run` VM of the same deployment and shape is served from the pool.
- **`vm_ip`** is required for `replica` VMs on the Firecracker backend, inside the host's
  `vm_prefix`.

**Extensions the host honours when present.** The Python side omits them in P8a:

- `directors: ["192.0.2.10"]`: the IPv4 addresses allowed to send GRE-encapsulated caller traffic to replicas (P8b). **Sent since P8b-10:** the live directors' addresses.
- `director_push: [{"id": "dir_…", "address": "192.0.2.10", "port": 7446}]` (P8b-10): where the host pushes real-server registrations and withdraws directly (§9.4), besides the control plane.
- `claims_frozen: true`: ORed with the host's own `BOSS_MICROVM_CLAIMS_FROZEN` for `per_run`.
- Per VM:
  - `replica_handle`;
  - `runs: [{run_id, epoch, handles: {model, egress, mcp, run}}]`.
  These are opaque handles, kept in host memory only and never written to bbolt. From P8b the
  work item (`hosts/next` → `runs[].handles`, §4.5) carries `mdl` (the model handle, bound to the
  replica's VM) and `mdl_provider`; the host passes it to the guest with the `run` op.
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
`orphan_tap_removed`, and the detail-only P8b kinds `vm_timing` (`detail.kind` ∈ `cold_boot`,
`restore`, `snapshot`, `warm_claim`, `cold_claim`; `detail.ms`), `snapshot` (`key`, `image`,
`clean_attested`, `mem_bytes`), `snapshot_failed`, `snapshot_gc`, `warm_ready`, `warm_failed`,
`warm_dropped`. Detail-only kinds bump no `state_gen`.

The control plane may answer `404`. The host then stops posting events, and state still arrives
through heartbeat and observed.

---

### 2.8 Work: one long-poll per host (1.1; LLD §3.13.0)

```
POST /v1/run-plane/hosts/next          Authorization: BossHost …
{"wait_ms": 25000, "state_gen": 42}
```

The body is **only** `wait_ms` (≤ 30000) and `state_gen` (and an optional lease `ttl`, 5–120 s,
default 30). There is **no replica list**: BOSS keeps its own model of the host's replicas:

- **who may take work:** the host's replicas BOSS has recorded `serving` (through `/observed` or
  the resync map), wanted running, of a live `runtime: microvm` deployment;
- **total slots:** `agent_replicas.slots` (from the deployment's `resources.slots`, default 4);
- **busy:** the leases `replica:<id>` holds right now. A run's final write (settle, fail,
  hand-back) releases it; a crash frees it when its 30 s lease expires.

Answers (always `200`):

| Answer | Meaning |
|---|---|
| `{"resync": true, "runs": [], "state_gen": <stored>}` | `state_gen` ≠ the generation BOSS stored for this host. PUT the full map once (§2.9), then poll again. |
| `{"runs": [ … ]}` | Claimed now, or after a wait. Possibly empty after `wait_ms`. |

```json
{"runs": [{"run_id": "arun_…", "job_id": "job_…", "replica_id": "rep_…", "epoch": 3,
           "run_token": "lease_…", "kind": "agent.run", "lease_expires_at": "…"}]}
```

- **In one transaction** BOSS claims queued jobs whose `target` is `deployment:<d>` for the host's
  serving deployments (`FOR UPDATE SKIP LOCKED`), up to the replicas' free slots, assigns each run
  to the replica of its deployment with **the most free slots** (round robin on ties), and leases
  it to `replica:<id>` at `epoch` = the job's previous epoch + 1.
- **Nothing claimable:** the request waits (≤ `wait_ms`) on BOSS's event hub for an admission to
  one of the host's deployments (or for the host's own state to change: a freed slot, a replica
  now serving), then claims again. One sweep per API process every 5 s catches a lost
  notification.
- **`run_token` is the host's.** Keep it in memory (never bbolt, never a log), attach it to that
  run's run-plane calls (§4.2 step 5), and forget it when the run settles. It never enters the VM.
- **Then** deliver each run to its replica's work inbox (§4.5) and **immediately** issue the next
  `hosts/next`. Keep exactly **one** outstanding per host, whatever the number of VMs.
- **The VM refuses, is dead, or does not answer in 2 s:** hand the run back:
  `POST /v1/run-plane/runs/{run_id}/lease {"hand_back": true}` with the lease headers
  (`X-Boss-Replica`, `X-Boss-Job`, `X-Boss-Run-Token`, `X-Boss-Fence-Epoch`). BOSS re-queues the job
  at once (no attempt spent) and wakes the deployment's hosts; the next claim leases it at
  epoch + 1, and the old epoch's calls get `409 fenced`. Push the replica's real state through
  `/observed` if it changed (draining, unhealthy), so BOSS stops assigning to it.
- **Host refusal rules (local only):** the host refuses at once (and hands back) a run for a replica
  it does not run (`unknown_replica`), a VM not running, a VM with no free slot by its own 500 ms
  ping (`no_free_slot`), or an unreachable inbox. Each such refusal is treated as drift: the host
  bumps `state_gen` and pushes its real map, so BOSS stops assigning there. A refused delivery
  releases the host's local slot reservation.

**`runs[].handles` (P8b S10/S12).** Each assignment also carries the GUEST's placeholders for
that run — `mdl` (`boss_mdl_…`), `mcpgw` (`boss_mcpgw_…`) and, for the MCP gateway, `mcp_servers`,
a `{name: guest URL}` object the host passes through untouched. The host binds every string
handle to `(run, epoch, the VM it delivers to)` in its gateway (`BindRun`) before the VM can
present one, hands them to the VM in the work line (§4.5), and never writes them. They are
unbound when the lease ends or the run is handed back.

### 2.9 The full replica map, after a resync (1.1)

```
PUT /v1/hosts/{id}/replicas            Authorization: BossHost …
{"state_gen": 42, "replicas": [{"replica_id": "rep_…", "vm_id": "vm_…", "state": "serving",
                                "busy": 1, "vm_ip": "…", "restarts": 0}, …]}
```

- Every VM the host holds, as in the heartbeat. A replica of this host the map omits becomes
  `gone`; one the host does not hold is refused **403 `replica_not_on_host`**.
- BOSS stores `state_gen`; the next poll with that generation claims. A full map is never sent
  otherwise.

## 3. Lifecycle, watchdog and crash recovery (LLD §3.3, §3.16.1, §3.21)

### 3.1 Reconcile

The reconcile loop runs every 500 ms, and immediately on a desired change:

- it creates missing VMs;
- it stops VMs that are no longer desired;
- it replaces VMs whose spec changed;
- it sweeps taps that no VM owns.

### 3.2 Watchdog

- It sends a vsock ping every **500 ms**. The whole ping (dial, vsock handshake, exchange) is
  bounded to 90 % of the tick, so **3 misses take ≤ 1.5 s**.
- A Firecracker or process **exit** is detected at once.
- **3 missed pings** after a 15 s boot grace mean `unhealthy`.
- On `unhealthy` the host withdraws first, then restarts.
- After **3 restarts within 10 min**, the next failure is `crashloop`:
  - the VM is stopped;
  - after the window, **one** replacement attempt is made, keeping the restart history;
  - if that replacement fails too, the VM is **`quarantined`**: nothing restarts it until its
    spec changes or it leaves desired state (event `quarantined`);
  - a replacement that stays healthy for a full window clears the history.
- **Headless cap.** With no control-plane contact for `BOSS_HOSTD_HEADLESS_MAX_SECONDS` (default
  86400), the host stops serving: the gateway answers `503 host_not_serving`, no work is
  requested, the heartbeat status is `headless_expired`. VMs keep running.
- **Slots (1.1).** The ping's answer carries the runtime's `busy_slots` and `total_slots`
  (boss-vsockd asks the work inbox, §4.5, with `{"op":"slots"}`; when the control ping carries no
  slots, the host asks the inbox `{"op":"slots"}` itself within the same tick). A draining or unhealthy VM
  reports 0 total. The host uses them to refuse or hand back and to detect drift; it never sends
  them to BOSS (BOSS counts leases itself, §2.8). A change that alters serving / draining is an
  `/observed` push with a bumped `state_gen`.
- The first answered control exchange is the **bootstrap**. It carries `vm_id`, the mode, a fresh
  32-byte seed, the VM's vsock `cid`, the host clock (`host_time_unix_nano`), `restored`, and for
  replicas `vm_ip`, `169.254.1.1` as gateway, the VIP and the MAC; for a `per_run` claim the run's
  opaque handles. It carries no token. Sending it **taints** the VM: it is never snapshotted
  afterwards (§3.4).

### 3.3 Crash recovery

- VMs run in their own session, so they survive `kill -9 boss-hostd`.
- On start the host scans:
  - **`/proc`** for VM processes: Firecracker `--api-sock <run dir>/<vm_id>/…`, or boss-vsockd
    `--boss-vm-id=`;
  - **netlink** for `bt*` taps.
- Found VMs are **compared with desired state** before adoption: every VM directory holds the
  spec it was started with (`boss-spec.json`, 0600: image digest, mode, `vm_ip`, vCPU, memory,
  NIC). A VM whose record differs from desired (or has none) is stopped and replaced
  (event `adopted_mismatch`, field named); the others are adopted untouched.
- Adopted VMs:
  - gateway listeners are re-opened;
  - the tap policy is re-applied (counters are kept);
  - the bootstrap is re-sent (it is idempotent).
  They are never restarted.
- Warm VMs (`boss-spec.json` says `warm: true`) go back into their pool; a claimed warm VM is
  known by the `vm_id` in its record, not by its slot directory name.
- Jailed VMs are found by the `--id` the jailer gave Firecracker plus the pid file in that jail
  (the jailer pivots the root, so `/proc/<pid>/root` is `/`). Orphan namespaces (`bv-*` without a
  jail directory) are deleted on reconcile.
- Duplicate processes for one `vm_id` are stopped.
- The bbolt cache is optional and can be deleted at any time.

### 3.4 Snapshots, warm pools and hygiene (LLD §2.10, §3.4; S15)

**Shape (snapshot key):** `(image_digest, kernel_digest, vcpu, mem_mib, runtime_version)` **plus
`vm_mode`**, because a `per_run` snapshot has no network device and a replica snapshot has one. The
kernel digest is sha256 of the configured `vmlinux`; `runtime_version` is `BOSS_HOSTD_RUNTIME_VERSION`
(the baked boss-runtime). The store directory name is the first 24 hex of sha256 over the tuple.

**Store:** `BOSS_HOSTD_SNAP_DIR/<key>/{meta.json, mem, state, overlay.ext4}` (default
`<state dir>/snapshots`). `meta.json` is written last, so a half-written snapshot is never found.
`meta.json` records the shape, `clean_attested`, `created_at`, `source_vm_id`, `mem_bytes`.

**When a snapshot is taken.** The first VM of a shape on a host cold-boots (shared read-only base
rootfs + fresh overlay from a host template made once with `mke2fs`), and as soon as its control
channel answers a **ping** (boss-vsockd up: "runtime imported, waiting for identity") the host
pauses it, writes a full snapshot (`PUT /snapshot/create`, memory + device state) and a sparse copy
of its overlay, resumes it, and the VM goes on to be claimed or warm. Nothing identity-bearing has
reached the guest at that point.

**Hygiene (S15):**

1. A VM is **tainted** the moment the host sends it a bootstrap (identity and handles) or hands it
   a run; `Snapshot` on a tainted VM is refused with `snapshot refused: VM has received an
   identity, handle or run`. Only a clean VM can be snapshotted, and the record says so
   (`clean_attested: true`).
2. The guest holds no credential-bearing client before the bootstrap: boss-vsockd keeps nothing
   but loopback relays. **Requirement for `boss-runtime` (Python, in the image):** build HTTP
   clients lazily, after the bootstrap; generate the replica's TLS key only after the bootstrap
   (never at import time), or every clone shares it.
3. **Reseed on bootstrap:** the guest mixes the 32-byte seed into the kernel pool with entropy
   credit (`RNDADDENTROPY`) and re-keys the CRNG at once (`RNDRESEEDCRNG`), so `/dev/urandom` and
   `getrandom()` diverge from the siblings before anything reads them; it reports `reseeded: true`.
   VMGenID does the same on hosts whose guest kernel has the driver (≥ 5.18); the seed does not
   depend on it (boss-1's guest kernel is 5.10). The guest also sets its clock from
   `host_time_unix_nano` when off by more than a second, and a replica re-MACs `eth0` from the
   bootstrap (the snapshot carries the template's MAC).
4. **No MMDS** (decision 27). The bootstrap over vsock already carries every non-secret
   identity field, nothing in the guest consumes MMDS, and the in-guest `meta skuid != 0 drop`
   rule (LLD §3.18) does not exist yet. A guest's attempt to reach `169.254.169.254` therefore
   leaves on the tap and is dropped and counted there (`meta_`), as in P8a. MMDS returns with a
   consumer and that rule, V2 and non-secret only.
5. **Files:** `mem`, `state`, `overlay.ext4` and `meta.json` are 0600, owned by boss-hostd (root).
   A restoring VM's uid is granted **read** by a POSIX ACL entry (`user:<uid>:r--`) for as long as
   it runs and revoked at stop; no group or other access ever. Clones hard-link the same inode into
   their jails and Firecracker maps it `MAP_PRIVATE`, so clean pages are shared copy-on-write. On a
   filesystem without ACLs or across filesystems the host falls back to a private sparse copy.
6. **GC:** a sweep every minute deletes snapshots whose `image_digest` no desired VM or pool
   references, once older than a 10 min grace (both fixed in `hostd.Config`: `SnapshotGCEvery`,
   `SnapshotGCGrace`; no environment variable) — event `snapshot_gc`.

**Follow-up (S15 rule 2):** the "TLS key and credential clients only after the bootstrap" rule
for `boss-runtime` is documented here and has no test yet; add one (50 clones, 50 distinct TLS
keys) once `boss-runtime` is in the image.

**Restore.** Jail prepared (kernel and base hard-linked, overlay copied sparse from the snapshot,
`mem`/`state` linked or copied), Firecracker started with no config, `PUT /snapshot/load`
(`mem_backend: File`, `resume_vm: true`), then the bootstrap over vsock. The device state is the
snapshot's, tap name included, and Firecracker 1.10 has no `network_overrides` (1.12+): so **a
replica is snapshotted only under the jailer**, where every replica's guest tap is `tap0` in its own
namespace; an unjailed replica is refused (`ErrReplicaNeedsJail`) and cold boots every time
(decision 28). `per_run` snapshots have no NIC and restore anywhere. A restored clone keeps
its snapshot's vsock CID (Firecracker fixes it at load; the host identifies VMs by socket, never by
CID, and the bootstrap's `cid` is informational).

**Warm pools.** A warm VM is a restored (or the first, cold-booted) VM of a shape with **no
identity**: it has answered pings, never a bootstrap. It lives under a host-local slot id
(`w-<8 hex>`); its directory, jail and (for a replica spare) host link `bt<12 hex of sha256(slot)>`
keep that name after the claim, and `boss-spec.json` then carries the control-plane `vm_id`.

- `per_run`: `min_warm` per pool from desired state (default 2), filled only while claims are not
  frozen; refilled when depth drops; dropped when the pool disappears.
- `replica`: `BOSS_HOSTD_REPLICA_SURGE` (default 1) spare per shape of the replicas this host runs,
  so a replacement or an image roll is a claim away. (Only a link-owning backend can hold a spare
  before it has a `vm_ip`: the jailer path; the P8a plain-tap path has no spares.)
- Until a shape has a snapshot only one fill runs at a time (the cold boot that makes it); fills
  then run concurrently up to `BOSS_HOSTD_MAX_VMS` (warm VMs count).
- A **claim** is a desired VM of the same deployment and shape: the oldest ready warm VM gets the
  identity (bootstrap), the replica policy on its link, and the gateway listeners. States:
  `provisioning → restoring → ready → serving`. Without a spare the host restores from the
  snapshot (or cold boots) at placement.

**Measured on boss-1** (8 vCPU, 16 GiB, KVM, kernel 6.8, Firecracker v1.10.1 + jailer, guest
kernel 5.10, 128 MiB VMs; `internal/e2e/p8b_linux_test.go`, 50 clones claimed in waves of 10 from
a pool of 10):

| Measure | n | p50 | p95 | Target (LLD §4.2 T7) |
|---|---|---|---|---|
| Warm claim (desired VM → bootstrapped from the pool) | 50 | 89 ms | **124 ms** | < 300 ms |
| Snapshot restore (`PUT /snapshot/load` → vsock up) | 61 | 94 ms | **127 ms** | — (LLD estimate 10–40 ms) |
| Cold boot (jail + Firecracker → control answers) | 2 | — | **709 ms** | < 3 s |
| Snapshot (pause, write 128 MiB + state + overlay, resume) | 2 | 370 ms | — | — |
| Pool of 10 filled (1 cold boot + snapshot, 9 restores) | 1 | 4.0 s | — | — |

P8b-T4 on the same run: 50 clones, 50 distinct RNG outputs and `vm_id`s, 49 restored (the
first was the cold-booted template), 50 reseeded, clocks within 5 s; no run token, private key
or join token in any jail, console log or snapshot file. Density sample: 63 jailed 128 MiB VMs
used 1313 MiB of host RSS, 20.8 MiB per VM (memory file shared `MAP_PRIVATE`). Stopping a VM left
no jail, cgroup, namespace or ACL grant behind.

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
| 7101 | model | `POST /anthropic/v1/messages`, `/openai/v1/chat/completions`, `/openai/v1/responses` | `endpoints.model_gateway` — the central streaming model gateway (§4.6) |
| 7102 | git / api | `GET /_boss/git-credential` answered by the host (§4.9); `GET,POST /git/<host>/…`; `GET,POST,PATCH,PUT /api/<host>/…` | `endpoints.egress` — boss-egress's host route (§4.9) |
| 7103 | mcp | `POST /mcp/<server>` (GET and DELETE refused: no server-initiated stream, no session) | `endpoints.mcp_gateway` — the control plane's own `/mcp` route (§4.7) |
| 7104 | run_plane | `/v1/run-plane/next`, `/v1/run-plane/hosts/…` **denied** (VMs never poll for work) and `/v1/run-plane/runs/<run>/tokens` **denied** (only the host mints, §4.8); any other `/v1/run-plane/…` | `endpoints.run_plane` (the control plane) |
| 7105 | connect | denied until P8b | — |
| 7105 (guest side) | **work inbox** (host-initiated, 1.1) | the host connects to the **guest's** port 7105; §4.5 | — |
| 5000 | control (host-initiated) | `bootstrap`, `ping`, `ifaces`, `selfcheck_egress`, `selfcheck_gateway` | — |

### 4.2 Per request

1. **Identify** the VM by its socket.
2. **Match** the request against the route table. Prefixes match on a segment boundary
   (`/anthropic/v1/messages` never matches `/anthropic/v1/messagesX`). A path with dot segments,
   empty segments, or an encoded `/`, `\`, `.` or NUL is refused `400 path_not_canonical`.
3. **Resolve** the handle from `Authorization`, `X-Api-Key` or `Proxy-Authorization`.
   - The handle maps to `(run, epoch, vm)`.
   - A handle from another VM's socket is refused (`handle_replay`), **revoked**, and reported.
   - A fenced epoch gets `409 fenced`.
   - On the run plane, a **replica needs no handle**: its socket is its identity.
4. **Strip** every guest credential and identity header:
   - `Authorization`, `Proxy-Authorization`, `X-Api-Key`, `Api-Key`, `Cookie`, `X-Auth-Token`,
     `Private-Token`, `X-Amz-Security-Token`;
   - `Forwarded`, `X-Forwarded-*`, `X-Real-Ip`;
   - every `X-Boss-*` and `Boss-*`;
   - `DPoP` (a guest-made proof never stands in for the host's, §4.8);
   - every request **trailer** (they could smuggle a header past the strip).
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

### 4.5 The work inbox (1.1; LLD §3.13.0)

`boss-runtime` in a VM has **no pull loop**. It listens on **vsock port 7105 of the guest** (the
host connects to the guest's CID; guest-initiated port 7105 on the host side is the separate
`connect` route). On the `process` backend it is a Unix socket per VM, `<dir>/w_7105.sock`, mode
0600. One JSON object per line, one answer line per request; a connection may carry many:

| Host → guest | Guest → host |
|---|---|
| `{"op":"run","run_id":"arun_…","job_id":"job_…","replica_id":"rep_…","epoch":3}` | `{"ok":true}` — accepted, the run is driving |
| | `{"ok":false,"reason":"no_free_slot"\|"draining"\|"duplicate"\|"bad_request"}` — refused: hand it back (§2.8) |
| `{"op":"ping"}` or `{"op":"slots"}` | `{"ok":true,"busy_slots":1,"total_slots":4}` |

- The guest accepts a run **only while it has a free slot**. It never sees a run token.
- An accepted run fetches its package through the gateway, `GET /v1/run-plane/runs/{run_id}` (run,
  messages, pinned agent version, resume answers; never the token), then drives the kernel; all of
  its calls go back through port 7104 as before.
- **Run-plane gateway rule (1.1, replaces the pass-through in §4.2 step 4):** for any
  `/v1/run-plane/runs/{run_id}/…` call, the host sets `X-Boss-Replica` from the socket and
  `X-Boss-Run-Token` from its run map (overwriting whatever the guest sent); `X-Boss-Job` and
  `X-Boss-Fence-Epoch` pass through and are checked against Postgres. A run the host holds no
  token for is answered `409 fenced` without a forward. On `POST /v1/run-plane/admit` responses,
  the host removes `lease.run_token`, keeps it in its run map, and forwards the rest.

### 4.6 The model route: what boss-hostd sends to the model gateway (P8b S10)

The central model gateway (`apps/model-gateway`, `boss_engine.services.streaming_model_gateway`)
is the only process that holds a provider key. The host forwards a guest's model call to
`endpoints.model_gateway` (an absolute URL the API takes from `BOSS_MODEL_GATEWAY_URL`; absent →
`503 gateway_unconfigured`) and the answer streams back chunk by chunk. **The model rules carry
`HostProof: true`, so the Go gateway sends, on every model forward:**

| What | How | Why |
|---|---|---|
| The HOST's identity | `Authorization: BossHost <proof>` (§1.3) over the **gateway's** method and path (`htu` = the forwarded path, e.g. `/anthropic/v1/messages`), while the connection still presents the VM's SVID | the gateway answers only enrolled hosts (`fleet_ca.verify_proof`: pinned certificate, 60 s, jti spent once) |
| The guest's handle | `X-Boss-Handle: boss_mdl_…` — the value the guest presented in `x-api-key` / `Authorization: Bearer` / `Proxy-Authorization`, **moved** here after the strip | the gateway binds it to `(run, epoch, vm)` in Postgres; it never goes upstream |
| The host's word on the VM, run and epoch | `X-Boss-Vm`, `X-Boss-Run`, `X-Boss-Fence-Epoch`, `X-Boss-Deployment` from the socket's binding | a handle minted before its VM existed (a code task) is **bound to the first VM that redeems it**; from then on a handle whose row names another VM or run is a **replay**: revoked, `host_events(handle_replay)`, `401 handle_replay`; an epoch that is not the handle's, or not the run's current one in Postgres, is `409 fenced` within that one request |
| The run assertion | `Boss-Run-Assertion` (§4.2 step 5), unchanged | its `run`, `vm` and `epoch` must agree with the headers above (`409 assertion_mismatch`). Signature verification against the SVID chain follows when the host forwards the SVID leaf (S13) |
| The guest's path and query | `/<provider>/<path>` exactly as the guest sent it, under the gateway's base URL; no double prefix | the gateway pins each provider's inference paths (`403 not_a_model_path`) and reads the model from the body or path (`403 model_not_allowed`) |
| A trace id | `traceparent`, when the guest sent one | the `guest_calls` row the gateway writes carries it |

Everything else the guest sent is stripped as in §4.2 step 4 (every credential, cookie,
forwarding and `X-Boss-*` header, trailers). Providers: `anthropic`, `openai`, `openrouter`,
`azure_openai`, `bedrock` (SigV4-signed by the gateway).

**Answers the host relays unchanged** (every non-2xx is `{"type":"error","error":{"type","code","message"}}`,
Anthropic-shaped so a CLI prints it): `401 host_proof_required | no_handle | unknown_handle |
handle_revoked | handle_expired | handle_replay | foreign_provider`, `402 spend_refused`,
`403 not_a_model_path | model_not_allowed`, `404 unknown_provider`, `409 fenced | assertion_mismatch`,
`413 body_too_large`, `429 spend_deferred`, `502 upstream_unreachable`,
`503 key_unavailable | upstream_unconfigured`. 2xx bodies stream: the host flushes every chunk
(`FlushInterval: -1`) and never buffers.

**What the guest does** (the in-guest agent, contract v1.1): `ANTHROPIC_BASE_URL` /
`OPENAI_BASE_URL` = `endpoints.model` + `/<provider>`; `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` =
`handles.mdl`. Nothing else: no key, no token, no login file.

**Follow-ups (P8b):** the gateway listens on plain HTTP inside the compose network today; the mTLS listener (host client certificate verified against the fleet CA, SVID chain check of `Boss-Run-Assertion`, S13) lands with the central-gateway TLS work. Until then the BossHost proof is the host's authentication.

**Per call, the gateway** reserves the run's budget (`BudgetGovernor` via `model_routing.governed`,
one reservation per call, settled with the usage read from the provider's own stream), reads the
key through `resolve_secret_ref` as `sys_model_gateway` (re-read when the reference rotates),
writes one `llm_calls` row and one `guest_calls` row (`route = model`, `rejected_by =
model_gateway:<reason>` on a refusal, `client_disconnected` on an abandoned stream; batched, never
on the request path). A stream the guest abandons is still settled with the usage read so far —
only an upstream error, or a stream that never produced a byte, releases the reservation. Bedrock
streams (`converse-stream`, `invoke-with-response-stream`) are accounted from their event-stream
`metadata` / `chunk` frames. The handle's model allowlist is the run's routed models (Setup › Repo
and models, every job); a project on Auto names none, so any model of the provider is allowed. In strict mode the
model hosts leave boss-egress's CONNECT allowlist (`BOSS_EGRESS_STRICT_SECRETS=1`), so the
injecting route is the only path to a model.

### 4.7 The MCP route: what boss-hostd sends to the MCP/tool gateway (P8b S12, BOSSAPP-1073)

The MCP/tool gateway is the control plane itself: `POST /mcp/<server>` on the API
(`boss_engine.services.mcp_gateway`, host lane), which is why `endpoints.mcp_gateway` is the
relative path `/mcp`. It needs the policy engine, the decisions queue (a tool set to Ask parks the
call until a person answers in Approvals) and the registry's receipts, and all of those live
there. The guest's MCP clients point at `127.0.0.1:7103/mcp/<server>` (contract v1.1
`mcp.servers`) with `handles.mcpgw` as their bearer; **the MCP rule carries `HostProof: true` and
`Audience: boss:mcp`**, so on every MCP forward the Go gateway sends exactly what it sends on a
model forward (§4.6): `Authorization: BossHost <proof>` over the gateway path (`/mcp/<server>`),
the handle **moved** into `X-Boss-Handle`, the host's word in `X-Boss-Vm`, `X-Boss-Run`,
`X-Boss-Fence-Epoch`, `X-Boss-Deployment`, the `Boss-Run-Assertion`, and `traceparent`. Every
guest credential, cookie, forwarding, `X-Boss-*` and **`DPoP`** header is stripped first: a
guest-made proof never stands in for the host's.

**What the control plane does with it:**

| Check | Answer |
|---|---|
| The handle's row (`mcp_gateway_handles`, hashed) | `401 no_handle \| unknown_handle \| handle_revoked \| handle_expired` |
| The host's word itself | `X-Boss-Vm` or `X-Boss-Run` missing → `400 binding_headers_required` |
| Its binding against the host's word | another org, VM or run → **replay**: the row is revoked, a `host_events(handle_replay, actor=mcp_gateway)` row is written, `401 handle_replay`; a code task's unbound handle is bound to the first VM that redeems it |
| Its epoch | not the handle's, or not the run's current one in Postgres → `409 fenced` within that one request |
| `<server>` against the row's `servers` (the agent's MCP grant, agents.held_mcp_servers) | not on it → `403`, a typed `boss.denied_by_policy` result; `boss_run` (the run's own tools) is always on |
| The tool, per call | `tool_policy_verdict`: the grant's tool list, the registry's Allow/Ask/Off, the ordered policy engine — exactly the loopback gateway's decision; an Ask parks the call (SSE keepalives when the client sent `Accept: text/event-stream`) |
| S13 (§4.8) | `X-Boss-Delegation` + `DPoP`, when present, must verify for **aud `boss:mcp`** and name this run: `401 run_token_<reason>` (`run_token_bad_aud` for a model-gateway, egress or run-plane token; `run_token_dpop_key_mismatch` for a proof from another key; `run_token_mismatch` for another run); with `BOSS_HOST_TOKENS_REQUIRED=1`, absent → `401 run_token_required` |

Refusals are JSON-RPC errors (`{"jsonrpc":"2.0","id":null,"error":{"code":-32001,"message":"boss
mcp gateway: <reason>","data":{"reason":"<reason>"}}}`), relayed unchanged. `GET` and `DELETE` are
`405` (the host refuses them without a forward).

**No passthrough.** Nothing the guest sent reaches an upstream MCP server: the upstream sign-in is
read at call time through `connection_credentials` (shared / per-user / per-agent OAuth, obtained
with RFC 8707 `resource` = that server, refreshed when due) and goes on the upstream call only;
`mcp_client` applies `tokens.refuse_passthrough` — the first-party-audience guard — to every
upstream header set, so a BOSS session, key, run token, DPoP proof or egress credential is never
sent to a third-party server. A tool failure's text passes the run redactor before the guest sees
it. **STDIO servers** that need secrets never run in the guest: `stdio://` servers run under
`mcp_stdio_supervisor` beside the gateway; the guest sees only `/mcp/<server>`.

**Token exchange upstream.** Where an upstream supports exchanging the person's token for a
tool-scoped one (Entra, Google, AgentCore 3LO), that exchange belongs here, never in the guest.
None of the built-in upstreams is exchanged yet: the per-user OAuth token minted for `resource` =
the server is the tool-scoped token today.

**Audit.** One `guest_calls` row per call: `route = mcp`, `rule_id = mcp-<server>`,
`path_template = /mcp/<server>/<jsonrpc method>`, `rejected_by = mcp_gateway:<reason>` on a
refusal — including a call the policy refused inside a 200 JSON-RPC answer (`verdict = deny`,
`rejected_by = mcp_gateway:denied_by_policy`) — the handle's fingerprint, never the handle, a body
or an argument; batched off the
request path (the model gateway's writer).

**Handles.** Minted with the run's lease (`run_plane._guest_handles`): `handles.mcpgw` bound to
`(run, epoch, the replica's VM)` with the deployment agent's MCP grant as `servers`, plus
`handles.mcp_servers` (`{name: http://127.0.0.1:7103/mcp/<name>}`, `boss_run` included). Revoked
with the lease (settle, fail), and on a replay.

### 4.8 Host-held, per-destination run tokens (P8b S13, BOSSAPP-1074)

**The shim is boss-hostd.** A DPoP key inside a VM that runs model-written code protects nothing,
so the host generates an **Ed25519 key pair per run, in memory, never written**, and asks the
control plane for the run's tokens bound to its thumbprint:

```
POST /v1/run-plane/runs/{run_id}/tokens        (host-authenticated; the lease headers of §4.5)
{"jkt": "<RFC 7638 thumbprint>", "audiences": ["boss:model","boss:mcp","boss:egress","boss:run_plane"]}

200 {"run_id", "jkt", "expires_in": 300, "rotate_after_seconds": 200,
     "tokens": {"boss:model": {"access_token", "token_type": "DPoP", "expires_in", "jti"}, …}}
```

- Fenced like every run call (`409 fenced` on a stale lease). `boss:api` is never issued to a
  host (`400 bad_audience`). The control plane mints through `identity.runs.start_run(...,
  dpop_jkt=…)`: the run's subject is the person the job was enqueued for (OBO, their grant) or
  the deployment's agent itself (autonomous), the actor is the agent's Passport
  (`roles.agent_id` for the deployment's agent key; `409 no_agent_passport` without one), the
  scope `agent:invoke` narrowed live. BOSS keeps a host-held Shim with **no private key** for it.
- **One token per destination (RFC 8707).** Every forward carries the token for THAT audience
  in `X-Boss-Delegation` and a fresh **`DPoP`** proof (RFC 9449: `htm`, `htu` = the forwarded
  URL, `ath`, `jti`, `iat`) signed by the run's key. The model gateway verifies `boss:model`, the
  MCP gateway `boss:mcp`, the run plane `boss:run_plane`, egress `boss:egress`; each refuses the
  others (`bad_aud`), a proof from another key (`dpop_key_mismatch`), a replayed proof
  (`dpop_replay`) and a token for another run. Rule order in `routes.go` names the audience per
  route (`Rule.Audience`).
- **Rotation at two thirds of the TTL.** The host generates a NEW key and calls the endpoint
  again; the previous tokens are not revoked (the run's end revokes them all), so a forward that
  already captured its `(token, key)` pair completes, and a new token never pairs with the old
  key. The guest's handles never change, so a rotation is invisible to the agent.
- **Required per host (decision, PR #121 review).** A host that holds per-run keys advertises
  `capabilities: ["host_tokens"]` at enrol and on every heartbeat; the control plane records it
  in `agent_hosts.host_tokens`, **sticky** (a host cannot un-advertise it). From then on every
  forward from that host must carry its token: `401 run_token_required` otherwise — at the model
  gateway, the MCP gateway and the run plane. A host that predates S13 (no capability) may still
  forward on its host proof alone. `BOSS_HOST_TOKENS_REQUIRED=1` only forces the requirement on
  (tests, a strict install); it never turns it off. The local lane is unchanged.
- **Where each token is verified** (one check, `identity/host_delegation.py`): the model gateway
  (`boss:model`), the MCP gateway (`boss:mcp`), the run plane (`boss:run_plane`, on every
  `/v1/run-plane/runs/{run}/…` call a VM's gateway forwarded — recognised by its
  `Boss-Run-Assertion`; the host's own calls are authenticated by its proof), and egress
  (`boss:egress`), each **required per host** by the same rule (§4.9 closes the egress
  follow-up: the git/REST route carries `HostProof`, egress learns the host from the proof and
  calls `fleet.host_tokens_required(connection, host_id)`).
- **Egress follow-up (closed by P8b-5, BOSSAPP-1072).** The git/REST route has `HostProof: true`;
  egress verifies the proof (or a terminator's forwarded certificate) to learn the host, and a
  host with `host_tokens` cannot reach egress without its `boss:egress` token
  (`401 run_token_required`).
- **Fail closed.** If the control plane cannot read whether a host requires tokens (a database
  error), it treats the token as required; the answer is not cached.
- **Minted before the VM hears of the run.** `boss-hostd` mints a claimed run's tokens before
  writing the work line; a refused mint hands the run back with reason `tokens_unavailable:
  <code>`. A run a replica admits itself (`/v1/run-plane/admit`) is minted in the gateway before
  the answer reaches the guest; a refused mint answers the guest `503 tokens_unavailable`. A
  control plane without the endpoint (`404`) leaves the run on the host proof alone.
- **A guest can never mint.** The route table denies `/v1/run-plane/runs/<run>/tokens` (and
  anything below it, any case) before the run-plane rule (`run-plane-tokens-denied`), and the
  control plane refuses a forwarded one (`403 host_only`). The requested scope is narrowed to
  `agent:invoke` (`400 bad_scope` if nothing is left).
- At the run's end (`settle`, `fail`, hand back) the host forgets the key and tokens
  (`UnbindRun`), and the control plane revokes the run's tokens with its handles.

### 4.9 The git and REST route: what boss-hostd sends to boss-egress (P8b S11, BOSSAPP-1072)

**In the guest** (contract v1.1; the in-guest agent's `configure_git_relay`), git is pointed at the
relay and nothing else changes:

```
url.http://127.0.0.1:7102/git/github.com/.insteadOf    https://github.com/
url.http://127.0.0.1:7102/git/bitbucket.org/.insteadOf https://bitbucket.org/
credential.helper = ""                                 (reset)
credential.helper = !f() { … GET http://127.0.0.1:7102/_boss/git-credential[?run=<run>] … }; f
```

No token and no handle is written to any git config or environment variable. The helper asks
**boss-hostd**, which answers `username=boss\npassword=<this VM's boss_egr_ handle>` from the
socket's identity (never another VM's; a VM holding several runs' handles must name the run; a
fenced run's handle is never handed out). git sends it only after a challenge, so a git/api call
with no handle is answered `401` with `WWW-Authenticate: Basic` and never forwarded. Hosts with no
insteadOf (GitLab, any other) fail closed in the guest: it has no network.

**On the forward** the git and REST rules carry `HostProof: true` and `Audience: boss:egress`, so
boss-hostd sends exactly what it sends to the model and MCP gateways (§4.6, §4.7): `Authorization:
BossHost <proof>` over the egress path, the handle **moved** into `X-Boss-Handle` (from the guest's
HTTP Basic password or bearer), `X-Boss-Vm`, `X-Boss-Run`, `X-Boss-Fence-Epoch`,
`X-Boss-Deployment`, the `Boss-Run-Assertion`, and the run's `boss:egress` token in
`X-Boss-Delegation` with a fresh `DPoP` proof over the forwarded URL. The guest path is kept:
`/git/<host>/<owner>/<repo>.git/…` and `/api/<host>/…` under `endpoints.egress`
(`BOSS_EGRESS_HOST_URL`, else `BOSS_EGRESS_URL`; absent → `503 gateway_unconfigured`).

**What egress does** (`apps/egress/boss_egress/proxy.py`, `_host_route`):

| Check | Answer |
|---|---|
| The host: its `BossHost` proof (`fleet_ca.verify_proof`), or a client certificate a named terminator (`BOSS_HOST_CERT_TERMINATORS`) forwarded | `401 host_proof_required` or fleet_ca's own code |
| The handle (`identity_egress_credentials`, hashed, `sandbox_id = 'host:'`) | `401 no_handle \| unknown_handle \| handle_revoked \| handle_expired`; `X-Boss-Vm`/`X-Boss-Run` missing → `400 binding_headers_required` |
| Its binding against the host's word | another org, VM or run → **replay**: revoked, `host_events(handle_replay, actor=egress)`, `401 handle_replay`; an unbound handle binds to the first VM that redeems it |
| Its epoch | not the handle's, or not the run's current one → `409 fenced` |
| The run's `boss:egress` token (`identity/host_delegation.py`) | **required per host** (`host_tokens`): `401 run_token_required`; another destination's token `run_token_bad_aud`; another key `run_token_dpop_key_mismatch`; another run `run_token_mismatch`. A host that predates S13 and attaches none is served only if the run's egress token was registered server to server, else `401 egress_not_registered` |
| The git host (D19) | `github.com`, `bitbucket.org` (git); `github.com`/`api.github.com`, `bitbucket.org`/`api.bitbucket.org` (REST). Any other → `403 git_host_not_routed` (`strict_secrets_git_host_refused` with `BOSS_EGRESS_STRICT_SECRETS=1`) |
| The rule table, on the token's task binding | GitHub: `egress_rules.check_git` / `check_api` on the token's `github` repos and permissions; Bitbucket: `check_git` / `check_bitbucket_api` on its `bitbucket` repos. `repo_outside_run`, `insufficient_permission`, … |
| Pushes | only `refs/heads/boss/<run>-*` (`ref_not_allowed`); never a force-push (`force_push`); **never a branch delete on this route** (`branch_delete`), not even the run's own |

Then every inbound credential is stripped and the real one injected: a per-run, down-scoped GitHub
installation token (minted on first use for the token's repos only, held in egress's memory), or
the install's Bitbucket credential (`BOSS_BITBUCKET_TOKEN` as `x-token-auth`, else
`BOSS_BITBUCKET_USERNAME` + `BOSS_BITBUCKET_APP_PASSWORD`). Bitbucket cannot mint per-run tokens,
so its down-scoping is the rule table; its pushes are judged from the pack alone (a non-fast-forward
whose old tip is not in the pushed history is a force-push). One `identity_egress_log` row per call,
never a body, query or token.

**The run's git scope** is the token's, not the handle's: `POST /v1/run-plane/runs/{run}/tokens`
mints the `boss:egress` token with `repos`, `bitbucket_repos` and `github_needs` from the job's
payload, else the deployment's `metadata` (`run_plane.run_git_scope`). Nothing named → nothing
reachable. The handle is minted with the run's lease (`run_plane._guest_handles`, `handles.egr`,
bound to the replica's VM) and revoked with it.

**The local lane is unchanged**: `runs.egress_access` (the per-run shim socket, the network-bound
credential, `/github/…` and `/github-api/…`). A VM handle presented there is refused
(`egress_host_route_only`).

## 5. Networking (LLD §3.7)

### 5.1 Replica VMs

**The tap:**

- One tap per VM, `bt<12 hex of sha256(vm_id)>`.
- Persistent and owned by the hostd uid.
- MAC `02:fc:<vm_ip bytes>`.
- Route `vm_ip/32 dev <tap>`.
- `proxy_arp=1`, `rp_filter=0`, IPv6 off on the tap.
- The guest configures `vm_ip/32` and `default via 169.254.1.1 onlink` from the bootstrap.

- The guest sets `arp_ignore=1` and `arp_announce=2` (the VIP lives on the VM in P8b; ARP is
  always sourced from `vm_ip`, so proxy-ARP answers for the gateway).

**Directors encapsulate in GRE, not IPIP** (`ipvsadm --tun-type gre`, P8b). nftables can match
GRE's inner headers with types; it cannot type a raw-payload concatenation of an IPIP inner
header, and the host's conntrack never sees the inner flow of either. GRE therefore gives the host
the exact 4-tuple of every caller flow.

**nftables (table `inet boss_hostd`, default drop, every drop counted):**

- **Unknown taps:** any `bt*` interface without its own chain is dropped (`drop_unknown_tap`).
- **`internal_nets`** (interval set): `127.0.0.0/8`, `169.254.0.0/16`, the host's VM prefix, every
  address on the host, and the operator's infra ranges (`netadm --internal-nets`). Private ranges
  are not internal by default: callers inside a private network are supported.
- **IN** (`in_<tap>`): GRE from `@directors` to `vm_ip` carrying TCP to `VIP:vip_port` is
  accepted (`in_`). A caller's **SYN** learns the flow into `flows_<tap>` as
  `(VIP, caller, vip_port, caller_port)` (10 min); later packets refresh only a live flow.
  Everything else is dropped (`indrop_`).
- **OUT** (`out_<tap>`):
  - `→ 169.254.169.254` is dropped (`meta_`);
  - **the directors' health-check reply** (P8b-10, §9.5): from the VIP, sport ∈ `vip_ports`, to
    `@directors`, non-SYN, on a flow that director opened (`flows_`) is accepted (`reply_`) — the
    one reply shape allowed toward an internal address, because directors sit on the infra network;
  - `→ @internal_nets` is dropped (`orig_`), before every other accept;
  - from the VIP, sport ∈ `vip_ports`, on a learned flow: **RST** deletes the flow and is
    accepted; **FIN** moves the flow to `closing_<tap>` (30 s) and is accepted; any other non-SYN
    packet on a flow in `flows_` or `closing_` is accepted (`reply_`);
  - everything else is dropped (`orig_`): DNS, VM↔VM, internet, host addresses, a forged packet to
    a peer that never called.

**Proof** (`scripts/sandbox/hostd-netlab.sh`, run as root on boss-1, kernel 6.8, nft 1.0.9, with
the exact rules `boss-hostd netpol-render` prints): a GRE-tunnelled caller gets HTTP 200 with the
reply returned direct from the VIP (`in=6 reply=6 orig=0`); the flow is learned on SYN and moved
to `closing_` (30 s) by the server's FIN; 3 forged ACKs `VIP:443 → unseen peer` are dropped and
counted; VM → infra range is dropped; GRE from a non-director is dropped.

**The P8a-T4 evidence** is `orig_` + `meta_` = 0. It is reported per replica as
`drops_originated`.

**Under the jailer** every VM has its own network namespace `bv-<id>`. A replica's holds the
guest tap (`tap0`, owned by the VM uid; the same name in every namespace, so one snapshot's network
device restores anywhere) bridged (`br0`) to the inner end of a veth pair whose **host end carries
the policy name** `bt<12 hex>`: the route, proxy-ARP, sysctls and nftables chains above apply to it
unchanged. The VM manager builds the link at Create (the policy is applied right after), and the
namespace dies with the VM. A `per_run` VM's namespace is empty.

### 5.2 `per_run` VMs

- No `network-interfaces` entry at all: only `lo` and vsock (and, jailed, an empty netns).
- Off by default behind `BOSS_MICROVM_CLAIMS_FROZEN`.

### 5.3 The privileged helper

- `boss-hostd netadm --socket S --allow-uid U --vm-prefix CIDR [--tap-owner-uid N]
  [--internal-nets CIDR,…]` holds `CAP_NET_ADMIN`. Under systemd it runs with
  `AmbientCapabilities=CAP_NET_ADMIN`, its flags in a root-owned unit.
- **Its policy is its own.** The VM prefix, the tap owner uid (default `--allow-uid`) and the
  internal nets come only from its flags. A request whose `vm_prefix` differs from the helper's,
  whose `vm_ip` is outside it, or whose tap owner differs, is refused. A helper without
  `--vm-prefix` refuses everything.
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
- **`/health`:** the legacy fields keep their legacy meaning (`capacity`/`available` of the coding
  lane). The VM manager's capacity is reported only under `fleet`
  (`{host_id, vms, capacity, available, revoked}`).
- The LLD's `claim`, `forward`, `cancel`, `drain`, `logs` and `fs` VM calls are P8b.

---

## 7. Configuration

| Variable | Meaning |
|---|---|
| `BOSS_HOSTD_CP_URL` | Control plane base URL. It enables the fleet agent. |
| `BOSS_HOSTD_CP_CA` | PEM bundle pinned for the control plane's TLS server certificate. |
| `BOSS_HOSTD_JOIN_TOKEN_FILE` | The one-time join token, read on first start only, and **deleted** after a successful enrolment. |
| `BOSS_HOSTD_HEADLESS_MAX_SECONDS` | Default 86400. Control plane unreachable this long → stop serving (§3.2). |
| `BOSS_HOSTD_STATE_DIR` | `pki/` (0600 keys, pinned fleet CA) and `cache.db`. Default `/var/lib/boss-hostd`. |
| `BOSS_HOSTD_RUN_DIR` | Per-VM directories. Keep the path short, because of the Unix socket limit. Default `/run/boss-hostd/vms`. |
| `BOSS_HOSTD_VM_BACKEND` | `process` (laptop/CI) or `firecracker`. |
| `BOSS_HOSTD_GUEST_BIN` | `boss-vsockd`, for the process backend. |
| `BOSS_HOSTD_FC_ROOTFS` | Read-only base rootfs with `/sbin/boss-vsockd`. See `scripts/sandbox/hostd-guest-bake.sh`. |
| `BOSS_SANDBOX_FC_BIN`, `BOSS_SANDBOX_FC_KERNEL` | Firecracker binary and `vmlinux`, shared with the legacy lane. |
| `BOSS_HOSTD_NETADM_SOCKET` | The helper's socket, or `inprocess` when hostd has `CAP_NET_ADMIN` itself. |
| `BOSS_HOSTD_MAX_VMS`, `BOSS_HOSTD_POOL`, `BOSS_HOSTD_STRICT_SECRETS` | Capacity cap (20), pool label, strict-secrets fact (true). |
| `BOSS_MICROVM_CLAIMS_FROZEN` | Default `true`. Gates `per_run` VMs, their warm pools and the legacy lane. Replicas are not gated. |
| `BOSS_HOSTD_JAILER_BIN` | Firecracker's `jailer`. Set → every VM is jailed (§8); boss-hostd must then run as root. Unset → P8a behaviour, `jailer_on: false`. |
| `BOSS_HOSTD_JAILER_CHROOT` | Jail base (default `/srv/boss-jail`; keep it short: the vsock sockets live inside). |
| `BOSS_HOSTD_JAILER_UID_BASE` | First uid/gid of the per-VM pool (default 100000; range 10000). |
| `BOSS_HOSTD_SNAP_DIR` | Snapshot store (default `<state dir>/snapshots`), §3.4. |
| `BOSS_HOSTD_OVERLAY_MIB` | Per-VM writable overlay drive (default 1024; `0` = none, writable paths on tmpfs as in P8a). Needs `mke2fs`. |
| `BOSS_HOSTD_RUNTIME_VERSION` | Baked boss-runtime version; part of the snapshot key. |
| `BOSS_HOSTD_REPLICA_SURGE` | Warm spares per replica shape (default 1; `0` disables). |

---

## 8. The jailer (LLD §3.18, S14)

### 8.1 How a VM is launched

```
jailer --id <fcid> --exec-file <firecracker> --uid U --gid U --chroot-base-dir <base>
       --cgroup-version 2 --parent-cgroup boss-hostd
       --cgroup cpu.max="<vcpu×100000> 100000" --cgroup memory.max=<(mem_mib+32) MiB>
       --resource-limit fsize=4294967296 --resource-limit no-file=4096
       --new-pid-ns --netns /run/netns/bv-<fcid> -- [--config-file /vm.json]
```

- `<fcid>` is the vm id with `_` → `-`. The jail is `<base>/firecracker/<fcid>/root`, mode 0700,
  owned by `U`; boss-hostd's own records (`boss-spec.json`, `console.log`) live one level up,
  root-owned, out of the VM's reach.
- **`U`** is unique per running VM: derived from the id inside
  `[BOSS_HOSTD_JAILER_UID_BASE, +10000)` and moved past any uid another jail already owns.
- Inside the jail: `vmlinux` and `rootfs.ext4` are **hard links** to the host files (read-only;
  granted by ACL if not world-readable), `overlay.ext4` a private sparse copy, `vm.json` the
  config, `restore/{mem,state}` the snapshot links, `run/firecracker.socket` the API socket, and
  the vsock sockets. The jailer copies the Firecracker binary in and mknods `/dev/kvm`,
  `/dev/net/tun`.
- The jailer passes `--id` and the API socket to Firecracker itself; boss-hostd never passes
  `--no-api`, `--no-seccomp` or `--seccomp-filter`: **default seccomp**, API-socket mode.
- With `--new-pid-ns` the jailer forks Firecracker into the new namespace, writes
  `<jail>/firecracker.pid` and exits. boss-hostd waits for that pid file, watches the pid, and
  drives the API through `<jail>/run/firecracker.socket` (root only).
- `cpu.max`/`memory.max` land in `/sys/fs/cgroup/boss-hostd/<fcid>/`; the directory is removed at
  stop, with the jail and the namespace.
- Guest-initiated vsock listeners (`<jail>/v.sock_<port>`) are chowned to `U`, the only thing
  allowed to connect.

### 8.2 What boss-hostd needs

Root (the jailer needs `CAP_SYS_ADMIN`, `CAP_SETUID`/`SETGID`, `CAP_SYS_CHROOT`, `CAP_MKNOD`, and
the netns/veth work `CAP_NET_ADMIN`), cgroup v2 with `cpu` and `memory`, `/run/netns`, `mke2fs`
for the overlay template. `Check()` names the first missing piece (§2.3 reasons). Without
`BOSS_HOSTD_JAILER_BIN` the host runs as in P8a and reports `jailer_on: false` with the reason.

### 8.3 Rate limiters (LLD §3.6)

Firecracker token buckets, refilled once a second: every drive `bandwidth 50 MB/s`, `ops 1000`;
the replica NIC `rx` and `tx` `12.5 MB/s` (100 Mbit/s). The guest gateway caps each VM at 256
connections (§4.3). The limits are part of the VM config and so of its snapshot.

### 8.4 Proven on boss-1

`internal/e2e/p8b_linux_test.go` (run as root): 63 jailed VMs on one host had distinct uids,
`Seccomp: 2`, a new PID namespace, 0700 chroots owned by their uid, cgroup v2 limits under
`/sys/fs/cgroup/boss-hostd/<id>` and a namespace each; a restored clone's uid was refused another
VM's chroot, the store's `meta.json` and the host's spec record while reading only its own
snapshot memory through the ACL; `jailer_on` and `seccomp_default` were measured true with no
reasons; two jailed replicas served with `eth0` bridged in their namespace and the run plane
reached over vsock. P8a-T4 re-run jailed (20 replicas in namespaces + 1 `per_run`) and unjailed
(plain taps): every tap `orig=0 meta=0` in steady state; the deliberate egress and metadata probes
of the cold-booted template and of a restored replica were each dropped and counted on their own
tap only (`orig=2 meta=1` on vm_01 and on vm_02, every other tap 0, both jailed and unjailed)..

## 9. The IPVS directors: boss-lbd (P8b-10, LLD §3.14.2, §3.14.4)

```
 client ──► VIP:443 (MASTER director, VRRP) ── IPVS rr/wlc, persistence off ──► GRE(outer dst = vm_ip) ──► host route ──► tap ──► VM
 VM: VIP on lo, bgre0 decaps, TLS ends in the VM, reply src = VIP straight to the client (bypasses the director)
```

Two small Linux nodes (A and B) run `boss-lbd` + `keepalived`. Both carry traffic: the control
plane groups VIPs (≤ 50 per VRRP instance) and makes A MASTER of the even groups and B of the odd
ones. Nothing here decides ownership of a run: a director fault resets connections at worst
(LLD §2.8).

### 9.1 Enrol, heartbeat, renew

| Call | Path | Auth | Notes |
|---|---|---|---|
| Enrol | `POST /v1/hosts/enrol` with `role: "director"`, `address` | a join token issued with `role: director` (`POST /v1/fleet/join-tokens {"role": "director"}`) | `201`: `host_id` = the director id (`dir_…`), a 24 h certificate with **client and server auth** and an IP SAN of `address`, the fleet CA. A host token on a director (or the reverse) is `400 join_token_role`. |
| Heartbeat | `POST /v1/directors/{id}/heartbeat` | BossHost proof (director certificate) | Every 2 s (§9.3). |
| Desired | `GET /v1/directors/{id}/desired?since=&wait=` | BossHost | Level-based long-poll, like the host's. |
| Renew | `POST /v1/directors/{id}/renew` | BossHost | Same shape as a host renewal; no SVID. |
| Console | `GET /v1/fleet/directors`, `POST /v1/fleet/directors/{id}/retire`, `GET/POST /v1/fleet/vip-pools` | `fleet:read` / `fleet:manage` | A retired director's calls are refused; the peer takes every group. |

A director's certificate authenticates as the `sys_host` principal with credential kind
`director_cert`: host routes refuse it (`401 director_credential_required` the other way round).

### 9.2 Desired state (director)

```json
{"changed": true, "director_id": "dir_…", "generation": 12, "status": "ready",
 "self": {"id": "dir_a", "address": "192.0.2.10", "side": "A", "port": 7446},
 "peer": {"id": "dir_b", "address": "192.0.2.11", "side": "B", "port": 7446},
 "directors": [ …both… ], "vip_pools": ["203.0.113.0/26"],
 "vips": [{"vip": "203.0.113.17", "ports": [443], "scheduler": "rr", "deployment_id": "…", "group": 0,
           "members": [{"replica_id": "rep_…", "vm_ip": "10.200.4.7", "host_id": "hst_…", "weight": 1, "slots": 4}]}],
 "groups": [{"group": 0, "vrid": 1, "master": "dir_a", "vips": ["203.0.113.17"]}],
 "hosts": [{"host_id": "hst_…", "address": "198.51.100.1", "vm_prefix": "10.200.4.0/23"}]}
```

- **Members are serving replicas only** (`agent_replicas.observed_state = 'serving'`, with a
  `vm_ip`), weight = `rs_weight`. A VIP with no serving member still appears (the service exists,
  with no real server).
- `scheduler` is `rr`, or `wlc` once the members' `slots` differ.
- `groups` is computed by the control plane (stored group per VIP, ≤ 50 per group, group *i*
  MASTER on the *i*-th director by id order modulo the pair), so both sides render the same
  keepalived configuration. `vrid` = 1 + group.
- `hosts` is the static route list: `vm_prefix via address` (§5.1).
- **Generation** moves when a replica enters or leaves `serving`, the scheduler changes a host's
  set, a VIP is allocated, a host's address changes, or a director enrols or retires.
- **VIP allocation (D8):** the lowest free address of the org's IPv4 `vip_pool` rows (`routing
  ipvs`) is given to a production microVM deployment on its first placement; `vip_ports` defaults to
  `[443]`. Without a pool the deployment serves internal callers only. The VIP returns to the pool
  when the deployment retires or leaves `microvm`.
- **Stable groups:** a VIP gets its VRRP group at allocation (`agent_deployments.vrrp_group`, the
  lowest group with room) and keeps it; adding a VIP never moves another VIP's MASTER.

What `boss-lbd` does with it: one IPVS virtual service per `(VIP, port)` (TCP, the scheduler,
flags 0 = no persistence), real servers in **tunnel mode with tunnel type GRE**; services inside
`vip_pools` that are no longer desired are removed, services outside the pools are never touched;
the routes; `keepalived.conf` (VRRP only, `nopreempt`, unicast to the peer, `priority 150` on the
MASTER side, `virtual_ipaddress_excluded` for the rest of a group, a `track_script` and `notify_*`
per group) is written and keepalived is sent `SIGHUP` when it changed; the IPVS sync daemons run
as `master` **and** `backup` on both nodes (both carry traffic); sysctls `ip_forward=1`,
`vs.expire_nodest_conn=1`, `vs.conntrack=0`, `rp_filter=0`, `accept_local=1`.

### 9.3 Heartbeat and the director lease

```json
→ {"address": "192.0.2.10", "groups_master": [0, 2], "keepalived_live": true,
   "ipvs": {"services": 4, "dests": 11, "conns": 812, "sync_state": "master+backup"},
   "observed_generation": 12, "contract_version": "1.1"}
← {"director_id": "dir_a", "desired_generation": 12, "leases": {"0": "dir_a", "1": "dir_b", "2": "dir_a"},
   "renew": false, "heartbeat_seconds": 2}
```

`director_leases(org_id, vrrp_group) → holder, expires_at` (**2 s TTL**, renewed by every heartbeat:
**every 0.5 s**; the answer carries `lease_expires`, group → unix ms). A heartbeat takes the lease
of every group its keepalived reports MASTER when it is free, expired or already its own; it never
takes another live director's; it releases its own lease of every group it no longer reports
MASTER (a dead keepalived — pid gone — reports none).

`boss-lbd` faults a group (`track/<group>` = `fault`; keepalived `vrrp_script`, `fall 2`) **only on a
real split brain**: it is MASTER of the group AND the holder (another director) **renewed the lease
after this node became MASTER** (an expiry larger than the one seen in the first answer to a
request sent after the transition — no cross-node clock comparison). The fault is then **latched**
while that director holds the lease, whatever the local VRRP state, so FAULT taking MASTER away
cannot clear it and flap the group (first review). In steady state the BACKUP side is not faulted,
so a director failover is plain VRRP (≈ 3–4 s, LLD §2.14): a dead holder simply stops renewing. A
split brain lasts at most ≈ 1 s (baseline + one renewal) + `fall 2` before the newcomer drops the VIP.
After a healed split brain the latch holds until the holder's lease lapses or is released.

A director silent for 10 s is marked `unreachable` by the scheduler pass; its leases lapse.

### 9.4 Real-server pushes from hosts

```
PUT    /v1/vips/{vip}/members/{replica}   {"vm_ip": "10.200.4.7", "weight": 0|n, "ports": [443], "host_id": "hst_…", "reason": "process_exit"}
DELETE /v1/vips/{vip}/members/{replica}?vm_ip=10.200.4.7&reason=process_exit
```

- **Who may push** (review of PR #120): only an enrolled host's own certificate — issued directly
  by the fleet CA (never under a host's SVID issuer), not a CA, client auth and not server auth
  (a director's has both), no VM SPIFFE id, and a CN the director's desired state names as a live
  host — and only for a `(vip, vm_ip)` the desired state lists under that host. Anything else is
  `403`. A registration can re-weight or re-add a listed member, never add an address.
- **Order:** every push carries `seq` (taken when the host decides; DELETE: `?seq=`); pushes for one
  member run on one lane on the host, and the director and the control plane (`rs_seq`) refuse an
  older `seq` than one applied (`stale`), so the latest decision wins.
- **To every director** in `director_push`, over mTLS on port 7446: the host presents its fleet
  client certificate, the director presents its fleet certificate and the host checks the leaf's
  CN is the expected director id. The director applies it to IPVS **before answering**
  (`{"applied": n, "latency_us": …}`) and records `boss_lbd_withdraw_seconds`. Weight 0 =
  drain (existing connections finish); DELETE = confirmed death (with `expire_nodest_conn=1`
  that VM's connections are reset and clients reconnect elsewhere). A push holds for 30 s
  against a stale desired feed; a feed that no longer lists the member, or a later registration,
  clears it. A registration (weight > 0) ahead of the feed is accepted when the VIP's service
  exists.
- **To the control plane**, the durable record: the host's own replica only
  (`403 replica_not_on_host`), whose deployment owns the VIP (`409 vip_mismatch`); it sets
  `rs_weight` (0 on withdraw/DELETE) and wakes the directors.
- **When:** the watchdog pushes weight 0 the moment a VM turns unhealthy, and DELETE on a
  confirmed death (process exit, three missed pings) or a stop; a replica that turns `serving`
  is registered with its weight. Both run off the watchdog's path (≤ 500 ms per director).

### 9.5 Health checks

1 s interval; 2 failures → weight 0; 4 → delete; 2 successes → restored. The verdict is a
**health set** that never expires: a member the checks took out stays out, whatever the feed or a
registration says, until the checks pass again. A check is a **TCP SYN
inside GRE** to the member's `vm_ip` with the VIP as inner destination and the director's address
as inner source, from a fixed source port per member; the VM's SYN-ACK returns direct (DSR) to
the director; the prober then sends an RST through the same path, closing the VM's half-open
connection. It passes only when GRE is accepted by the host policy, decapsulated in the guest, the
VIP is held there and the listener is up — the exact data path. (A TLS-handshake check needs a
per-member tunnel device on the director; left for a later ticket, decision 31.)

### 9.6 The guest

`ReplicaNetSetup` puts the VIP on `lo` (/32; `arp_ignore=1`, `arp_announce=2` already) and adds
`bgre0` (`mode gre local vm_ip`, no fixed remote, `rp_filter=0`), so either director's packets
decapsulate to the VIP. Replies leave `eth0` with source = VIP (DSR).

### 9.7 Measured on boss-1 (netns lab, `scripts/sandbox/lbd-netlab.sh`)

Two director namespaces (boss-lbd + keepalived 2.2.8, kernel 6.8, GRE tunnel mode), one host
namespace with the exact `boss-hostd netpol-render` policy on three taps, three replica
namespaces (VIP on lo, `bgre0`, a TLS server), one client. 25 checks green, 3 runs (2026-11-15, after
the second PR #120 review):

| Check | Result |
|---|---|
| T1 | 6 TLS connections to the VIP → `v3 v2 v1 v3 v2 v1` (rr over 3 replicas); the director saw 54 GRE packets out and **0** reply packets from the VIP (DSR) |
| T2 health | replica killed → drained after 2 failed 1 s checks (≈ 2 s), deleted 2 s later; a 40-connection client loop: **0 failures** after one retry |
| T2 push | host → both directors over mTLS, weight 0 then DELETE: **2.0–3.7 ms** round trip per director (TLS handshake included); IPVS apply on the director ≤ **0.31 ms** (`boss_lbd_withdraw_seconds_max`); a non-fleet certificate refused |
| T13 | **~1 600 new TLS connections/s** through one director from a single client (client-bound: `openssl s_time`; IPVS counted 8 471 conns, 0 outgoing packets); `ip_vs_conn` is merged into kmalloc on 6.8 (≈ 320 B per entry → ~300 MB per million connections, as §2.8 estimated) |
| VRRP + lease | group 0 MASTER on A, group 1 on B (lease file = the CP's rules, 2 s TTL, 0.5 s renewals); **split brain** (A→B adverts cut 12 s): the stray MASTER held the VIP ≈ 2.5 s (from 3.4 s to 5.9 s), then FAULT, latched, one MASTER entry, no flapping; **failover**: A's keepalived SIGKILLed with its VRRP child first (node death) → B owns the VIP after **3.41–3.58 s** (3 runs; VRRP master-down), a keepalived stop 0.62 s |
| Push auth | another host's replica, a director certificate, a non-fleet certificate, an unknown `vm_ip`: all `403`, IPVS unchanged |

## 10. The agent CA, the replica's TLS listener and AP-D11 (P8b-9/11; LLD §3.13.2, §3.17)

### 10.1 The agent CA (D7)

- **Separate from the fleet CA.** `agent_ca` holds a BOSS agent root (10 y) and two intermediates
  (5 y, pathlen 0): **serving** (replica certificates) and **caller** (service-account client
  certificates). Keys are sealed with a key derived from the vault KEK (`agent-ca/v1`).
- **Issuance** (`POST /v1/run-plane/replica/cert {csr_pem}`): BOSS checks the replica is desired
  (on this host, wanted running, live microvm deployment), the host attests for serving (ready,
  not revoked, heartbeat < 10 s, and on Firecracker `kvm_live` + `firecracker_live`), and the
  image is signed and not revoked (a Firecracker replica must pin one). Answer:
  `{certificate_pem, chain_pem, serial, not_after, renew_after, dns_names, ip_addresses, renews}`.
- **The certificate:** 24 h, `serverAuth`, SAN DNS `<agent>.<project>.agents.<domain>`
  (`BOSS_AGENT_DNS_DOMAIN`, default the trust domain; or the deployment's `hostname`), the VIP as
  an IP SAN, and `spiffe://<td>/deployment/<d>/replica/<r>`. Renew at two thirds with a **new
  key**; the previous certificate stays valid until it expires (no dropped connections).
- **Revocation:** short life + immediate withdraw. Revoking a host revokes its replicas'
  certificates; `POST /v1/fleet/agent-certs/{serial}/revoke` revokes one; unexpired revoked
  serials are listed per org in the replica's (authenticated) trust document. `GET
  /v1/pki/agent-ca` (public) serves only the bundles and the root's SHA-256, and never creates
  the CA: the first authenticated issuance does. Intermediates carry NameConstraints (serving:
  `agents.<domain>` + the trust domain; caller: the trust domain); a hostname outside
  `agents.<domain>` is ignored, and a name another deployment holds is refused
  (`dns_name_taken`).
- **Published to callers:** the signed A2A card of a microvm agent names the CA
  (`bossAgentCa: {url, root_sha256}`, an `mtlsSecurityScheme`, and the `https://<dns>/a2a`
  interface); `/v1/agents/{id}/jwks` carries `boss_agent_ca`.
- **Caller certificates** (`POST /v1/pki/caller-certs`, `service_account_key:manage`): a service
  account's client certificate, ≤ 30 days, `clientAuth`. It is registered as one of the account's
  credentials (`identity_api_credentials`, scope `agent:invoke`), so suspension, revocation and
  the PDP's live check apply to it as to a key.
- **Options, not built:** ACME (a publicly trusted certificate per deployment, for callers that
  cannot pin) and a customer-supplied CA with BOSS as a sub-CA.

### 10.2 The listener (`engine/boss_engine/runtime/listener.py`)

- `boss-runtime --listen VIP:443`: TLS 1.2+ ends in the VM. mTLS is **optional**
  (`CERT_OPTIONAL` against the caller bundle); a bearer is verified **locally** (a BOSS key's
  checksum; a JWT's EdDSA signature against the cached JWKS, `exp`/`nbf`, `iss` and `aud` from
  the trust document; an unknown `kid` re-fetches at most every 30 s). No credential, or one that
  fails, is `401` before BOSS is asked; a negative or non-numeric Content-Length is `400`. The
  caller bundle is retried until it loads and re-read every 30 s.
- **Authorisation is never local:** every request calls `POST /v1/run-plane/admit` with
  `{caller: <Authorization>, client_cert: <PEM>, op, surface}`; `op` is `invoke` (admit + lease
  to this replica), `thread`, `read`, `cancel` or `answer`. BOSS authenticates the forwarded
  credential and re-runs the PDP (`agent:invoke`) each time; `surface: a2a` also runs the D11
  exposure check. A token and a certificate naming different callers are refused.
- **Routes:** the shared library `protocols/agent_serving.py`, the same handlers the control plane
  serves in-process (`/threads`, `/threads/{t}/runs[/stream|/wait]`, `/runs/{stream,wait}`,
  `/threads/{t}/runs/{r}[/stream|/cancel]`, `/a2a`, `/.well-known/agent-card.json`; `/v1` is
  optional). A run leased here is driven in one of the inbox's slots; SSE goes back on the same
  connection (chunked, `Connection: close`).
- **Limits:** 256 concurrent connections (the excess closed at accept), a 4096 backlog with
  `TCP_DEFER_ACCEPT`, a 10 s handshake and 30 s header deadline, a 1 MiB body.

### 10.3 AP-D11: `strong_isolation(deployment)` (`services/strong_isolation.py`)

True only when **all** hold, read at request time, with no flag: `runtime: microvm`, live, not
frozen (`BOSS_MICROVM_CLAIMS_FROZEN`), `strict_secrets` (D21); `vm_mode` set, every serving
replica in that mode, and `per_run` when `expose.public` (D5); at
least one serving replica, and every serving replica's host on Firecracker with `kvm_live`,
`firecracker_live`, `jailer_on`, `seccomp_default`, `strict_secrets`, `net_policy_ok`, no
`facts.fallback_tap` (D15), a heartbeat under 10 s old; the image pinned, signed and active; each
serving replica holding a valid serving certificate and untainted (D17).

- **Lapse → close:** A2A for a write-capable agent is checked on every request, so it closes on the
  next one. The heartbeat stores `agent_hosts.isolation_ok` and bumps the directors when it flips;
  the scheduler's sweep clears it for a host silent > 10 s (`host_events: attestation_lapsed`).
  A deployment exposed over A2A with write tools (`isolation_required`) is fed to the directors
  only with members whose host attests now, holding a valid certificate.
- **`runtime: local` is unchanged:** always false.
- **Host side (asks):** report `fallback_tap: true` in `facts` while the D15 tap is on.

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
8. **Reply-only egress uses GRE and an exact learned flow set**, not IPIP and
   `ct state established` (which cannot match: the host sees only the outer flow). Flows are
   learned from the caller's SYN, refreshed while live, moved to a 30 s closing set on FIN,
   deleted on RST; internal nets are dropped first. **Ask for P8b:** directors use
   `ipvsadm --tun-type gre`. Proven in `scripts/sandbox/hostd-netlab.sh` (§5.1).
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

13. **(1.1) One poll per host, no replica list, BOSS counts slots.** `hosts/next {wait_ms,
    state_gen}`; slots = `agent_replicas.slots` − live leases; serving/draining from `/observed`
    with `state_gen`; `{resync:true}` → one `PUT …/replicas`. Operator decision, LLD §3.13.0.
14. **(1.1) The run token is held by the host**, returned by `hosts/next` and `admit`, attached by
    the gateway, never in the guest.
15. **(1.1) The work inbox shares the number 7105 with `connect`** on purpose: host-initiated and
    guest-initiated vsock ports are separate namespaces.
16. **(1.1) The privileged helper owns its policy** (§5.3); a caller cannot widen the VM prefix or
    choose a tap owner.
17. **(1.1) Adopted VMs carry their own spec record** (`boss-spec.json`), so a mismatch with desired
    state is caught even after the bbolt cache is lost (§3.3).
18. **(1.1) Stopped replicas are reported as `stopped`** until a push carrying them succeeds (§2.4).
19. **(1.1) Until boss-runtime serves the inbox, `boss-vsockd` runs a stand-in** on the same
    socket and protocol (`--no-inbox` hands it to the runtime); it accepts runs up to its slots and
    records them, which is what P8a-T6/T7 exercise.

20. **(P8b) The snapshot key includes `vm_mode`.** The LLD's tuple has no mode, but a `per_run`
    snapshot has no network device and a replica's has one; one snapshot cannot serve both.
21. **(P8b) Snapshot files stay 0600 and are shared by ACL, not by group.** Per-VM uids and
    `MAP_PRIVATE` page sharing both matter; a `user:<uid>:r--` ACL entry per running clone, revoked
    at stop, keeps the owner-only mode and the shared inode. Without ACL support the host copies.
22. **(P8b) The guest tap is `tap0` in every replica namespace;** the host-side veth end carries the
    P8a policy name. One snapshot per shape then restores into any replica's namespace, and netpol
    is unchanged.
23. **(P8b) Warm VMs are named by a host-local slot id** (`w-<8 hex>`) for their whole life; the
    claim writes the control-plane `vm_id` into the spec record. The control plane only ever sees
    `vm_id`.
24. **(P8b) The reseed is RNDADDENTROPY + RNDRESEEDCRNG, not VMGenID alone.** The guest kernel on
    boss-1 (5.10) has no VMGenID driver; the host-delivered seed works on every kernel.
25. **(P8b) A restored clone keeps its snapshot's vsock CID.** Firecracker fixes it at load; the
    host identifies a VM by its socket. The bootstrap carries the CID as information.
26. **(P8b) The replica surge spare exists only on link-owning backends** (jailer + netns), where
    the link can exist before the `vm_ip` is known.
28. **(P8b review) Replica snapshots only under the jailer.** Firecracker 1.10 restores the
    snapshot's `host_dev_name`; without the jailer's uniform `tap0` a clone would open the template's
    tap. `network_overrides` (Firecracker 1.12+) can lift this later.
29. **(P8b-10) The directors' desired state is computed by the control plane, VRRP grouping
    included.** Two directors rendering keepalived from the same `groups` can never disagree about
    who is MASTER; VIPs are chunked by 50 in sorted order (VRID = 1 + group), alternating sides.
30. **(P8b-10) The director lease breaks ties; it never takes VIPs down with the control plane.**
    A director faults a group while another live director holds its lease (whatever its own VRRP
    state, so a split brain cannot flap). An unreachable control
    plane, an empty lease or an expired one leave keepalived's decision alone (LLD §3.14.4 said
    "cannot renew → FAULT"; that would remove every external VIP during a Postgres outage).
31. **(P8b-10) Health checks are a TCP SYN inside GRE through the real data path**, from a fixed
    source port per member, closed by an RST; the host policy admits exactly that reply toward a
    director (§5.1). IPIP is not used anywhere; `moby/ipvs` v1.1.0 cannot set a tunnel type, so
    `internal/lbd/ipvs` speaks the IPVS genetlink family directly. A TLS-handshake check is a
    later ticket.
32. **(P8b-10) A pushed withdraw outranks the desired feed for 30 s.** The feed drops the member
    within ~100 ms through `/observed`; the override is the safety net against a stale feed
    re-adding a dead real server, and a registration or a feed without the member clears it.
27. **(P8b review) No MMDS yet.** Removed until a guest consumer and the in-guest root-only rule
    exist; the vsock bootstrap carries the same data, and the tap keeps counting metadata attempts.

## Asks for the control-plane side

- **Resources:** use `vcpu` and `mem_mib` in `agent_deployments.resources`. The host reads those
  keys.
- **Events (optional):** add `POST /v1/hosts/{id}/events` if crashloop and handle-replay detail
  should land in `host_events`. Without it the states still arrive through heartbeat and observed.
- **P8b:**
  - ~~send `directors`~~ (done, P8b-10: `directors` and `director_push`);
  - send `fences` and `revocations` in desired state.
  The host already enforces them. (Per-run handles arrive with the claimed run, §2.8 — done.)

## Left for later P8b tickets

- Signed images (two flavours) and the image roll with a canary snapshot.
- MMDS V2 (non-secret) for replicas, with the in-guest `meta skuid != 0 drop` rule (decision 27).
- The per-repository setup cache (S22).
- ~~The VIP on `tunl0` in the guest~~ (done, P8b-10: VIP on `lo`, GRE decap on `bgre0`, §9.6).
- ~~Director registration and withdraw (weight 0), with IPVS `--tun-type gre`~~ (done, P8b-10, §9.4).
- A TLS-handshake health check on the directors (decision 31); director metrics into Prometheus
  scraping (today: `127.0.0.1:9446/metrics` on each director).
- The TLS self-check before `serving`.
- ~~The egress gateway route; egress verifying the `boss:egress` delegation token~~ (done, P8b-5:
  §4.9). The gateways' mTLS listeners.
- GitLab and other git hosts (D19: refused until routed); the D22 per-deployment CONNECT allowlist.
- Upstream token exchange for tool-scoped MCP tokens where a provider supports it (§4.7).
