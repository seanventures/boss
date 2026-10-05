/**
 * Infra › Cloud's row shapes (spec §4.8). `GET /v1/infra/cloud?view=` answers a CloudBody whose
 * rows are one table row per sub-view (lib/infra-view.ts keeps them loose); these parsers are
 * the one place each sub-view's keys are read, and they never invent a field. Money arrives as
 * `*_usd_micros` AND a ready `*_usd` string; only the string is ever drawn. The contract (the
 * exact keys) is written out in tests/_helpers/infra-cloud-fixtures.ts.
 */
import { arr, num, obj, str } from "../../lib/factory-floor";
import { parseWord, type Word } from "../../lib/infra-view";

const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const optNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const bools = (v: unknown): Record<string, boolean> => {
  const out: Record<string, boolean> = {};
  for (const [k, x] of Object.entries(obj(v))) out[k] = x === true;
  return out;
};
const ref = (v: unknown): { id: string; name: string } | null => {
  const o = obj(v);
  return o.id || o.name ? { id: str(o.id), name: str(o.name) } : null;
};
const strs = (v: unknown): string[] => arr(v).map(String);

/** A money cell: the server's string, or `—` when it sent none. Never micros. */
/** The server's dollar string, or `—`. Cost is shown only when > 0: a zero is never `$0.00`. */
export const money = (v: unknown): string => {
  const s = optStr(v);
  return !s || /^\$0(?:\.0+)?$/.test(s.trim()) ? "—" : s;
};

// ── Accounts ────────────────────────────────────────────────────────────────────────────────

export type CloudAccount = {
  id: string; provider: string; providerName: string; label: string; state: Word;
  slots: string[]; credentialVersion: number; lastTest: Word; lastTestAt: string | null;
  authority: string; destructiveBlocked: boolean; resources: number; spend30d: string;
  owner: string | null; scopes: string[];
  flags: { readOnly: boolean; billingReadable: boolean; deployCapable: boolean; destructiveActionsDisabled: boolean };
  impact: { resources: number; spend: string; openChanges: number; receipts: number };
  lastError: string | null;
  rotationSlots: { key: string; required: boolean }[];
  events: { id: string; kind: string; actor: string; at: string | null }[];
  receipts: { id: string; action: string; status: string; at: string | null }[];
  can: Record<string, boolean>;
};
export function parseAccount(w: unknown): CloudAccount {
  const o = obj(w);
  const cr = obj(o.credential);
  const lt = obj(o.last_test);
  const fl = obj(o.flags);
  const im = obj(o.impact);
  return {
    id: str(o.id), provider: str(o.provider), providerName: str(o.provider_name, str(o.provider)), label: str(o.label),
    state: parseWord(o.state), slots: strs(cr.slots), credentialVersion: num(cr.version, 1),
    lastTest: parseWord(lt, "Untested"), lastTestAt: optStr(lt.at), authority: str(o.authority),
    destructiveBlocked: o.destructive_blocked === true, resources: num(o.resources), spend30d: money(o.spend_30d_usd),
    owner: optStr(o.owner), scopes: strs(o.scopes),
    flags: { readOnly: fl.read_only === true, billingReadable: fl.billing_readable === true,
             deployCapable: fl.deploy_capable === true, destructiveActionsDisabled: fl.destructive_actions_disabled === true },
    impact: { resources: num(im.resources), spend: money(im.spend_usd), openChanges: num(im.open_changes), receipts: num(im.receipts) },
    lastError: optStr(o.last_error),
    rotationSlots: arr(o.rotation_slots).map((s) => { const x = obj(s); return { key: str(x.key), required: x.required !== false }; }),
    events: arr(o.events).map((e) => { const x = obj(e); return { id: str(x.id), kind: str(x.kind), actor: str(x.actor), at: optStr(x.at) }; }),
    receipts: arr(o.receipts).map((e) => { const x = obj(e); return { id: str(x.id), action: str(x.action), status: str(x.status), at: optStr(x.at) }; }),
    can: bools(o.can),
  };
}

export type CatalogEntry = {
  provider: string; name: string; category: string; adapter: string; adapterStatus: string;
  categories: string[]; skills: string[]; skillCount: number; pasteable: boolean; baseUrl: boolean;
  authMethods: { key: string; available: boolean; slots: { key: string; required: boolean }[] }[];
};
export function parseCatalog(w: unknown): CatalogEntry[] {
  return arr(w).map((c) => {
    const o = obj(c);
    return {
      provider: str(o.provider), name: str(o.name, str(o.provider)), category: str(o.category), adapter: str(o.adapter),
      adapterStatus: str(o.adapter_status), categories: strs(o.categories), skills: strs(o.skills), skillCount: num(o.skill_count),
      pasteable: o.pasteable === true, baseUrl: o.base_url === true,
      authMethods: arr(o.auth_methods).map((m) => {
        const x = obj(m);
        return { key: str(x.key), available: x.available === true,
                 slots: arr(x.slots).map((s) => { const y = obj(s); return { key: str(y.key), required: y.required !== false }; }) };
      }),
    };
  });
}

// ── Resources ───────────────────────────────────────────────────────────────────────────────

export type CloudResource = {
  id: string; kind: "Resource" | "Droplet"; name: string; provider: string; account: { id: string; name: string } | null;
  region: string | null; service: string | null; env: string | null; owner: string | null; health: Word;
  costMonth: string; lastSeenAt: string | null; ttl: string | null; expiresAt: string | null; externalId: string | null;
  links: { build: string | null; deploy: string | null; budget: string | null; receipt: string | null };
  droplet: { status: string; size: string | null; boundary: string | null; providerRef: string | null; termination: string | null } | null;
};
export function parseResource(w: unknown): CloudResource {
  const o = obj(w);
  const l = obj(o.links);
  const d = o.droplet ? obj(o.droplet) : null;
  return {
    id: str(o.id), kind: str(o.kind) === "Droplet" ? "Droplet" : "Resource", name: str(o.name), provider: str(o.provider),
    account: ref(o.account), region: optStr(o.region), service: optStr(o.service), env: optStr(o.env), owner: optStr(o.owner),
    health: parseWord(o.health, "No signal"), costMonth: money(o.cost_month_usd), lastSeenAt: optStr(o.last_seen_at),
    ttl: optStr(o.ttl), expiresAt: optStr(o.expires_at), externalId: optStr(o.external_id),
    links: { build: optStr(l.build), deploy: optStr(l.deploy), budget: optStr(l.budget), receipt: optStr(l.receipt) },
    droplet: d ? { status: str(d.status), size: optStr(d.size), boundary: optStr(d.boundary), providerRef: optStr(d.provider_ref),
                   termination: optStr(d.termination) } : null,
  };
}

// ── Capacity ────────────────────────────────────────────────────────────────────────────────

export type CloudPool = {
  id: string; poolKey: string; kind: string; desired: number; actual: number; min: number; max: number;
  queuePerWorker: number | null; costHour: string; plan: { word: string; tone: Word["tone"]; summary: string; at: string | null } | null;
  history: { at: string | null; from: number; to: number; reason: string }[];
  plans: { id: string; at: string | null; action: string; desired: number | null; reason: string }[];
};
export function parsePool(w: unknown): CloudPool {
  const o = obj(w);
  const p = o.plan ? obj(o.plan) : null;
  return {
    id: str(o.id), poolKey: str(o.pool_key), kind: str(o.kind), desired: num(o.desired), actual: num(o.actual),
    min: num(o.min), max: num(o.max), queuePerWorker: optNum(o.queue_per_worker), costHour: money(o.cost_hour_usd),
    plan: p ? { ...parseWord(p), summary: str(p.summary), at: optStr(p.at) } : null,
    history: arr(o.history).map((h) => { const x = obj(h); return { at: optStr(x.at), from: num(x.from), to: num(x.to), reason: str(x.reason) }; }),
    plans: arr(o.plans).map((h) => { const x = obj(h); return { id: str(x.id), at: optStr(x.at), action: str(x.action), desired: optNum(x.desired), reason: str(x.reason) }; }),
  };
}
export type CapacityHeader = {
  queue: { depth: number; leased: number; running: number; realProvision: boolean } | null;
  apiHealth: { provider: string; state: Word; api: string; rateLimit: string; usageCap: string; checkedAt: string | null }[];
  fabric: { pools: number; instances: number; realHostEffects: number; byKind: Record<string, number> } | null;
};
export function parseCapacityHeader(h: Record<string, unknown>): CapacityHeader {
  const q = h.queue ? obj(h.queue) : null;
  const f = h.fabric ? obj(h.fabric) : null;
  const byKind: Record<string, number> = {};
  if (f) for (const [k, v] of Object.entries(obj(f.by_kind))) byKind[k] = num(v);
  return {
    queue: q ? { depth: num(q.depth), leased: num(q.leased), running: num(q.running), realProvision: q.real_provision === true } : null,
    apiHealth: arr(h.api_health).map((a) => {
      const x = obj(a);
      return { provider: str(x.provider), state: parseWord(x.state), api: str(x.api), rateLimit: str(x.rate_limit),
               usageCap: str(x.usage_cap), checkedAt: optStr(x.checked_at) };
    }),
    fabric: f ? { pools: num(f.pools), instances: num(f.instances), realHostEffects: num(f.real_host_effects), byKind } : null,
  };
}

// ── Spend ───────────────────────────────────────────────────────────────────────────────────

export type CloudSpend = {
  id: string; provider: string; spend30d: string; share: number; events: number; anomalies: number;
  estimated: boolean; included: boolean;
  categories: { name: string; usd: string }[];
  costEvents: { id: string; category: string; usd: string; confidence: string; at: string | null }[];
  reconciliation: { id: string; state: Word; at: string | null; resourcesSeen: number; costsSeen: number }[];
  usage: { events: number; ledger: number; billingMirror: string | null };
};
export function parseSpend(w: unknown): CloudSpend {
  const o = obj(w);
  const u = obj(o.usage);
  return {
    id: str(o.id), provider: str(o.provider, str(o.id)), spend30d: money(o.spend_30d_usd),
    share: Math.max(0, Math.min(1, num(o.share))), events: num(o.events), anomalies: num(o.anomalies),
    estimated: o.estimated === true, included: o.included === true,
    categories: arr(o.categories).map((c) => { const x = obj(c); return { name: str(x.name), usd: money(x.usd) }; }),
    costEvents: arr(o.cost_events).map((c) => { const x = obj(c); return { id: str(x.id), category: str(x.category), usd: money(x.usd), confidence: str(x.confidence), at: optStr(x.at) }; }),
    reconciliation: arr(o.reconciliation).map((c) => {
      const x = obj(c);
      return { id: str(x.id), state: parseWord(x.state), at: optStr(x.at), resourcesSeen: num(x.resources_seen), costsSeen: num(x.costs_seen) };
    }),
    usage: { events: num(u.events), ledger: num(u.ledger), billingMirror: optStr(u.billing_mirror) },
  };
}
export type Anomaly = { id: string; provider: string; severity: Word; summary: string; status: string };
export const parseAnomalies = (v: unknown): Anomaly[] => arr(v).map((a) => {
  const x = obj(a);
  return { id: str(x.id), provider: str(x.provider), severity: parseWord(x.severity), summary: str(x.summary), status: str(x.status) };
});

// ── Changes ─────────────────────────────────────────────────────────────────────────────────

export type CloudChange = {
  id: string; title: string; summary: string; account: { id: string; name: string } | null; risk: number;
  cost: string; state: Word; createdAt: string | null;
  operations: { action: string; resourceType: string; resourceRef: string | null; destructive: boolean }[];
  operationsJson: string; rollbackPlan: string[];
  receipts: { id: string; action: string; status: string }[];
  drift: { id: string; classification: string; summary: string; unmanaged: number; orphaned: number; security: number }[];
  can: Record<string, boolean>;
};
export function parseChange(w: unknown): CloudChange {
  const o = obj(w);
  return {
    id: str(o.id), title: str(o.title), summary: str(o.summary), account: ref(o.account), risk: num(o.risk),
    cost: money(o.cost_usd), state: parseWord(o.state), createdAt: optStr(o.created_at),
    operations: arr(o.operations).map((x) => {
      const y = obj(x);
      return { action: str(y.action), resourceType: str(y.resource_type), resourceRef: optStr(y.resource_ref), destructive: y.destructive === true };
    }),
    operationsJson: str(o.operations_json), rollbackPlan: strs(o.rollback_plan),
    receipts: arr(o.receipts).map((x) => { const y = obj(x); return { id: str(y.id), action: str(y.action), status: str(y.status) }; }),
    drift: arr(o.drift).map((x) => {
      const y = obj(x);
      return { id: str(y.id), classification: str(y.classification), summary: str(y.summary), unmanaged: num(y.unmanaged),
               orphaned: num(y.orphaned), security: num(y.security) };
    }),
    can: bools(o.can),
  };
}

/** Accounts a form may pick (`+ Change`, `+ Pool`, `Clean up…`): from the body's filters. */
export function accountOptions(filters: Record<string, string[]>, header: Record<string, unknown>): { id: string; name: string }[] {
  const listed = arr(header.accounts).map((a) => ref(a)).filter((a): a is { id: string; name: string } => !!a);
  if (listed.length) return listed;
  return (filters.account ?? []).map((a) => ({ id: a, name: a }));
}

/** `12.5` dollars → micros, for the routes that take micros. NaN or negative → null. */
export function dollarsToMicros(text: string): number | null {
  const t = text.trim().replace(/^\$/, "");
  if (!t) return 0;
  const n = Number(t);
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.round(n * 1_000_000);
}

/** Validate an Operations editor: a JSON array of objects, each with skill_id, action and
 *  resource_type. Answers the parsed list, or the first problem with its line. */
export function parseOperations(text: string): { ok: true; ops: Record<string, unknown>[] } | { ok: false; error: string; line: number | null } {
  if (!text.trim()) return { ok: false, error: "Add at least one operation", line: null };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    const pos = /position (\d+)/.exec(msg);
    const lineMatch = /line (\d+)/.exec(msg);
    const line = lineMatch ? Number(lineMatch[1]) : pos ? text.slice(0, Number(pos[1])).split("\n").length : null;
    return { ok: false, error: line ? `Not valid JSON · line ${line}` : "Not valid JSON", line };
  }
  if (!Array.isArray(value) || value.length === 0) return { ok: false, error: "Needs a list of operations", line: 1 };
  for (let i = 0; i < value.length; i += 1) {
    const op = value[i];
    const at = lineOfItem(text, i);
    if (!op || typeof op !== "object" || Array.isArray(op)) return { ok: false, error: `Operation ${i + 1} is not an object · line ${at}`, line: at };
    for (const k of ["skill_id", "action", "resource_type"]) {
      if (typeof (op as Record<string, unknown>)[k] !== "string" || !(op as Record<string, unknown>)[k]) {
        return { ok: false, error: `Operation ${i + 1} needs ${k} · line ${at}`, line: at };
      }
    }
  }
  return { ok: true, ops: value as Record<string, unknown>[] };
}

/** The line an array's i-th top-level item starts on (for line errors). */
function lineOfItem(text: string, i: number): number {
  let depth = 0;
  let seen = -1;
  let quote = false;
  for (let p = 0; p < text.length; p += 1) {
    const c = text[p];
    if (quote) { if (c === "\\") p += 1; else if (c === "\"") quote = false; continue; }
    if (c === "\"") { quote = true; continue; }
    if (c === "[" || c === "{") {
      depth += 1;
      if (depth === 2) { seen += 1; if (seen === i) return text.slice(0, p).split("\n").length; }
    } else if (c === "]" || c === "}") depth -= 1;
  }
  return 1;
}
