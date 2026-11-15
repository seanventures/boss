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

- `directors: ["192.0.2.10"]`: the IPv4 addresses allowed to send GRE-encapsulated caller traffic to replicas (P8b).
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
| 7101 | model | `POST /anthropic/v1/messages`, `/openai/v1/chat/completions`, `/openai/v1/responses` | `endpoints.model_gateway` (P8b) |
| 7102 | git / api | `GET,POST /git/…`; `GET,POST,PATCH,PUT /api/…` | `endpoints.egress` (P8b) |
| 7103 | mcp | `GET,POST,DELETE /mcp/…` | `endpoints.mcp_gateway` (P8b) |
| 7104 | run_plane | `/v1/run-plane/next` and `/v1/run-plane/hosts/…` **denied** (VMs never poll for work); any other `/v1/run-plane/…` | `endpoints.run_plane` (the control plane) |
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
  - `→ @internal_nets` is dropped (`orig_`), before any accept;
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
27. **(P8b review) No MMDS yet.** Removed until a guest consumer and the in-guest root-only rule
    exist; the vsock bootstrap carries the same data, and the tap keeps counting metadata attempts.

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

## Left for later P8b tickets

- Signed images (two flavours) and the image roll with a canary snapshot.
- MMDS V2 (non-secret) for replicas, with the in-guest `meta skuid != 0 drop` rule (decision 27).
- The per-repository setup cache (S22).
- The VIP on `tunl0` in the guest.
- Director registration and withdraw (weight 0), with IPVS `--tun-type gre` (§5.1).
- The TLS self-check before `serving`.
- The central model, egress and MCP gateways the routes already point at.
- Per-run DPoP-bound tokens held by the host.
