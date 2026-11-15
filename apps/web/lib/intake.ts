/**
 * EVENTS IN THE CONSOLE (P9a-7, BOSSAPP-1099; wireframes E1 Sources, E2 Connect Slack, E4 Rules,
 * E5 Stream, E6 Dead letters, E7 People). One client for the routes in
 * apps/api/app/routers/intake.py (connections, events, dead letters, replay, SSO attestation,
 * people links), the intake routes on routers/agent_platform.py (an agent's triggers of
 * `kind=intake`, preview-match), the chat endpoint Slack rides on (routers/chat_channels.py) and
 * a rule's daily budget (a scoped budget of scope `trigger`, routes_scoped_budgets.py).
 *
 * Parsing is pure and defensive. A read answers `{ok, data}` or `{ok:false, error, …}` with the
 * error as a sentence. Writes go through obsWrite (one press, one effect, an Idempotency-Key) and
 * are registered in lib/identity/write-permissions.ts with the permission their route checks.
 * A secret is sent once and never read back: the API answers field NAMES only.
 */
import { obsWrite, type WriteResult } from "../app/observability/obs-write";

type R = Record<string, unknown>;
const o = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);
const n = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const strs = (v: unknown) => a(v).map((x) => s(x)).filter(Boolean);
const enc = encodeURIComponent;
const press = (scope: string, label: string) => ({ scope: `events:${scope}`, label });

// ── reads ─────────────────────────────────────────────────────────────────────────────────────
export type Read<T> = { ok: true; data: T } | { ok: false; error: string; code: string; status: number; denied: boolean };

const CODE_WORDS: Record<string, string> = {
  not_found: "That is gone. Refresh the page.",
  missing_field: "A required field is empty.",
  missing_secret: "A required secret is empty.",
  endpoint_taken: "This Slack app already has a connection.",
  reason_required: "Say why, in a few words.",
  unknown_idp: "That sign-in provider is not active in this org.",
  idp_has_no_domains: "That sign-in provider has no verified domains yet.",
  domain_not_verified: "Pick a domain the sign-in provider has verified.",
  secrets_required: "Paste both secrets.",
  channel_required: "Name the channel BOSS posts in.",
};
export const codeWords = (code: string, message = "") => message || CODE_WORDS[code] || "BOSS could not do that just now.";

export async function evRead<T>(path: string, parse: (v: unknown) => T): Promise<Read<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (r.ok) return { ok: true, data: parse(body) };
    const d = o(body).detail;
    const code = typeof d === "object" && d ? s(o(d).error) : s(o(body).error);
    const msg = typeof d === "string" ? d : s(o(d).message) || s(o(body).message);
    const denied = r.status === 403;
    return { ok: false, code, status: r.status, denied,
             error: denied ? (msg || "You don't have permission to see this. Ask an admin.") : codeWords(code, msg) };
  } catch (e) {
    return { ok: false, code: "network", status: 0, denied: false, error: `BOSS could not be reached (${(e as Error)?.message ?? e}).` };
  }
}

// ── the catalog and connections (E1, E2) ──────────────────────────────────────────────────────
export type AuthField = { name: string; label: string; secret: boolean; required: boolean; help: string };
export type Source = {
  key: string; label: string; icon: string; description: string; modes: string[];
  auth_fields: AuthField[]; event_types: string[]; capabilities: Record<string, boolean>;
};
export function parseSource(v: unknown): Source {
  const x = o(v);
  return {
    key: s(x.key) || s(x.kind), label: s(x.label) || s(x.key), icon: s(x.icon), description: s(x.description),
    modes: strs(x.modes),
    auth_fields: a(x.auth_fields).map((f) => ({ name: s(o(f).name), label: s(o(f).label) || s(o(f).name),
      secret: o(f).secret === true, required: o(f).required !== false, help: s(o(f).help) })).filter((f) => f.name),
    event_types: strs(x.event_types),
    capabilities: Object.fromEntries(Object.entries(o(x.capabilities)).map(([k, val]) => [k, val === true])),
  };
}

export type Attestation = { idp: string; idp_name: string; domains: string[]; attested_by: string; attested_at: string };
export const parseAttestation = (v: unknown): Attestation | null => {
  const x = o(v);
  return s(x.idp) ? { idp: s(x.idp), idp_name: s(x.idp_name), domains: strs(x.domains), attested_by: s(x.attested_by), attested_at: s(x.attested_at) } : null;
};

export type ConnStatus = "active" | "paused" | "disabled" | "auth_failed" | "rate_limited";
export type Connection = {
  id: string; project_id: string; kind: string; label: string; name: string; mode: string; status: ConnStatus;
  url: string; url_path: string; chat_endpoint_id: string; secret_fields: string[];
  team_id: string; default_dm_agent: string; attestation: Attestation | null;
  health: { last_test_at: string; last_test_ok: boolean | null; refusals: number; status_reason: string };
  rules: number; last_hour: number; backlog: number; dead: number; last_event_at: string;
};
const STATUSES: ConnStatus[] = ["active", "paused", "disabled", "auth_failed", "rate_limited"];
export function parseConnection(v: unknown): Connection {
  const x = o(v);
  const cfg = o(x.config);
  const h = o(x.health);
  const st = s(x.status) as ConnStatus;
  return {
    id: s(x.id), project_id: s(x.project_id), kind: s(x.kind), label: s(x.label) || s(x.kind), name: s(x.name), mode: s(x.mode) || "push",
    status: STATUSES.includes(st) ? st : "active", url: s(x.url), url_path: s(x.url_path),
    chat_endpoint_id: s(x.chat_endpoint_id) || s(cfg.chat_endpoint_id), secret_fields: strs(x.secret_fields),
    team_id: s(cfg.team_id), default_dm_agent: s(cfg.default_dm_agent), attestation: parseAttestation(cfg.sso_attested),
    health: { last_test_at: s(h.last_test_at), last_test_ok: typeof h.last_test_ok === "boolean" ? h.last_test_ok : null,
              refusals: n(h.refusals), status_reason: s(h.status_reason) },
    rules: n(x.rules), last_hour: n(x.last_hour), backlog: n(x.backlog), dead: n(x.dead), last_event_at: s(x.last_event_at),
  };
}
/** Where a connection is, in a few words: its own name, the Slack workspace, or how it arrives. */
export function whereWords(c: Connection): string {
  if (c.name && c.name.toLowerCase() !== c.label.toLowerCase() && c.name.toLowerCase() !== c.kind) return c.name;
  if (c.kind === "slack") return c.mode === "socket" ? "Socket Mode" : c.team_id ? `Workspace ${c.team_id}` : "The install's Slack app";
  if (c.kind === "github") return "Signed webhook";
  return c.mode === "push" ? "Generic webhook · signed" : c.mode;
}

export type TestResult = { ok: boolean; checks: Record<string, unknown>; last_hour: number; backlog: number; dead: number; last_event_at: string };
export const parseTest = (v: unknown): TestResult => {
  const x = o(v);
  return { ok: x.ok === true, checks: o(x.checks), last_hour: n(x.last_hour), backlog: n(x.backlog), dead: n(x.dead), last_event_at: s(x.last_event_at) };
};
/** The test's checks as short lines: "Signing secret", "Bot token: unresolvable". */
export function checkWords(t: TestResult): { label: string; ok: boolean }[] {
  return Object.entries(t.checks).map(([k, v]) => {
    const name = k.replace(/^secret:/, "").replace(/_/g, " ");
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    if (typeof v === "string") return { label: v === "ok" ? label : `${label}: ${v}`, ok: v === "ok" };
    const ok = o(v).ok === true;
    return { label: ok ? "Source check" : `Source check: ${s(o(v).detail) || "failed"}`, ok };
  });
}

// ── events (E5) ───────────────────────────────────────────────────────────────────────────────
export type EventStatus = "received" | "processing" | "routed" | "ignored" | "filtered" | "refused" | "failed" | "dead" | "shed" | "digest";
export const EVENT_STATUSES: EventStatus[] = ["received", "processing", "routed", "ignored", "filtered", "refused", "failed", "dead", "shed", "digest"];
export type Tone = "green" | "grey" | "amber" | "red" | "violet" | "blue";
/** The stream's chips, in the wireframe's order: one word, one tone, the meaning in the tooltip. */
export const STATUS_CHIPS: { status: EventStatus; word: string; tip: string; tone: Tone }[] = [
  { status: "routed", word: "Routed", tip: "Matched a rule and woke an agent", tone: "green" },
  { status: "ignored", word: "Ignored", tip: "Not for any agent (no mention, our own bot, a duplicate)", tone: "grey" },
  { status: "filtered", word: "Filtered", tip: "A rule saw it but its filter said no", tone: "grey" },
  { status: "refused", word: "Refused", tip: "A guard said no: unlinked person, limit, budget or stop", tone: "amber" },
  { status: "failed", word: "Retrying", tip: "Failed or held for now; BOSS tries again by itself", tone: "amber" },
  { status: "dead", word: "Dead", tip: "Kept failing; set aside for a person to replay", tone: "red" },
  { status: "digest", word: "Digest", tip: "Part of a flood; summarised into one run every 5 minutes", tone: "violet" },
  { status: "shed", word: "Shed", tip: "Over the connection's hard cap; kept, not routed", tone: "red" },
  { status: "received", word: "Waiting", tip: "Saved; the router has not looked at it yet", tone: "blue" },
  { status: "processing", word: "Routing", tip: "The router is looking at it now", tone: "blue" },
];
export const chipOf = (st: string): { status: string; word: string; tip: string; tone: Tone } =>
  STATUS_CHIPS.find((c) => c.status === st) ?? { status: st, word: st, tip: st, tone: "grey" };

export type Actor = { kind: string; display: string; external_id: string };
export type EventSummary = {
  id: string; connection_id: string; connection_name: string; kind: string; status: EventStatus; type: string; subject: string;
  text: string; actor: Actor; screen_verdict: string; attempts: number; duplicates: number; runs: string[]; last_error: string;
  received_at: string; event_time: string; processed_at: string; next_attempt_at: string; replays: number;
};
export function parseEvent(v: unknown): EventSummary {
  const x = o(v);
  const st = s(x.status) as EventStatus;
  return {
    id: s(x.id), connection_id: s(x.connection_id), connection_name: s(x.connection_name), kind: s(x.kind),
    status: EVENT_STATUSES.includes(st) ? st : "received", type: s(x.type), subject: s(x.subject), text: s(x.text),
    actor: { kind: s(o(x.actor).kind), display: s(o(x.actor).display), external_id: s(o(x.actor).external_id) },
    screen_verdict: s(x.screen_verdict), attempts: n(x.attempts), duplicates: n(x.duplicates), runs: strs(x.runs),
    last_error: s(x.last_error), received_at: s(x.received_at), event_time: s(x.event_time), processed_at: s(x.processed_at),
    next_attempt_at: s(x.next_attempt_at), replays: n(x.replays),
  };
}
export type Counts = Record<EventStatus | "total", number>;
export const parseCounts = (v: unknown): Counts => {
  const x = o(v);
  return Object.fromEntries([...EVENT_STATUSES, "total"].map((k) => [k, n(x[k])])) as Counts;
};
export type EventList = { events: EventSummary[]; counts: Counts };
export const parseEventList = (v: unknown): EventList => ({ events: a(o(v).events).map(parseEvent).filter((e) => e.id), counts: parseCounts(o(v).counts) });

export type Subject = { mode: string; kind: string; principal_id: string; method: string; display: string; reason: string; read_only: boolean };
export type Outcome = {
  trigger_id: string; fire_outcome: string; run_id: string; thread_id: string; reason: string; digest: boolean;
  filtered: string; subject: Subject | null; replay: { n: number; mode: string; by: string; reason: string } | null;
};
export type Fire = { id: string; trigger_id: string; outcome: string; reason: string; run_id: string; fired_at: string; trigger_name: string };
export type EventDetail = EventSummary & {
  dedupe_key: string; conversation_key: string; verify_scheme: string; envelope: R; screen: R; outcome: Outcome[]; fires: Fire[];
  raw: unknown; replay_of: string; principal_id: string;
  /** The caller lacks user:read: who the chat user is in BOSS (principal, emails) was withheld by the API. */
  people_redacted: boolean;
};
export function parseOutcome(v: unknown): Outcome {
  const x = o(v);
  const sub = x.subject && typeof x.subject === "object" ? o(x.subject) : null;
  return {
    trigger_id: s(x.trigger_id), fire_outcome: s(x.fire_outcome), run_id: s(x.run_id), thread_id: s(x.thread_id), reason: s(x.reason),
    digest: x.digest === true, filtered: s(x.filtered),
    subject: sub ? { mode: s(sub.mode), kind: s(sub.kind), principal_id: s(sub.principal_id), method: s(sub.method), display: s(sub.display),
                     reason: s(sub.reason), read_only: sub.read_only === true } : null,
    replay: x.replay !== undefined ? { n: n(x.replay), mode: s(x.mode), by: s(x.by), reason: s(x.reason) } : null,
  };
}
export function parseEventDetail(v: unknown): EventDetail {
  const x = o(v);
  return {
    ...parseEvent(v), dedupe_key: s(x.dedupe_key), conversation_key: s(x.conversation_key), verify_scheme: s(x.verify_scheme),
    envelope: o(x.envelope), screen: o(x.screen), outcome: a(x.outcome).map(parseOutcome),
    fires: a(x.fires).map((f) => ({ id: s(o(f).id), trigger_id: s(o(f).trigger_id), outcome: s(o(f).outcome), reason: s(o(f).reason),
      run_id: s(o(f).run_id), fired_at: s(o(f).fired_at), trigger_name: s(o(f).trigger_name) })),
    raw: x.raw ?? null, replay_of: s(x.replay_of), principal_id: s(x.principal_id),
    // Fail closed: only an explicit false from the API shows the person.
    people_redacted: x.people_redacted !== false,
  };
}

// ── the event as a flow (E5's detail: "visibility is a diagram, not text") ───────────────────
export type NodeState = "done" | "failed" | "waiting" | "skipped" | "active";
export type FlowNode = { key: string; label: string; detail: string; tip: string; state: NodeState };

const METHOD_WORDS: Record<string, string> = { code: "Code", sso_email: "SSO email", aad_oid: "Entra ID" };
export const methodWords = (m: string) => METHOD_WORDS[m] || (m ? m.replace(/_/g, " ") : "");
export const IDENTITY_WORDS: Record<string, { word: string; tip: string }> = {
  person: { word: "The person", tip: "Whoever sent the message. No linked account, no run." },
  person_or_owner: { word: "Person, else owner", tip: "The linked person if any, else you" },
  owner: { word: "Owner", tip: "You, within the permission you granted" },
  owner_readonly: { word: "Owner, read-only", tip: "You, with every write tool removed" },
};
const KIND_LABELS: Record<string, string> = { slack: "Slack", webhook: "Webhook", github: "GitHub", teams: "Teams", servicenow: "ServiceNow" };
export const labelOfKind = (k: string) => KIND_LABELS[k] || (k ? k.charAt(0).toUpperCase() + k.slice(1) : "");

/** Where an event came from, for the first node: "#C0SUPPORT" for Slack, the subject otherwise. */
export function placeOf(e: EventSummary | EventDetail): string {
  const env = "envelope" in e ? e.envelope : {};
  const ch = s(o(o(env).data).channel) || s(o(o(env).bossconversation).channel);
  if (ch) return `#${ch}`;
  if (e.kind === "slack" && e.subject) return `#${e.subject.split("/")[0]}`;
  return e.connection_name || e.subject || labelOfKind(e.kind) || e.type;
}
function shortReason(r: string): string {
  return r.split(/[.;(:]/)[0].trim().slice(0, 28);
}

/**
 * Source → Checked → Saved → Rule → For whom → Run → Reply, from the event's own record. Each node
 * says what happened in one or two words; where it stopped is the failed node, and what never
 * happened after it is greyed. `ruleName` names a trigger id (the screen has the rules loaded);
 * `runDone` is the run's state when the screen read it (null: not read).
 */
export function eventFlow(e: EventDetail, ruleName: (id: string) => string, runDone: boolean | null = null): FlowNode[] {
  const nodes: FlowNode[] = [];
  const source = e.connection_name || labelOfKind(e.kind) || "Source";
  nodes.push({ key: "source", label: labelOfKind(e.kind) || source, detail: placeOf(e), tip: `${source} · ${e.type}`, state: "done" });
  nodes.push({ key: "checked", label: "Checked", detail: "signature ✓",
               tip: e.verify_scheme ? `The source's signature matched (${e.verify_scheme.replace(/_/g, " ")})` : "Verified at the front door", state: "done" });
  nodes.push({ key: "saved", label: "Saved", detail: e.duplicates ? `once · ${e.duplicates} repeat${e.duplicates === 1 ? "" : "s"}` : "once",
               tip: `Event ${e.id}${e.duplicates ? ` · ${e.duplicates} repeat deliveries ignored` : " · no duplicate"}`, state: "done" });
  const fired = e.outcome.filter((x) => x.trigger_id && x.fire_outcome);
  const main = fired.find((x) => ["admitted", "duplicate", "skipped"].includes(x.fire_outcome))
    ?? fired.find((x) => x.fire_outcome === "refused" || x.fire_outcome === "deferred") ?? fired[0];
  if (!main) {
    const why = e.outcome.find((x) => x.filtered)?.filtered || e.outcome.find((x) => x.reason)?.reason || e.last_error;
    const failing = e.status === "dead" || e.status === "failed";
    const label = e.status === "dead" ? "Failed" : e.status === "failed" ? "Retrying" : e.status === "received" || e.status === "processing" ? "Rule" : "No rule";
    const detail = e.status === "filtered" ? "filtered" : e.status === "shed" ? "shed" : e.status === "ignored" ? "none matched"
      : e.status === "dead" ? `${e.attempts} tries` : e.status === "failed" ? "held" : "waiting";
    nodes.push({ key: "rule", label, detail, tip: why || "No rule on this connection matched", state: failing ? "failed" : e.status === "received" || e.status === "processing" ? "active" : "skipped" });
    nodes.push({ key: "whom", label: "For whom", detail: "", tip: "Did not happen", state: "skipped" });
    nodes.push({ key: "run", label: "Run", detail: "", tip: "Did not happen", state: "skipped" });
    return nodes;
  }
  const rule = ruleName(main.trigger_id);
  nodes.push({ key: "rule", label: "Rule", detail: rule, tip: main.reason || `Matched ${rule}`, state: "done" });
  const sub = main.subject;
  if (sub && sub.kind === "none") {
    const unlinked = sub.mode === "person" || /link/i.test(sub.reason);
    nodes.push({ key: "whom", label: "For whom?", detail: unlinked ? "not linked" : "nobody", tip: sub.reason || "Nobody to act for", state: "failed" });
    if (unlinked) nodes.push({ key: "prompt", label: "Link sent", detail: "private DM", tip: "One-time sign-in link, at most once an hour", state: sub.reason && !/prompt|link/i.test(sub.reason) ? "skipped" : "done" });
    return nodes;
  }
  const who = sub?.kind === "person" ? (sub.display || "the person") : "Owner";
  nodes.push({ key: "whom", label: `For ${who}`, detail: sub?.kind === "person" ? methodWords(sub.method) : sub?.read_only ? "read-only" : "standing grant",
               tip: sub?.reason || (sub?.kind === "person" ? `Linked by ${methodWords(sub.method)}` : "The rule owner's standing grant"), state: "done" });
  if (main.fire_outcome === "refused" || main.fire_outcome === "deferred") {
    nodes.push({ key: "run", label: main.fire_outcome === "refused" ? "Refused" : "Held", detail: shortReason(main.reason), tip: main.reason,
                 state: main.fire_outcome === "refused" ? "failed" : "waiting" });
    return nodes;
  }
  if (main.digest) {
    nodes.push({ key: "run", label: "Digest", detail: "every 5 min", tip: "Summarised into one run every 5 minutes", state: "done" });
    return nodes;
  }
  const run = main.run_id || e.runs[0] || "";
  nodes.push({ key: "run", label: run ? `Run ${run.slice(-4)}` : "Run", detail: main.fire_outcome === "duplicate" ? "already ran" : rule,
               tip: run ? `Run ${run} · ${main.fire_outcome}` : main.fire_outcome, state: run ? "done" : "waiting" });
  if (e.kind === "slack") {
    nodes.push({ key: "reply", label: "Reply", detail: "in thread",
                 tip: runDone ? "The run finished and answered in the thread" : runDone === false ? "The run is still working" : "Sent by the run through the reply outbox",
                 state: runDone ? "done" : runDone === false ? "active" : "waiting" });
  }
  return nodes;
}
export function flowSentence(nodes: FlowNode[]): string {
  return nodes.map((x) => `${x.label}${x.detail ? ` ${x.detail}` : ""}${x.state === "failed" ? " (stopped here)" : x.state === "skipped" ? " (did not happen)" : ""}`).join(" → ");
}

// ── dead letters (E6) ─────────────────────────────────────────────────────────────────────────
export type DeadLetter = EventSummary & { routes: string[]; first_failed_at: string; dead_at: string };
export type DeadLetters = { items: DeadLetter[]; per_connection: { connection_id: string; kind: string; name: string; dead: number }[] };
export const parseDeadLetters = (v: unknown): DeadLetters => ({
  items: a(o(v).dead_letters).map((x) => ({ ...parseEvent(x), routes: strs(o(x).routes), first_failed_at: s(o(x).first_failed_at), dead_at: s(o(x).dead_at) })).filter((d) => d.id),
  per_connection: a(o(v).per_connection).map((x) => ({ connection_id: s(o(x).connection_id), kind: s(o(x).kind), name: s(o(x).name), dead: n(o(x).dead) })),
});

// ── agents and rules (E4) ─────────────────────────────────────────────────────────────────────
export type Agent = { key: string; name: string; environments: string[]; cost_7d_usd: number };
export const parseAgents = (v: unknown): Agent[] => a(o(v).agents).map((x) => ({
  key: s(o(x).key), name: s(o(x).name) || s(o(x).key),
  environments: a(o(x).live).map((l) => s(o(l).environment)).filter(Boolean), cost_7d_usd: n(o(x).cost_7d_usd),
})).filter((x) => x.key);
/** The environment a new rule goes on: prod when the agent is live there, else its first. */
export const envOf = (g: Agent | undefined) => (g?.environments.includes("prod") ? "prod" : g?.environments[0] || "prod");

export type QuietHours = { start: string; end: string; timezone: string; mode: string };
export type Triage = { mode: "off" | "model"; daily_usd_cap: number; max_usd_per_event: number; candidates: string[] };
export type Limits = { per_entity: number | null; per_entity_window_seconds: number | null; storm_per_min: number | null };
export type Rule = {
  id: string; name: string; agent_key: string; environment: string; connection_id: string; priority: number; on: boolean; status: string;
  paused_reason: string; identity_mode: string; identity_modes: string[]; match: unknown; addressing: string; fanout: string;
  triage: Triage; limits: Limits; quiet_hours: QuietHours | null; digest_mode: boolean; match_ok: boolean; match_error: string;
  fires: number; last_fired_at: string; last_outcome: string; on_behalf_of: string; config: R;
};
export function parseRule(v: unknown): Rule {
  const x = o(v);
  const cfg = o(x.config);
  const tri = o(cfg.triage);
  const lim = o(x.limits);
  const q = o(x.quiet_hours);
  const num = (k: unknown) => (typeof k === "number" && k > 0 ? k : null);
  return {
    id: s(x.id), name: s(x.name), agent_key: s(x.agent_key), environment: s(x.environment), connection_id: s(x.connection_id),
    priority: n(x.priority, 100), on: x.on === true || s(x.status) === "on", status: s(x.status), paused_reason: s(x.paused_reason),
    identity_mode: s(x.identity_mode), identity_modes: strs(x.identity_modes), match: cfg.match ?? null,
    addressing: s(cfg.addressing) || "mention_or_dm", fanout: s(cfg.fanout) || "first_match",
    triage: { mode: tri.mode === "model" ? "model" : "off", daily_usd_cap: n(tri.daily_usd_cap, 5), max_usd_per_event: n(tri.max_usd_per_event, 0.002), candidates: strs(tri.candidates) },
    limits: { per_entity: num(lim.per_entity), per_entity_window_seconds: num(lim.per_entity_window_seconds), storm_per_min: num(lim.storm_per_min) },
    quiet_hours: s(q.start) || s(q.mode) ? { start: s(q.start), end: s(q.end), timezone: s(q.timezone) || "UTC", mode: s(q.mode) } : null,
    digest_mode: x.digest_mode === true, match_ok: x.match_ok !== false, match_error: s(x.match_error), fires: n(x.fires),
    last_fired_at: s(x.last_fired_at), last_outcome: s(x.last_outcome), on_behalf_of: s(o(x.runs_as).on_behalf_of), config: cfg,
  };
}
export const byOrder = (x: Rule, y: Rule) => x.priority - y.priority || x.id.localeCompare(y.id);

export type PreviewEvent = { id: string; type: string; subject: string; status: string; received_at: string; matched: boolean; text: string; actor: string };
export type Preview = { ok: boolean; error: string; matched: number; considered: number; events: PreviewEvent[] };
export const parsePreview = (v: unknown): Preview => {
  const x = o(v);
  return { ok: x.ok !== false, error: s(x.error), matched: n(x.matched), considered: n(x.considered),
           events: a(x.events).map((e) => ({ id: s(o(e).id), type: s(o(e).type), subject: s(o(e).subject), status: s(o(e).status),
             received_at: s(o(e).received_at), matched: o(e).matched === true, text: s(o(e).text), actor: s(o(e).actor) })) };
};

export type RouteBudget = { id: string; scope_id: string; cap_usd: number; spent_usd: number; period: string };
export const parseBudgets = (v: unknown): RouteBudget[] => a(o(v).policies)
  .filter((p) => s(o(p).scope_type) === "trigger")
  .map((p) => ({ id: s(o(p).id), scope_id: s(o(p).scope_id), cap_usd: n(o(p).cap_usd_micros) / 1e6,
                 spent_usd: n(o(p).spent_usd_micros) / 1e6, period: s(o(p).period) }))
  .filter((p) => p.id);

// ── people (E7) ───────────────────────────────────────────────────────────────────────────────
export type PersonState = "linked" | "not_linked" | "guest" | "revoked";
export type Person = {
  external_user_id: string; display: string; state: PersonState; link_id: string; method: string;
  person: { principal_id: string; name: string; email: string } | null; linked_at: string; revoked_at: string; last_seen_at: string; prompted_at: string;
};
export type People = { connection_id: string; kind: string; endpoint_id: string; attestation: Attestation | null; people: Person[];
  /** The caller lacks user:read: who each linked person is was withheld by the API. */
  people_redacted: boolean;
  counts: { linked: number; not_linked: number; guests: number; revoked: number } };
export function parsePeople(v: unknown): People {
  const x = o(v);
  const c = o(x.counts);
  const states: PersonState[] = ["linked", "not_linked", "guest", "revoked"];
  return {
    connection_id: s(x.connection_id), kind: s(x.kind), endpoint_id: s(x.endpoint_id), attestation: parseAttestation(x.attestation),
    people_redacted: x.people_redacted === true,
    people: a(x.people).map((p) => {
      const y = o(p);
      const st = s(y.state) as PersonState;
      const who = y.person && typeof y.person === "object" ? o(y.person) : null;
      return { external_user_id: s(y.external_user_id), display: s(y.display), state: states.includes(st) ? st : "not_linked", link_id: s(y.link_id),
        method: s(y.method), person: who ? { principal_id: s(who.principal_id), name: s(who.name), email: s(who.email) } : null,
        linked_at: s(y.linked_at), revoked_at: s(y.revoked_at), last_seen_at: s(y.last_seen_at), prompted_at: s(y.prompted_at) };
    }).filter((p) => p.external_user_id),
    counts: { linked: n(c.linked), not_linked: n(c.not_linked), guests: n(c.guests), revoked: n(c.revoked) },
  };
}

// ── the chat endpoint Slack rides on, and the org's sign-in providers (E2) ───────────────────
export type ChatEndpoint = { id: string; kind: string; name: string; status: string; channel: string; secrets_set: string[]; url: string };
export const parseEndpoint = (e: unknown): ChatEndpoint => {
  const x = o(e);
  const u = o(x.urls);
  return { id: s(x.id), kind: s(x.kind), name: s(x.name), status: s(x.status), channel: s(o(x.config).channel), secrets_set: strs(x.secrets_set),
           url: s(u.event_subscriptions_request_url) || s(u.webhook_url) };
};
export const parseEndpoints = (v: unknown): ChatEndpoint[] => a(o(v).endpoints).map(parseEndpoint).filter((e) => e.id);
export type Idp = { id: string; name: string; domains: string[]; status: string };
export const parseIdps = (v: unknown): Idp[] => a(o(v).providers).map((p) => {
  const x = o(p);
  return { id: s(x.id), name: s(x.name) || s(x.display_name), domains: strs(x.domains), status: s(x.status) || "active" };
}).filter((p) => p.id);

// ── read paths ────────────────────────────────────────────────────────────────────────────────
const conn = (id: string) => `/v1/intake/connections/${enc(id)}`;
export const readCatalog = () => evRead("/v1/intake/catalog", (v) => a(o(v).sources).map(parseSource).filter((x) => x.key));
export const readConnections = (projectId: string) => evRead(`/v1/intake/connections?project_id=${enc(projectId)}`, (v) => a(o(v).connections).map(parseConnection).filter((c) => c.id));
export type EventQuery = { status?: string; connection_id?: string; q?: string; hours?: number; limit?: number };
export function readEvents(projectId: string, q: EventQuery = {}) {
  const p = new URLSearchParams({ project_id: projectId });
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== "") p.set(k, String(v));
  return evRead(`/v1/intake/events?${p.toString()}`, parseEventList);
}
export const readEvent = (id: string) => evRead(`/v1/intake/events/${enc(id)}`, parseEventDetail);
export const readDeadLetters = (projectId: string) => evRead(`/v1/intake/dead-letters?project_id=${enc(projectId)}`, parseDeadLetters);
export const readAgents = (projectId: string) => evRead(`/v1/agent-platform/agents?project_id=${enc(projectId)}`, parseAgents);
export const readRulesOf = (projectId: string, agentKey: string, connectionId: string) =>
  evRead(`/v1/agents/${enc(agentKey)}/triggers?project_id=${enc(projectId)}&kind=intake&connection_id=${enc(connectionId)}`,
         (v) => a(o(v).triggers).map(parseRule).filter((r) => r.id));
/** Every rule on one connection, across the agents (each agent's triggers of `kind=intake`). */
export async function readRules(projectId: string, agents: Agent[], connectionId: string): Promise<Read<Rule[]>> {
  const all = await Promise.all(agents.map((g) => readRulesOf(projectId, g.key, connectionId)));
  const failed = all.find((r): r is Extract<Read<Rule[]>, { ok: false }> => !r.ok);
  if (failed && all.every((r) => !r.ok)) return failed;
  const seen = new Set<string>();
  const rules = all.flatMap((r) => (r.ok ? r.data : [])).filter((r) => (seen.has(r.id) ? false : (seen.add(r.id), true)));
  return { ok: true, data: rules.sort(byOrder) };
}
export const readPeople = (connectionId: string) => evRead(`${conn(connectionId)}/people`, parsePeople);
export const readChatEndpoints = (projectId: string) => evRead(`/v1/chat/endpoints?project_id=${enc(projectId)}`, parseEndpoints);
export const readIdps = () => evRead("/v1/identity/providers", parseIdps);
export const readRouteBudgets = (projectId: string) => evRead(`/v1/budget-policies/scoped?project_id=${enc(projectId)}`, parseBudgets);
export const readRunStatus = (threadId: string, runId: string, projectId: string) =>
  evRead(`/v1/threads/${enc(threadId)}/runs/${enc(runId)}?project_id=${enc(projectId)}`, (v) => s(o(v).status) || s(o(o(v).run).status));

// ── writes ────────────────────────────────────────────────────────────────────────────────────
export type ConnectInput = { kind: string; name?: string; mode?: string; config?: R; secrets?: Record<string, string> };
export const createConnection = (projectId: string, input: ConnectInput): Promise<WriteResult> =>
  obsWrite("/v1/intake/connections", { project_id: projectId, ...input }, press("new", "Connect"));
export const updateConnection = (id: string, patch: { name?: string; config?: R; secrets?: Record<string, string> }): Promise<WriteResult> =>
  obsWrite(conn(id), patch as R, press(`${id}:save`, "Save"), "PATCH");
export const deleteConnection = (id: string): Promise<WriteResult> => obsWrite(conn(id), {}, press(`${id}:remove`, "Remove"), "DELETE");
export const testConnection = (id: string): Promise<WriteResult> => obsWrite(`${conn(id)}/test`, {}, press(`${id}:test`, "Test"));
export const rotateConnectionSecret = (id: string): Promise<WriteResult> => obsWrite(`${conn(id)}/rotate-secret`, {}, press(`${id}:rotate`, "Rotate secret"));
export const setConnectionPaused = (id: string, paused: boolean): Promise<WriteResult> =>
  obsWrite(`${conn(id)}/${paused ? "pause" : "resume"}`, {}, press(`${id}:pause`, paused ? "Pause" : "Resume"));
export const attestConnection = (id: string, idp: string, domains: string[]): Promise<WriteResult> =>
  obsWrite(`${conn(id)}/sso-attestation`, { identity_provider_id: idp, domains }, press(`${id}:attest`, "Confirm"), "PUT");
export const clearAttestation = (id: string): Promise<WriteResult> => obsWrite(`${conn(id)}/sso-attestation`, {}, press(`${id}:attest-off`, "Turn off"), "DELETE");
export const createSlackEndpoint = (projectId: string, input: { name: string; channel: string; signing_secret: string; bot_token: string }): Promise<WriteResult> =>
  obsWrite("/v1/chat/endpoints", { project_id: projectId, kind: "slack", name: input.name, config: { channel: input.channel },
    secrets: { signing_secret: input.signing_secret, bot_token: input.bot_token } }, press("slack", "Save"));
export const replayEvents = (projectId: string, ids: string[], mode: "reprocess" | "force", reason = ""): Promise<WriteResult> =>
  obsWrite("/v1/intake/events/replay", { project_id: projectId, intake_event_ids: ids, mode, reason }, press(`replay:${mode}`, mode === "force" ? "Force run" : "Reprocess"));
/** "Would match": a read-only dry run over the connection's last 50 events (integration:read). */
export const previewMatch = (projectId: string, actor: string, agentKey: string, connectionId: string, match: unknown): Promise<WriteResult> =>
  obsWrite(`/v1/agents/${enc(agentKey || "any")}/triggers/preview-match`, { project_id: projectId, actor, connection_id: connectionId, match, limit: 50 }, press("preview", "Would match"));
export type RuleBody = {
  name: string; connection_id?: string; priority: number; identity_mode: string; config: R;
  limits: Record<string, number>; quiet_hours: Partial<QuietHours>;
};
// Agent writes carry the signed-in actor the page hands them (tests/agent-writes-carry-the-signed-in-actor).
export const createRule = (projectId: string, actor: string, agentKey: string, environment: string, body: RuleBody): Promise<WriteResult> =>
  obsWrite(`/v1/agents/${enc(agentKey)}/triggers`, { project_id: projectId, actor, environment, kind: "intake", ...body }, press("rule:new", "Save rule"));
export const updateRule = (projectId: string, actor: string, ruleId: string, patch: Partial<RuleBody>): Promise<WriteResult> =>
  obsWrite(`/v1/agent-triggers/${enc(ruleId)}`, { project_id: projectId, actor, ...patch }, press(`rule:${ruleId}`, "Save rule"), "PATCH");
export const setRulePaused = (projectId: string, actor: string, ruleId: string, paused: boolean): Promise<WriteResult> =>
  obsWrite(`/v1/agent-triggers/${enc(ruleId)}/${paused ? "pause" : "resume"}`, { project_id: projectId, actor }, press(`rule:${ruleId}:on`, paused ? "Pause" : "Resume"));
export const deleteRule = (projectId: string, actor: string, ruleId: string): Promise<WriteResult> =>
  obsWrite(`/v1/agent-triggers/${enc(ruleId)}?project_id=${enc(projectId)}${actor ? `&actor=${enc(actor)}` : ""}`, {}, press(`rule:${ruleId}:rm`, "Remove"), "DELETE");
export const setRuleBudget = (projectId: string, ruleId: string, usdPerDay: number): Promise<WriteResult> =>
  obsWrite("/v1/budget-policies/scoped", { project_id: projectId, scope_type: "trigger", scope_id: ruleId,
    cap_usd_micros: Math.round(usdPerDay * 1_000_000), period: "day", hard_stop: true }, press(`budget:${ruleId}`, "Daily budget"));
export const removeRuleBudget = (projectId: string, policyId: string): Promise<WriteResult> =>
  obsWrite(`/v1/budget-policies/scoped/${enc(policyId)}?project_id=${enc(projectId)}`, {}, press(`budget:${policyId}:rm`, "Daily budget"), "DELETE");
export const revokePerson = (connectionId: string, linkId: string): Promise<WriteResult> =>
  obsWrite(`${conn(connectionId)}/people/${enc(linkId)}/revoke`, {}, press(`person:${linkId}`, "Revoke"));
