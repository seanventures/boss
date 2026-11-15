# Infra Agent · storage self-healing — API contract

**Epic:** BOSSAPP-1145 · ST-6 labels (1151) · ST-10 Docker reclaim (1155) · ST-12 Infra Agent (1157)
**Spec:** `boss-infra-agent-lld.md` §4–§5 (this file is the wire contract the console screens S1–S5 build against).
**Router:** `apps/api/app/routers/infra_storage.py` · services under `engine/boss_engine/services/storage_*.py`, `work_hold.py`, `host_jobs.py`, `infra_agent.py`.

Every route is JSON. Times are ISO-8601 UTC strings. Sizes are **GB (decimal, 1e9)** as numbers, never strings. Percentages are 0–100.

## 1. Permissions

| Route | Permission (scope) | Who |
|---|---|---|
| `GET /v1/infra/storage/summary` | `system:read` @ org | every viewer |
| `GET /v1/infra/storage/incidents/current` | `system:read` @ org | every viewer (polled by the strip) |
| `GET /v1/infra/storage/incidents` | `system:read` @ org | report lists |
| `GET /v1/infra/storage/incidents/{id}` | `system:read` @ org | report (S5) |
| `GET /v1/infra/storage/incidents/{id}/report.md` | `system:read` @ org | export |
| `POST /v1/infra/storage/incidents/{id}/acknowledge` | `system:read` @ org (`readonly_post` is **false**: it writes `acknowledged_by`) · declared `system:operate` | owner/admin — the strip hides the button otherwise |
| `GET /v1/infra/storage/hold` | `system:read` @ org | strip / Command Center |
| `POST /v1/infra/storage/clean` | `autonomy:manage` @ org (step-up) | "Clean now" (tier 1, no hold) |
| `POST /v1/infra/storage/hold/release` | `autonomy:manage` @ org (step-up) | safety valve |
| `GET /v1/infra/storage/rules` | `system:read` @ org | Storage card |
| `PUT /v1/infra/storage/rules` | `autonomy:manage` @ org (step-up) | thresholds + retention |
| `POST /v1/infra/storage/samples` | `system:operate` @ org (host key) | `boss steward` only |
| `GET /v1/host-jobs/lease` · `POST /v1/host-jobs/{id}/result` · `GET /v1/host-jobs/public-key` | `system:operate` @ org (host key) | `boss steward` only |
| tier-3 cards | existing `/v1/decisions/*` | decision kind `storage_delete`, rule proposals kind `storage_rule` |

The console reads permissions from `apps/web/lib/identity/read-routes.gen.ts` (regenerated with the matrix).

## 2. The incident object

```ts
type IncidentStatus = "warn" | "critical" | "asking" | "resuming" | "resolved";

type StripNode = { type: NodeType; label: string; badges: string[]; icon?: string };
// NodeType is lib/ambient.ts's. The flow uses: signal · agent · action · decision · needs_you · outcome.
// label ≤ 4 words (server-enforced). badges ≤ 2. Node order IS the flow order.

type IncidentFlowNode = StripNode & {
  key: "detected" | "paused" | "cleaning" | "asking" | "freed" | "resumed" | "reported";
  state: "done" | "active" | "waiting" | "ask";
  at: string | null;          // when the node became done/active
  detail: string;             // one sentence for the tooltip / expand
};

type PlanStep = {
  tool: string;               // "storage.prune_build_cache" … (§4)
  tier: 1 | 2 | 3;
  where: "host" | "api";      // host = boss steward does it; api = Postgres-side
  label: string;              // ≤ 4 words, for the step list
  state: "planned" | "queued" | "running" | "done" | "failed" | "refused" | "skipped" | "ask";
  items: number | null;       // dry-run count
  gb: number | null;          // dry-run bytes / 1e9
  freed_gb: number;           // after execution
  plan_hash: string | null;
  decision_id: string | null; // tier 3
  receipt_id: string | null;
  action_id: string | null;   // storage_actions.id
  reason: string | null;      // why skipped/refused/failed
};

type Incident = {
  id: string;                 // "si_…"
  host_id: string;
  host_label: string;         // "this Mac" / "boss-1"
  status: IncidentStatus;
  severity: "warn" | "critical";
  opened_at: string; resolved_at: string | null;
  cause_key: string | null;   // "backups.no_retention" | "docker.vm_unreclaimed" | "pg.temp_spill" | "images.no_retention" | "logs.uncapped" | "growth.fast" | "disk.low"
  cause_text: string;         // plain words, one sentence
  free_pct_start: number; free_pct_now: number; target_pct: number;
  freed_gb: number; eta_seconds: number | null;
  hold: { active: boolean; since: string | null; released_at: string | null; paused_runs: number };
  plan: PlanStep[];
  flow: IncidentFlowNode[];   // S1/S2/S5 draw THIS
  acknowledged_by: string | null; acknowledged_at: string | null;
  spend_usd: number;          // the agent's model spend on this incident
  agent_run_id: string | null;
  helper: { last_seen_at: string | null; reachable: boolean };   // "helper unreachable" after 10 min
  report: Report | null;      // set when resolved (§6)
};
```

### `GET /v1/infra/storage/incidents/current`
```json
{ "incidents": [Incident], "hold": HoldStatus, "generated_at": "…" }
```
Open incidents (status ≠ resolved), most severe first. Empty array when nothing is open. The strip polls this every 10 s and is kicked by SSE kinds `storage.*` and `infra.hold.*`.

### `GET /v1/infra/storage/incidents?limit=20&host_id=`
`{ "incidents": [Incident] }` newest first, resolved included.

### `GET /v1/infra/storage/incidents/{id}`
```json
{ "incident": Incident,
  "actions": [StorageAction],      // one row per tool call (dry runs included)
  "receipts": [{ "id", "dedupe_key", "effect_kind", "status", "created_at" }],
  "paused_work": [{ "job_id", "kind", "run_id", "title", "paused_at", "resumed_at", "waited_seconds" }],
  "decisions": [{ "id", "kind", "status", "question", "created_at" }] }
```
```ts
type StorageAction = { id: string; tool: string; tier: number; where: "host"|"api"; dry_run: boolean;
  status: "planned"|"queued"|"running"|"done"|"failed"|"refused"; items: number|null; bytes: number|null;
  plan_hash: string; receipt_id: string|null; decision_id: string|null; actor: string;
  started_at: string|null; ended_at: string|null; result: { items?: string[]; freed_bytes?: number; error?: string } };
```

### `POST /v1/infra/storage/incidents/{id}/acknowledge`
Body `{}`. Returns `{ "incident": Incident }` with `acknowledged_by` set. The strip stops flashing and keeps a `role=status` line until `resolved`. Idempotent.

## 3. Hold

```ts
type HoldStatus = {
  active: boolean;
  incident_id: string | null; host_id: string | null; since: string | null;
  scope: "all"; exempt_kinds: string[];       // ["agent.turn.interactive", "infra.*"]
  actor: string;                              // "agent:infra" | "person:<id>"
  paused_runs: number;                        // jobs with last_error.code = "infra_hold"
  released_at: string | null;
};
```
`GET /v1/infra/storage/hold` → `{ "hold": HoldStatus }`.
`POST /v1/infra/storage/hold/release` body `{ "reason": string }` → `{ "hold": HoldStatus, "resumed_jobs": number }`. Emits `infra.hold.released`.

A paused job is recognised anywhere by `jobs.last_error.code == "infra_hold"`; its `message` is `"Paused by the Infra Agent (incident si_…): storage is being cleaned; resumes from its checkpoint"` and `last_error.incident_id` carries the id. `code_task_runs.finalize_ctx.paused_by == "infra_agent"` for builds. Agents-at-work rows show a `paused · checkpoint` chip on that.

## 4. Storage summary (S3)

### `GET /v1/infra/storage/summary?host_id=`
```ts
type Summary = {
  host_id: string; host_label: string; sampled_at: string | null; source: "steward"|"api"|"hostd"|null;
  stale: boolean;                      // no sample for > 2 min
  disk: { total_gb: number; free_gb: number; used_pct: number; free_pct: number };
  by_type: Array<{ key: "images"|"build_cache"|"volumes"|"logs"|"backups"|"pg_db"|"pg_temp"|"docker_vm_unreclaimed"|"other"|"free";
                   label: string; gb: number; pct: number; rebuildable: boolean }>;
  forecast: { growth_gb_per_day: number; days_until_full: number | null;   // null = not growing
              full_at: string | null; window_days: number; samples: number;
              with_rules_days_until_full: number | null };
  trend: Array<{ at: string; used_gb: number; free_gb: number }>;          // 30 d, ≤ 240 points
  thresholds: { warn_free_pct: number; critical_free_pct: number; target_free_pct: number;
                floor_free_pct: number; floor_free_gb: number; warn_days: number; critical_days: number;
                pg_temp_critical_gb: number; docker_unreclaimed_gb: number };
  level: "ok" | "warn" | "critical" | "floor";
  rules: Rule[];
  last_cleanup: { incident_id: string; at: string; freed_gb: number; cause_key: string|null } | null;
  open_incident_id: string | null;
  hosts: Array<{ host_id: string; host_label: string; free_pct: number; days_until_full: number|null;
                 level: string; spark: number[] }>;   // every host with samples
};
```

## 5. Rules and thresholds

```ts
type Rule = { id: string; host_id: string | null; kind: "backups"|"images"|"build_cache"|"logs"|"telemetry"|"scratch_db"|"kg_derived"|"docker_trim"|"threshold";
  rule: Record<string, number|string|boolean>;   // backups: {keep_newest, keep_days, cap_gb}; images: {keep_versions}; build_cache: {cap_gb}; logs: {max_size_mb, max_file}; telemetry: {keep_days}; scratch_db: {ttl_hours}; docker_trim: {enabled}; threshold: Thresholds
  enabled: boolean; source: "default"|"operator"|"learned"; decision_id: string|null;
  created_by: string|null; created_at: string; updated_at: string|null; label: string };
```
`GET /v1/infra/storage/rules?host_id=` → `{ "rules": Rule[], "thresholds": Thresholds }`.
`PUT /v1/infra/storage/rules` body `{ "rules": [{ id?, host_id?, kind, rule, enabled }], "thresholds"?: Partial<Thresholds> }` → same as GET. An absent `id` creates; a known `id` updates; nothing is deleted (disable instead). Emits `storage.rule.approved` for each changed rule.

## 6. Clean now, report

`POST /v1/infra/storage/clean` body `{ "host_id"?: string, "dry_run"?: boolean }` → `{ "incident": Incident }`. Opens (or reuses) a `warn` incident **without a hold**, plans tier 1 and runs it. With `dry_run: true` only the plan is filled.

```ts
type Report = {
  summary: string;                                   // two sentences
  freed_gb: number; paused_runs: number; waited_seconds_max: number; lost_or_repeated: 0;
  cost_usd: number; duration_seconds: number;
  cause: { key: string|null; text: string; chips: string[] };
  removed: Array<{ type: string; detail: string; by: "rule"|"person"|"rebuildable"; gb: number; receipt_id: string|null }>;
  learned: { rule_added: Rule|null; rule_proposed: { decision_id: string; rule: Rule["rule"]; kind: string } | null };
  timeline: IncidentFlowNode[];
};
```
`GET /v1/infra/storage/incidents/{id}/report.md` returns `text/markdown`.

## 7. Decisions (S4)

Tier-3 cards are ordinary decisions. `recommendation` carries:
```json
{ "action": "storage_delete", "incident_id": "si_…", "host_id": "…", "tool": "storage.remove_backups",
  "args": { "keep_newest": 10, "keep_days": 7, "cap_gb": 20 }, "plan_hash": "…",
  "items": 348, "gb": 161.2, "keeps": 10, "to_delete": ["boss-backup-2026-09-30T…", "…"], "kept": ["…"],
  "already_done_gb": 42.0, "free_pct_now": 17, "target_pct": 25, "paused_runs": 14,
  "rule_proposal": { "kind": "backups", "rule": { "keep_newest": 10, "keep_days": 7, "cap_gb": 20 }, "preticked": true },
  "alternatives": [ { "label": "Keep 20 instead", "args": { "keep_newest": 20 } } ],
  "never_automatic": ["unlabelled", "data volumes", "non-scratch databases"] }
```
`decision_actions.kind_of` → `"storage_delete"`. Approving executes exactly `plan_hash` (an expired dry run re-plans and re-asks); the approve body may carry `{ "make_rule": true, "args": {…alternative} }`. Reject = "Not now": re-asked in 1 h or on further fall. Rule proposals (cause seen twice) are decisions with `recommendation.action = "storage_rule"`, kind `storage_rule`.

## 8. Events (SSE `GET /v1/events/stream?kinds=storage.,infra.`)

`storage.pressure` · `storage.incident.opened` · `storage.incident.escalated` · `storage.incident.asking` · `storage.incident.resolved` · `infra.hold.set` · `infra.hold.released` · `storage.action.done` · `storage.action.failed` · `storage.rule.proposed` · `storage.rule.approved`. Each carries `incident_id` and `host_id` in `data`.

## 9. Command Center

`GET /v1/command-center/view` gains `infra_incident: Incident | null` (the most severe open one) and `status.key == "infra_incident"` (tone red, text "Infra Agent is cleaning") between `budget_stopped` and `host_critical`. The etag includes incident id, status and freed_gb.

## 10. Host helper wire (`boss steward`)

`POST /v1/infra/storage/samples` body:
```json
{ "host_id": "mac-abc123", "host_label": "this Mac", "source": "steward", "at": "…",
  "total_gb": 494.4, "free_gb": 61.2, "docker_alloc_gb": 128.0, "docker_used_gb": 61.0,
  "images_gb": 29.1, "cache_gb": 8.2, "volumes_gb": 14.0, "logs_gb": 0.6, "backups_gb": 170.0, "backups_n": 358,
  "by_type": { "images": 29.1, "...": 0 }, "steward_version": "1" }
```
→ `{ "recorded": true, "level": "warn", "incident_id": null, "sample_every_seconds": 30 }`.

`GET /v1/host-jobs/lease?host_id=` → `{ "job": HostJob | null }`
```ts
type HostJob = { id: string; host_id: string; verb: "du"|"prune_build_cache"|"remove_images"|"remove_orphan_volumes"|"cap_logs"|"docker_trim"|"remove_backups";
  args: Record<string, unknown>;   // always includes install_id, dry_run, plan_hash (for a real run)
  expires_at: string; signature: string;   // base64 Ed25519 over canonical JSON of {id, host_id, verb, args, expires_at}
  key_id: string };
```
`POST /v1/host-jobs/{id}/result` body `{ "ok": boolean, "dry_run": boolean, "items": string[], "bytes": number, "error"?: string, "plan_hash": string }` → `{ "recorded": true }`.
`GET /v1/host-jobs/public-key` → `{ "key_id": string, "algorithm": "Ed25519", "public_key": "<base64 raw 32 bytes>" }`.

## 11. P8 KVM hosts: `boss-hostd` `storage` verbs (documented, not built)

On microVM hosts the Fleet doctor runs the same storage duty through `boss-hostd` instead of `boss steward` (LLD §7.3). The daemon already posts `DiskUsedPct` in its 2 s heartbeat; this adds to `Pressure`: `images_gb`, `snapshots_gb`, `setup_cache_gb`, `logs_gb`. Jobs arrive over the existing mTLS control channel as the same `HostJob` shape (§10) with these verbs: `du`, `prune_build_cache`, `remove_images`, `remove_orphan_volumes`, `cap_logs`, `remove_vm_snapshots` (`{older_than_hours, keep_newest}`), `prune_setup_cache` (`{cap_gb}`). The label gate is `boss.kind` + `boss.retain_until` (hostd labels every VM image, snapshot and cache entry it makes), enforced in Go on the host exactly as the steward enforces it in Python; the signature is the host's mTLS identity plus the same Ed25519 job signature. Results post to `POST /v1/host-jobs/{id}/result`. Host protection already cordons at 85 % disk; the Infra Agent's incident drives the Fleet doctor's storage steps on those hosts. Implementation lands with P8 (`services/sandbox-service/internal/hostd`), not in this PR.

## 12. What the Layer 1 scripts own (not duplicated here)

`scripts/install/backup.sh` retention (keep 10 / 7 d / 20 GB after each backup), `prune.sh` image retention + `builder prune --keep-storage`, compose `logging:` caps, Postgres guardrails and the build disk floor live on `feat/storage-layer1`. The executor's host verbs act on the same rules at incident time (and `remove_backups` only through the tier-3 card until a rule is approved); `storage_rules.DEFAULT_RULES` mirrors their defaults so the Storage card and the scripts say the same numbers. Coordinator note: `backup.sh` writes no manifest naming the install yet; the steward's backup gate therefore relies on BACKUP_ROOT + the `boss-backup-*` name (a manifest, when present, is honoured and a foreign one refuses the item).
