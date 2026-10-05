/**
 * Infra › Cloud fixtures (spec §4.8): `GET /v1/infra/cloud?view=accounts|resources|capacity|spend|changes`
 * wire bodies, and the routes mockInfraFetch() serves for Cloud. THIS HEADER IS THE ROW CONTRACT
 * infra_view.cloud() must answer (CloudBody's `rows` are loose in infra_view.py; app/infra/cloud-rows.ts
 * reads exactly these keys). Every body: {view, counts, rows, filters, header, notices, partial, etag}.
 *
 *   counts   {accounts, resources, pools, providers, changes, anomalies} — all six on every view
 *            (the segmented control shows Accounts→accounts, Resources→resources, Capacity→pools,
 *            Spend→providers, Changes→changes)
 *   notices  [{key: "read_only" | "byok", tip}] — read_only hides every write control
 *   Word     {word, tone} as infra_view.word() says it; money: `*_usd_micros` int AND `*_usd` string
 *            ("$12.40", "<$0.01"); only the string is drawn
 *
 *   view=accounts   rows[]: id, provider, provider_name, label, state: Word,
 *                     credential{slots: [slot key], version}, last_test: Word & {at},
 *                     authority ("Read only" | "Deploy capable" | "Custom"), destructive_blocked,
 *                     resources, spend_30d_usd_micros, spend_30d_usd, owner, scopes[],
 *                     flags{read_only, billing_readable, deploy_capable, destructive_actions_disabled},
 *                     impact{resources, spend_usd_micros, spend_usd, open_changes, receipts},
 *                     last_error, rotation_slots[{key, required}], events[{id, kind, actor, at}] ≤4,
 *                     receipts[{id, action, status, at}] ≤4,
 *                     can{manage, test, sync, enable, disable, rotate, revoke}
 *                   header.catalog[]: provider, name, category, adapter, adapter_status
 *                     ("available" | "planned"), categories[], skills[] (≤5 names), skill_count,
 *                     pasteable, base_url, auth_methods[{key, available, slots[{key, required}]}]
 *                   header.accounts[{id, name}] (pickers in + Change / + Pool / Clean up…)
 *   view=resources  rows[]: id, kind ("Resource" | "Droplet"), name, provider, account{id,name}|null,
 *                     region, service, env, owner, health: Word, cost_month_usd_micros, cost_month_usd,
 *                     last_seen_at, ttl ("4h" | null), expires_at, external_id,
 *                     links{build (run id), deploy (deploy ref), budget, receipt},
 *                     droplet{status, size, boundary, provider_ref, termination} | null
 *                   filters{provider[], account[], env[], region[], service[]}
 *                   query params honoured: provider, account, env, region, service
 *   view=capacity   rows[]: id, pool_key, kind, desired, actual, min, max, queue_per_worker,
 *                     cost_hour_usd_micros, cost_hour_usd, plan: (Word & {summary, at}) | null,
 *                     history[{at, from, to, reason}] ≤8, plans[{id, at, action, desired, reason}] ≤4
 *                   header.queue{depth, leased, running, real_provision},
 *                   header.api_health[{provider, state: Word, api, rate_limit, usage_cap, checked_at}],
 *                   header.fabric{pools, instances, real_host_effects, by_kind{kind: n}}
 *   view=spend      rows[] (one per provider; id = provider key): id, provider, spend_30d_usd_micros,
 *                     spend_30d_usd, share (0..1 of the 30d total), events, anomalies, estimated,
 *                     included (all on a subscription → "Included in plan"),
 *                     categories[{name, usd_micros, usd}], cost_events[{id, category, usd_micros, usd,
 *                     confidence, at}] ≤5, reconciliation[{id, state: Word, at, resources_seen,
 *                     costs_seen}] ≤3, usage{events, ledger, billing_mirror}
 *                   header.anomalies[{id, provider, severity: Word, summary, status}]
 *   view=changes    rows[]: id, title, summary, account{id,name}|null, risk (tier), cost_usd_micros,
 *                     cost_usd, state: Word, created_at,
 *                     operations[{action, resource_type, resource_ref, destructive}], operations_json,
 *                     rollback_plan[], receipts[{id, action, status}],
 *                     drift[{id, classification, summary, unmanaged, orphaned, security}],
 *                     can{approve, apply}
 */
import type { Route, W } from "./infra-fixtures";

const MIN = 60_000;
const at = (m: number): string => new Date(Date.now() + m * MIN).toISOString();
const word = (w: string, tone: string) => ({ word: w, tone });

const COUNTS = { accounts: 2, resources: 3, pools: 2, providers: 2, changes: 2, anomalies: 1 };
const ACCOUNTS = [{ id: "acct_1", name: "DigitalOcean · prod" }, { id: "acct_2", name: "AWS · sandbox" }];

export function cloudAccountsFixture(): W {
  return {
    view: "accounts", counts: COUNTS,
    rows: [
      { id: "acct_1", provider: "digitalocean", provider_name: "DigitalOcean", label: "DigitalOcean · prod", state: word("Active", "green"),
        credential: { slots: ["api_token"], version: 3 }, last_test: { ...word("Fresh", "green"), at: at(-30) },
        authority: "Deploy capable", destructive_blocked: true, resources: 2, spend_30d_usd_micros: 41_200_000, spend_30d_usd: "$41.20",
        owner: "ceo", scopes: ["read", "droplet:write"],
        flags: { read_only: false, billing_readable: true, deploy_capable: true, destructive_actions_disabled: true },
        impact: { resources: 2, spend_usd_micros: 41_200_000, spend_usd: "$41.20", open_changes: 1, receipts: 4 },
        last_error: null, rotation_slots: [{ key: "api_token", required: true }],
        events: [{ id: "ev_1", kind: "provider.tested", actor: "ceo", at: at(-30) }],
        receipts: [{ id: "rr_1", action: "sync", status: "succeeded", at: at(-60) }],
        can: { manage: true, test: true, sync: true, enable: false, disable: true, rotate: true, revoke: true } },
      { id: "acct_2", provider: "aws", provider_name: "AWS", label: "AWS · sandbox", state: word("Disabled", "grey"),
        credential: { slots: ["access_key_id", "secret_access_key"], version: 1 }, last_test: { ...word("Untested", "hollow"), at: null },
        authority: "Read only", destructive_blocked: true, resources: 1, spend_30d_usd_micros: 3_000, spend_30d_usd: "<$0.01",
        owner: null, scopes: ["read"],
        flags: { read_only: true, billing_readable: false, deploy_capable: false, destructive_actions_disabled: true },
        impact: { resources: 1, spend_usd_micros: 3_000, spend_usd: "<$0.01", open_changes: 0, receipts: 0 },
        last_error: null, rotation_slots: [{ key: "access_key_id", required: true }, { key: "secret_access_key", required: true }],
        events: [], receipts: [],
        can: { manage: true, test: false, sync: false, enable: true, disable: false, rotate: true, revoke: true } },
    ],
    filters: {},
    header: {
      accounts: ACCOUNTS,
      catalog: [
        { provider: "digitalocean", name: "DigitalOcean", category: "infra", adapter: "digitalocean_v2", adapter_status: "available",
          categories: ["compute", "apps", "dns"], skills: ["droplet.create", "droplet.resize", "app.deploy"], skill_count: 12,
          pasteable: true, base_url: false,
          auth_methods: [{ key: "api_token", available: true, slots: [{ key: "api_token", required: true }] }] },
        { provider: "aws", name: "AWS", category: "infra", adapter: "aws_boto3", adapter_status: "available",
          categories: ["compute", "storage"], skills: ["ec2.run", "s3.put"], skill_count: 30, pasteable: false, base_url: false,
          auth_methods: [{ key: "access_key", available: true,
                           slots: [{ key: "access_key_id", required: true }, { key: "secret_access_key", required: true }] }] },
        { provider: "openai", name: "OpenAI", category: "model", adapter: "openai_compat", adapter_status: "available",
          categories: ["models"], skills: ["chat"], skill_count: 2, pasteable: true, base_url: true,
          auth_methods: [{ key: "api_key", available: true, slots: [{ key: "api_key", required: true }] }] },
        { provider: "hetzner", name: "Hetzner", category: "infra", adapter: "", adapter_status: "planned",
          categories: ["compute"], skills: [], skill_count: 0, pasteable: false, base_url: false,
          auth_methods: [{ key: "api_token", available: false, slots: [{ key: "api_token", required: true }] }] },
      ],
    },
    notices: [{ key: "byok", tip: "Community connects your own provider keys" }],
    partial: [], generated_at: at(0),
  };
}

export function cloudResourcesFixture(filters: Record<string, string> = {}): W {
  let rows: W[] = [
    { id: "res_1", kind: "Resource", name: "invoice-db", provider: "digitalocean", account: ACCOUNTS[0], region: "nyc3", service: "databases",
      env: "prod", owner: "invoice", health: word("Up", "green"), cost_month_usd_micros: 15_000_000, cost_month_usd: "$15.00",
      last_seen_at: at(-20), ttl: null, expires_at: null, external_id: "db-7f3a",
      links: { build: "run_inv_1", deploy: "stack:sd_live", budget: "bud_1", receipt: "rr_1" }, droplet: null },
    { id: "res_2", kind: "Resource", name: "assets-bucket", provider: "aws", account: ACCOUNTS[1], region: "us-east-1", service: "s3",
      env: "sandbox", owner: null, health: word("No signal", "hollow"), cost_month_usd_micros: 3_000, cost_month_usd: "<$0.01",
      last_seen_at: at(-300), ttl: null, expires_at: null, external_id: "arn:aws:s3:::assets",
      links: { build: null, deploy: null, budget: null, receipt: null }, droplet: null },
    { id: "drop_1", kind: "Droplet", name: "preview-runner-1", provider: "digitalocean", account: ACCOUNTS[0], region: "nyc3", service: "droplets",
      env: "preview", owner: "factory", health: word("Up", "green"), cost_month_usd_micros: 6_000_000, cost_month_usd: "$6.00",
      last_seen_at: at(-2), ttl: "4h", expires_at: at(240), external_id: "4123987",
      links: { build: "run_search_1", deploy: null, budget: null, receipt: null },
      droplet: { status: "active", size: "s-1vcpu-1gb", boundary: "sandbox", provider_ref: "do:4123987", termination: "on expiry" } },
  ];
  for (const k of ["provider", "env", "region", "service"]) if (filters[k]) rows = rows.filter((r) => r[k] === filters[k]);
  if (filters.account) rows = rows.filter((r) => (r.account as W | null)?.id === filters.account);
  return {
    view: "resources", counts: COUNTS, rows,
    filters: { provider: ["aws", "digitalocean"], account: ["acct_1", "acct_2"], env: ["preview", "prod", "sandbox"],
               region: ["nyc3", "us-east-1"], service: ["databases", "droplets", "s3"] },
    header: { accounts: ACCOUNTS }, notices: [{ key: "byok", tip: "Community connects your own provider keys" }], partial: [], generated_at: at(0),
  };
}

export function cloudCapacityFixture(): W {
  return {
    view: "capacity", counts: COUNTS,
    rows: [
      { id: "pool_1", pool_key: "local-runners", kind: "local", desired: 3, actual: 2, min: 1, max: 4, queue_per_worker: 5,
        cost_hour_usd_micros: 0, cost_hour_usd: "$0.00", plan: { ...word("Scale up", "amber"), summary: "Queue 12 over 2 workers", at: at(-5) },
        history: [{ at: at(-5), from: 1, to: 2, reason: "Queue depth 12" }, { at: at(-120), from: 2, to: 1, reason: "Idle for 30m" }],
        plans: [{ id: "plan_1", at: at(-5), action: "scale_up", desired: 3, reason: "Queue 12 over 2 workers" }] },
      { id: "pool_2", pool_key: "do-burst", kind: "cloud", desired: 0, actual: 0, min: 0, max: 6, queue_per_worker: 8,
        cost_hour_usd_micros: 18_000, cost_hour_usd: "$0.02", plan: null, history: [], plans: [] },
    ],
    filters: {},
    header: {
      accounts: ACCOUNTS,
      queue: { depth: 12, leased: 2, running: 2, real_provision: false },
      api_health: [
        { provider: "digitalocean", state: word("OK", "green"), api: "available", rate_limit: "ok", usage_cap: "ok", checked_at: at(-4) },
        { provider: "aws", state: word("High", "amber"), api: "available", rate_limit: "near limit", usage_cap: "ok", checked_at: at(-9) },
      ],
      fabric: { pools: 2, instances: 2, real_host_effects: 0, by_kind: { local: 1, cloud: 1 } },
    },
    notices: [{ key: "byok", tip: "Community connects your own provider keys" }], partial: [], generated_at: at(0),
  };
}

export function cloudSpendFixture(): W {
  return {
    view: "spend", counts: COUNTS,
    rows: [
      { id: "digitalocean", provider: "digitalocean", spend_30d_usd_micros: 41_200_000, spend_30d_usd: "$41.20", share: 0.93, events: 31,
        anomalies: 1, estimated: false, included: false,
        categories: [{ name: "compute", usd_micros: 30_000_000, usd: "$30.00" }, { name: "databases", usd_micros: 11_200_000, usd: "$11.20" }],
        cost_events: [{ id: "ce_1", category: "compute", usd_micros: 1_200_000, usd: "$1.20", confidence: "billed", at: at(-60) }],
        reconciliation: [{ id: "rec_1", state: word("Done", "green"), at: at(-90), resources_seen: 3, costs_seen: 31 }],
        usage: { events: 31, ledger: 31, billing_mirror: "do-billing-2026-10" } },
      { id: "aws", provider: "aws", spend_30d_usd_micros: 3_100_000, spend_30d_usd: "$3.10", share: 0.07, events: 4, anomalies: 0,
        estimated: true, included: false,
        categories: [{ name: "storage", usd_micros: 3_100_000, usd: "$3.10" }],
        cost_events: [{ id: "ce_2", category: "storage", usd_micros: 3_000, usd: "<$0.01", confidence: "estimated_from_price_catalog", at: at(-200) }],
        reconciliation: [], usage: { events: 4, ledger: 4, billing_mirror: null } },
    ],
    filters: {},
    header: { anomalies: [{ id: "an_1", provider: "digitalocean", severity: word("High", "amber"), summary: "Compute spend 3× the 7-day average", status: "open" }] },
    notices: [{ key: "byok", tip: "Community connects your own provider keys" }], partial: [], generated_at: at(0),
  };
}

export function cloudChangesFixture(): W {
  return {
    view: "changes", counts: COUNTS,
    rows: [
      { id: "cs_1", title: "Resize invoice droplet", summary: "Move to 2 vCPU for month-end", account: ACCOUNTS[0], risk: 2,
        cost_usd_micros: 12_000_000, cost_usd: "$12.00", state: word("Needs you", "amber"), created_at: at(-40),
        operations: [{ action: "resize", resource_type: "droplet", resource_ref: "drop_1", destructive: false }],
        operations_json: "[{\"skill_id\":\"digitalocean.droplet\",\"action\":\"resize\",\"resource_type\":\"droplet\",\"resource_ref\":\"drop_1\"}]",
        rollback_plan: ["Resize back to s-1vcpu-1gb"], receipts: [],
        drift: [{ id: "dr_1", classification: "unmanaged", summary: "1 droplet BOSS did not create", unmanaged: 1, orphaned: 0, security: 0 }],
        can: { approve: true, apply: false } },
      { id: "cs_2", title: "Delete old bucket", summary: "Remove assets-old", account: ACCOUNTS[1], risk: 3,
        cost_usd_micros: 0, cost_usd: "$0.00", state: word("Applied", "green"), created_at: at(-3000),
        operations: [{ action: "delete", resource_type: "s3_bucket", resource_ref: "assets-old", destructive: true }],
        operations_json: "[]", rollback_plan: [], receipts: [{ id: "rr_9", action: "delete", status: "succeeded" }], drift: [],
        can: { approve: false, apply: false } },
    ],
    filters: {}, header: { accounts: ACCOUNTS },
    notices: [{ key: "byok", tip: "Community connects your own provider keys" }], partial: [], generated_at: at(0),
  };
}

export function cloudFixture(view: string, filters: Record<string, string> = {}): W {
  switch (view) {
    case "resources": return cloudResourcesFixture(filters);
    case "capacity": return cloudCapacityFixture();
    case "spend": return cloudSpendFixture();
    case "changes": return cloudChangesFixture();
    default: return cloudAccountsFixture();
  }
}

/** The same body as a read-only viewer sees it (no write control may render). */
export const readOnly = (body: W): W => ({ ...body, notices: [{ key: "read_only", tip: "You lack providerops_manage" }] });

export const CLOUD_ROUTES: Route[] = [
  { match: /^\/v1\/infra\/cloud$/, etag: true, reply: (u) => {
    const f: Record<string, string> = {};
    for (const k of ["provider", "account", "env", "region", "service"]) { const v = u.searchParams.get(k); if (v) f[k] = v; }
    return cloudFixture(u.searchParams.get("view") ?? "accounts", f);
  } },
  { method: "POST", match: /^\/v1\/providers\/easy-connect$/, reply: () => ({ secret_ref_id: "sref_new", provider_account_id: "acct_new" }) },
  { method: "POST", match: /^\/v1\/changesets$/, reply: () => ({ id: "cs_new", status: "pending_approval" }) },
];
