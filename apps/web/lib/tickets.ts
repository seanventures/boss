/**
 * TICKET SYSTEMS IN THE CONSOLE (BOSSAPP-1047 Phase C, wireframes T1–T9). One client for the
 * routes in apps/api/app/routers/ticket_systems.py (connections, browse, read, resolve, waiting,
 * import, start-from-ticket) and ticket_sync.py (write-back timeline + retry, webhook, a person's
 * own "Connect with Atlassian", /v1/me/ticket-identities).
 *
 * Parsing is pure and defensive. A read answers `{ok, data}` or `{ok:false, error, code, denied}`
 * where `error` is always a plain sentence (the API's typed `{"detail":{"error","message"}}` says
 * its message; a bare code is turned into words here). Writes go through obsWrite (one press,
 * one effect, an Idempotency-Key). A token is sent once and never read back: the API answers
 * `last4` and a fingerprint only.
 */
import { obsWrite, type WriteResult } from "../app/observability/obs-write";
import { asReadiness, type Readiness } from "../app/workflow-readiness-ui";

type R = Record<string, unknown>;
const o = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);
const n = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const strs = (v: unknown) => a(v).map((x) => s(x)).filter(Boolean);
const enc = encodeURIComponent;

// ── shapes ───────────────────────────────────────────────────────────────────────────────────
export type TicketType = "bug" | "story" | "task" | "epic" | "other";
export type StatusCategory = "todo" | "doing" | "review" | "done" | "";
export type Mapping = { external_project: string; boss_project_id: string; default: boolean };
export type WriteBack = { comment: boolean; transition: boolean; link_pr: boolean };
export type StatusMap = { todo: string; doing: string; review: string; done: string };
export type PickupMode = "ask" | "auto_start";
export type InboundMode = "webhook" | "poll" | "off";
export type TicketSettings = {
  mappings: Mapping[]; write_back: WriteBack; status_map: StatusMap;
  pickup: { label: string; mode: PickupMode }; inbound: { mode: InboundMode; poll_minutes: number };
  project_keys: string[];
  /** Shared with every project of the org (off unless a person who manages it turned it on). */
  share_with_org: boolean;
};
export type Connection = {
  id: string; project_id: string; provider: string; label: string; icon: string;
  status: "connected" | "disconnected" | "not_connected" | "not_shared"; base_url: string; fields: Record<string, string>;
  account: string | null; last4: string | null; fingerprint: string | null; capabilities: Record<string, boolean>;
  settings: TicketSettings; managed_from: { connection_id: string; project_id: string } | null;
  /** "org": connected once from another project of the org (home_project), usable here too. */
  reach: "project" | "org" | "unshared"; home_project: HomeProject | null;
  /** The caller may manage it (integration:manage at its home): share, settings, test, disconnect. */
  can_manage: boolean;
  /** The org list only: this project can use it (its own, shared with the org, or mirrored here). */
  usable_here: boolean;
  /** The org list only: names of the BOSS projects its mappings point at. */
  project_names: Record<string, string>;
  mirrors: { connection_id: string; project_id: string; status: string }[];
  bindings: { total: number; paused: number };
  /** The connection's own OAuth app for "Connect with Atlassian" (null: the connector has none).
   *  Never the secret: whether one is stored, and the callback URL to paste into the app. */
  oauth_app: OAuthApp | null;
};
/** A connection's home project: where it was added, managed and shared from, and whom to ask. */
export type HomeProject = { id: string; name: string; owners: string[];
  /** A project the viewer may not read: unnamed ("another project", id ""), its owners the org's. */
  hidden?: boolean };
export type OAuthApp = { client_id: string; secret_set: boolean; from_host: boolean; callback_url: string };
/** Whether "Connect with Atlassian" can start: an app is set on the connection or on the host. */
export const oauthReady = (c: Connection | null): boolean =>
  c?.oauth_app != null && (c.oauth_app.client_id !== "" || c.oauth_app.from_host);
export type AuthField = { name: string; label: string; placeholder: string; secret: boolean; required: boolean; help: string };
export type CatalogItem = {
  provider: string; label: string; icon: string; description: string; docs_url: string;
  auth_fields: AuthField[]; oauth: boolean; base_url_field: string; capabilities: Record<string, boolean>;
  status_map_defaults: Partial<StatusMap>; webhook: boolean;
  /** "Share with the whole org" starts ticked: a service/bot token, not someone's own. */
  share_default: boolean;
};
export type InBoss = { work_item_id: string; run_id: string | null; project_id: string; binding_id: string; sync_status: string } | null;
export type Ticket = {
  provider: string; connection_id: string; key: string; id: string; url: string; title: string; type: TicketType; raw_type: string;
  status: string; status_category: StatusCategory; priority: string; labels: string[]; assignee: string;
  parent_key: string; children_count: number | null; updated_at: string;
  screen: { verdict: string; reasons: string[] }; untrusted: boolean; in_boss: InBoss;
  /** A full read only. */
  body: string; acceptance_criteria: string[]; comments: { author: string; at: string; body: string }[];
  attachments: { name: string; url: string }[];
  /** The BOSS project the ticket belongs to (its mapping, else its connection's home). `by`:
   *  "mapping" when a project-key mapping says so, "home" when it only fell back to the project
   *  the connection was added from — which says nothing about where THIS ticket goes. */
  maps_to: { project_id: string; name: string; by: "mapping" | "home" } | null;
  /** Its size as the ticket says it (labels, a "Size:" line, story points), and the workflow its
   *  type and size start on (engine ticket_intake.default_workflow, BOSSAPP-1334). */
  size: "small" | "medium" | "large" | null;
  suggested_workflow: string;
  /** "org" when the connection was added from another project (import names its system). */
  connection_reach: "project" | "org";
};
export type TypedError = { connection_id?: string; ref?: string; error: string; message: string;
  /** not_shared: the connection that has it, where it lives, whom to ask, and whether the caller may share it. */
  shared_from?: NotShared };
/** A connection of another project, not shared with this one: enough to say "Share it" or whom to ask. */
export type NotShared = { connection_id: string; label: string; home: HomeProject; can_manage: boolean };
export type ExternalProject = { key: string; name: string; id: string; url: string; open_count: number | null; epic_count: number | null };
export type SyncEntry = {
  receipt_id: string; at: string; action: string; op: string; label: string; summary: string;
  status: "succeeded" | "failed" | "in_progress" | string; acted_as: string; reason: string; retryable: boolean;
};
export type SyncTimeline = {
  work_item_id: string; ticket: { provider: string; key: string; url: string; connection_id: string } | null;
  sync_status: string; sync_error: string; last_synced_at: string; entries: SyncEntry[]; failed: number;
};
export type Identity = {
  credential_id: string; project_id: string; provider: string; connection_id: string; display_name: string;
  account: string; email: string; email_matches: boolean; needs_confirmation: boolean; status: string; linked_at: string;
  /** The person said this account is theirs (POST …/confirm), for the account shown now. */
  confirmed_by_person: boolean;
};
export type WebhookSecret = { url: string; secret: string; last4: string; rotated: boolean; note: string };

// ── parsers ──────────────────────────────────────────────────────────────────────────────────
const TYPES: TicketType[] = ["bug", "story", "task", "epic", "other"];
const CATS: StatusCategory[] = ["todo", "doing", "review", "done"];
const bool = (v: unknown, d = false) => (typeof v === "boolean" ? v : d);

export function parseSettings(v: unknown): TicketSettings {
  const x = o(v);
  const wb = o(x.write_back), sm = o(x.status_map), pk = o(x.pickup), ib = o(x.inbound);
  return {
    mappings: a(x.mappings).map((m) => ({ external_project: s(o(m).external_project), boss_project_id: s(o(m).boss_project_id), default: o(m).default === true }))
      .filter((m) => m.external_project && m.boss_project_id),
    write_back: { comment: bool(wb.comment, true), transition: bool(wb.transition, true), link_pr: bool(wb.link_pr, true) },
    status_map: { todo: s(sm.todo), doing: s(sm.doing), review: s(sm.review), done: s(sm.done) },
    pickup: { label: s(pk.label), mode: pk.mode === "auto_start" ? "auto_start" : "ask" },
    inbound: { mode: ib.mode === "poll" || ib.mode === "off" ? ib.mode : "webhook", poll_minutes: n(ib.poll_minutes) ?? 5 },
    project_keys: strs(x.project_keys),
    share_with_org: x.share_with_org === true,
  };
}

export function parseConnection(v: unknown): Connection {
  const x = o(v);
  const st = s(x.status);
  const caps = o(x.capabilities);
  const mf = x.managed_from ? o(x.managed_from) : null;
  return {
    id: s(x.id), project_id: s(x.project_id), provider: s(x.provider), label: s(x.label) || s(x.provider), icon: s(x.icon),
    status: st === "connected" || st === "disconnected" || st === "not_shared" ? st : "not_connected", base_url: s(x.base_url),
    fields: Object.fromEntries(Object.entries(o(x.fields)).map(([k, val]) => [k, s(val)])),
    account: s(x.account) || null, last4: s(x.last4) || null, fingerprint: s(x.fingerprint) || null,
    capabilities: Object.fromEntries(Object.entries(caps).map(([k, val]) => [k, val === true])),
    settings: parseSettings(x.settings),
    managed_from: mf && s(mf.connection_id) ? { connection_id: s(mf.connection_id), project_id: s(mf.project_id) } : null,
    mirrors: a(x.mirrors).map((m) => ({ connection_id: s(o(m).connection_id), project_id: s(o(m).project_id), status: s(o(m).status) })),
    reach: s(x.reach) === "org" ? "org" : s(x.reach) === "unshared" ? "unshared" : "project",
    home_project: s(o(x.home_project).id) || s(o(x.home_project).name)
      ? { id: s(o(x.home_project).id), name: s(o(x.home_project).name) || s(o(x.home_project).id),
          owners: strs(o(x.home_project).owners), hidden: o(x.home_project).hidden === true || !s(o(x.home_project).id) }
      : null,
    can_manage: x.can_manage === true,
    usable_here: x.usable_here !== false,
    project_names: Object.fromEntries(Object.entries(o(x.project_names)).map(([k, val]) => [k, s(val)])),
    bindings: { total: n(o(x.bindings).total) ?? 0, paused: n(o(x.bindings).paused) ?? 0 },
    oauth_app: x.oauth_app && typeof x.oauth_app === "object"
      ? { client_id: s(o(x.oauth_app).client_id), secret_set: o(x.oauth_app).secret_set === true,
          from_host: o(x.oauth_app).from_host === true, callback_url: s(o(x.oauth_app).callback_url) }
      : null,
  };
}

export function parseCatalogItem(v: unknown): CatalogItem {
  const x = o(v);
  return {
    provider: s(x.provider), label: s(x.label) || s(x.provider), icon: s(x.icon), description: s(x.description), docs_url: s(x.docs_url),
    auth_fields: a(x.auth_fields).map((f) => {
      const y = o(f);
      return { name: s(y.name), label: s(y.label) || s(y.name), placeholder: s(y.placeholder), secret: y.secret === true,
               required: y.required !== false, help: s(y.help) };
    }).filter((f) => f.name),
    oauth: a(x.auth_kinds).some((k) => o(k).kind === "oauth2"),
    share_default: x.share_default === true,
    base_url_field: s(x.base_url_field),
    capabilities: Object.fromEntries(Object.entries(o(x.capabilities)).map(([k, val]) => [k, val === true])),
    status_map_defaults: Object.fromEntries(Object.entries(o(x.status_map_defaults)).map(([k, val]) => [k, s(val)])) as Partial<StatusMap>,
    webhook: x.webhook === true,
  };
}

export function parseTicket(v: unknown): Ticket {
  const x = o(v);
  const sc = o(x.screen);
  const ib = x.in_boss ? o(x.in_boss) : null;
  const t = s(x.type) as TicketType;
  const c = s(x.status_category) as StatusCategory;
  return {
    provider: s(x.provider), connection_id: s(x.connection_id), key: s(x.key), id: s(x.id), url: s(x.url), title: s(x.title),
    type: TYPES.includes(t) ? t : "other", raw_type: s(x.raw_type), status: s(x.status), status_category: CATS.includes(c) ? c : "",
    priority: s(x.priority), labels: strs(x.labels), assignee: s(o(x.assignee).name) || s(x.assignee),
    parent_key: s(x.parent_key), children_count: n(x.children_count), updated_at: s(x.updated_at),
    screen: { verdict: s(sc.verdict), reasons: strs(sc.reasons) }, untrusted: x.untrusted === true || s(sc.verdict) === "suspicious",
    in_boss: ib && s(ib.work_item_id) ? { work_item_id: s(ib.work_item_id), run_id: s(ib.run_id) || null, project_id: s(ib.project_id),
                                         binding_id: s(ib.binding_id) || s(ib.id), sync_status: s(ib.sync_status) } : null,
    body: s(x.body), acceptance_criteria: strs(x.acceptance_criteria),
    comments: a(x.comments).map((m) => ({ author: s(o(m).author), at: s(o(m).at), body: s(o(m).body) })),
    attachments: a(x.attachments).map((m) => ({ name: s(o(m).name), url: s(o(m).url) })).filter((m) => m.name),
    connection_reach: s(x.connection_reach) === "org" ? "org" : "project",
    maps_to: s(o(x.maps_to).project_id) ? { project_id: s(o(x.maps_to).project_id), name: s(o(x.maps_to).name) || s(o(x.maps_to).project_id),
                                            by: s(o(x.maps_to).by) === "home" ? "home" : "mapping" } : null,
    size: s(x.size) === "small" || s(x.size) === "medium" || s(x.size) === "large" ? (s(x.size) as "small" | "medium" | "large") : null,
    suggested_workflow: s(x.suggested_workflow) || workflowFor(TYPES.includes(t) ? t : "other"),
  };
}

/** A `not_shared` refusal's "who has it" (the API's home_project, home_project_name, owners, can_manage). */
export function parseNotShared(v: unknown): NotShared | undefined {
  const e = o(v);
  if (s(e.error) !== "not_shared" || !(s(e.home_project) || s(e.home_project_name))) return undefined;
  return { connection_id: s(e.connection_id), label: s(e.label) || "The ticket system",
           home: { id: s(e.home_project), name: s(e.home_project_name) || s(e.home_project), owners: strs(e.owners),
                   hidden: !s(e.home_project) },
           can_manage: e.can_manage === true && Boolean(s(e.home_project)) };
}
const parseErrors = (v: unknown): TypedError[] => a(v).map((e) => ({
  connection_id: s(o(e).connection_id) || undefined, ref: s(o(e).ref) || undefined, error: s(o(e).error), message: s(o(e).message),
  shared_from: parseNotShared(e),
}));

export function parseTimeline(v: unknown): SyncTimeline {
  const x = o(v);
  const t = x.ticket ? o(x.ticket) : null;
  const err = o(x.sync_error);
  return {
    work_item_id: s(x.work_item_id),
    ticket: t && s(t.key) ? { provider: s(t.provider), key: s(t.key), url: s(t.url), connection_id: s(t.connection_id) } : null,
    sync_status: s(x.sync_status, "idle"), sync_error: s(err.message) || s(err.reason), last_synced_at: s(x.last_synced_at),
    entries: a(x.entries).map((e) => {
      const y = o(e);
      return { receipt_id: s(y.receipt_id), at: s(y.at), action: s(y.action), op: s(y.op), label: s(y.label) || s(y.op), summary: s(y.summary),
               status: s(y.status), acted_as: s(y.acted_as), reason: s(y.reason), retryable: y.retryable !== false };
    }),
    failed: n(x.failed) ?? 0,
  };
}

export const parseIdentities = (v: unknown): Identity[] => a(o(v).identities).map((i) => {
  const y = o(i);
  return { credential_id: s(y.credential_id), project_id: s(y.project_id), provider: s(y.provider), connection_id: s(y.connection_id),
           display_name: s(y.display_name), account: s(y.account), email: s(y.email), email_matches: y.email_matches === true,
           needs_confirmation: y.needs_confirmation === true, status: s(y.status), linked_at: s(y.linked_at),
           confirmed_by_person: y.confirmed_by_person === true };
});

// ── words ────────────────────────────────────────────────────────────────────────────────────
/** A typed error code → a plain sentence, when the API's own message is missing. */
const CODE_WORDS: Record<string, string> = {
  auth_failed: "The ticket system refused BOSS's sign-in. Test the connection or update its token.",
  not_found: "That ticket or project was not found.",
  rate_limited: "The ticket system asked BOSS to slow down. Try again in a minute.",
  host_not_allowed: "That address is not one BOSS may call for this ticket system.",
  capability_not_supported: "This ticket system cannot do that.",
  invalid_ref: "That is not a ticket key or link BOSS recognises.",
  invalid_settings: "Those settings were not accepted.",
  invalid_request: "That request was not complete.",
  conflict: "Something changed meanwhile. Reload and try again.",
  upstream_error: "The ticket system did not answer properly. Try again shortly.",
  needs_person_sign_in: "Connect your own account first (Account › Ticket accounts).",
  not_configured: "This install has no Atlassian app yet. Ask an admin to set one up.",
  no_public_origin: "Set BOSS's public address first (Setup › Advanced › Public URL).",
};
export const codeWords = (code: string, message = "") => message || CODE_WORDS[code] || "BOSS could not do that just now.";

export type Read<T> = { ok: true; data: T } | { ok: false; error: string; code: string; status: number; denied: boolean; missing: boolean;
  /** not_shared: the connection another project has, so the page can offer "Share it" or whom to ask. */
  sharedFrom?: NotShared };

/** A GET that always answers in words: the typed error's message, permission denied, or missing. */
export async function ticketRead<T>(path: string, parse: (v: unknown) => T): Promise<Read<T>> {
  try {
    const r = await fetch(path, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (r.ok) return { ok: true, data: parse(body) };
    const d = o(body).detail;
    const code = typeof d === "object" && d ? s(o(d).error) : "";
    const msg = typeof d === "string" ? d : s(o(d).message);
    const denied = r.status === 403;
    const missing = (r.status === 404 && !code) || r.status === 405;
    return { ok: false, code, status: r.status, denied, missing, sharedFrom: parseNotShared(d),
             error: denied ? (msg || "You don't have permission to see this. Ask an admin.")
               : missing ? "This BOSS does not have ticket systems yet." : codeWords(code, msg) || `BOSS answered ${r.status}.` };
  } catch (e) {
    return { ok: false, code: "network", status: 0, denied: false, missing: false, error: `BOSS could not be reached (${(e as Error)?.message ?? e}).` };
  }
}

/** "epic → Major feature": the workflow a ticket's type starts on (TICKET_API §3, start_plan). */
export const WORKFLOW_FOR_TYPE: Record<TicketType, string> = {
  epic: "major_feature", story: "minor_feature", task: "minor_feature", other: "minor_feature", bug: "fast_fix",
};
export const WORKFLOW_WORDS: Record<string, string> = { major_feature: "Major feature", minor_feature: "Minor feature", fast_fix: "Bug fix" };
export const START_WORKFLOWS = ["minor_feature", "major_feature", "fast_fix"] as const;
export const TYPE_WORDS: Record<TicketType, string> = { epic: "Epic", story: "Story", task: "Task", bug: "Bug", other: "Ticket" };
export const workflowFor = (t: TicketType) => WORKFLOW_FOR_TYPE[t] ?? "minor_feature";
/** The workflow a ticket starts on: the engine's pick from its type AND size, else its type's. */
export const ticketWorkflow = (t: Pick<Ticket, "type"> & { suggested_workflow?: string }) => t.suggested_workflow || workflowFor(t.type);
/** "Story · Medium": what the pick was made from. */
export const pickedFrom = (t: Pick<Ticket, "type" | "size">) =>
  `${TYPE_WORDS[t.type]}${t.size ? ` · ${t.size[0].toUpperCase()}${t.size.slice(1)}` : ""}`;
export const workflowWords = (key: string) => WORKFLOW_WORDS[key] ?? key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

/** "J", "P", "GH": the provider's monogram for the tiles and chips. */
export function monogram(provider: string): string {
  const p = provider.toLowerCase();
  if (p.startsWith("github")) return "GH";
  if (p === "azure_devops") return "AZ";
  return (p[0] ?? "?").toUpperCase();
}

/** One line for a connection's write-back ("Comments · status · PR link"). */
export function writesWords(w: WriteBack): string {
  const parts = [w.comment ? "Comments" : "", w.transition ? "status" : "", w.link_pr ? "PR link" : ""].filter(Boolean);
  if (!parts.length) return "Nothing";
  return parts.map((p, i) => (i === 0 ? p[0].toUpperCase() + p.slice(1) : p)).join(" · ");
}

/** One line for a connection's new-ticket rule ("Label “boss” → ask first"). */
export function pickupWords(st: TicketSettings): string {
  if (!st.pickup.label || st.inbound.mode === "off") return "Off";
  return `Label “${st.pickup.label}” → ${st.pickup.mode === "auto_start" ? "start" : "ask first"}`;
}

/** "Jira: In Progress ✓": a row's sync state in a few words. */
export function syncWords(label: string, status: string, syncStatus: string): string {
  const where = `${label}${status ? `: ${status}` : ""}`;
  if (syncStatus === "failed") return `${where} · write-back failed`;
  if (syncStatus === "paused") return `${where} · sync paused`;
  return status ? `${where} ✓` : where;
}

// ── reads ────────────────────────────────────────────────────────────────────────────────────
const conn = (id: string) => `/v1/ticket-systems/${enc(id)}`;

/**
 * Setup › Ticket systems FOR THIS PROJECT. A link without `project_id` lands on the org's own
 * project (page.tsx), so a person who followed "Connect one" from HelpDesk Laptop added Jira to
 * another project and then saw "No ticket system yet" (2026-11-15). Every link goes through here.
 */
export const ticketSetupHref = (projectId?: string) =>
  `?area=setup&focus=ticket_systems${projectId ? `&project_id=${enc(projectId)}` : ""}`;

/** A connection not shared here, from a project list entry (reach "unshared"). */
export const notSharedOf = (c: Connection): NotShared | null =>
  c.home_project ? { connection_id: c.id, label: c.label, home: c.home_project, can_manage: c.can_manage } : null;
/** Setup › Ticket systems › All with that connection's sharing open: one press from "Share it".
 *  Keeps who you are (`org`) and the project you are in (`project_id`). */
export function shareHref(currentSearch: string, projectId: string | undefined, connectionId: string): string {
  const next = new URLSearchParams();
  const now = new URLSearchParams(currentSearch);
  for (const key of ["org", "org_id"]) { const v = now.get(key); if (v) next.set(key, v); }
  next.set("area", "setup");
  next.set("focus", "ticket_systems");
  if (projectId) next.set("project_id", projectId);
  next.set("tk", "all");
  next.set("share", connectionId);
  return `?${next}`;
}
/** "Ask Param G, owner of BOSS dogfood bootstrap, to share it" — names when the API gave them. */
export function askOwnerWords(home: HomeProject): string {
  const names = home.owners.slice(0, 3);
  // A project the viewer may not read is never named: ask an org owner (their names are fine).
  if (home.hidden) return names.length ? `Ask an org owner (${names.join(" or ")}) to share it` : "Ask an org owner to share it";
  return names.length ? `Ask ${names.join(" or ")} (owner of ${home.name}) to share it`
    : `Ask an owner of ${home.name} to share it`;
}

/** What sharing a connection means, in the words the person agrees to. */
export const shareWords = (org: string, owner: string | null) =>
  `Everyone in ${org || "your organisation"} and their agents can read tickets through this connection using ${owner ? `${owner}'s` : "its"} token.`;
/** The organisation's name, for the sharing sentence (GET /v1/auth/options, public). */
export async function readOrgName(): Promise<string> {
  try {
    const r = await fetch("/v1/auth/options", { cache: "no-store" });
    return r.ok ? s(o(await r.json()).org_name) : "";
  } catch { return ""; }
}
export const readCatalog = () => ticketRead("/v1/ticket-systems/catalog", (v) => a(o(v).items).map(parseCatalogItem).filter((c) => c.provider));
/** Every connection this project can USE (its own, and ones shared with the whole org). */
export const readConnections = (projectId: string) =>
  ticketRead(`/v1/ticket-systems?project_id=${enc(projectId)}`, (v) => a(o(v).connections).map(parseConnection).filter((c) => c.reach !== "unshared"));
/** The same, plus other projects' connections NOT shared with this one (named only: Setup). */
export const readConnectionsAndHidden = (projectId: string) =>
  ticketRead(`/v1/ticket-systems?project_id=${enc(projectId)}`, (v) => a(o(v).connections).map(parseConnection));
/** The same list with the asking project's name ("New work in Northgate Gateway"). */
export const readTicketSetup = (projectId: string) =>
  ticketRead(`/v1/ticket-systems?project_id=${enc(projectId)}`, (v) => ({
    connections: a(o(v).connections).map(parseConnection), projectName: s(o(v).project_name) }));
/** Setup › Ticket systems › All: every connection of the org, wherever it was added. */
export const readOrgConnections = (projectId: string) =>
  ticketRead(`/v1/ticket-systems/org?project_id=${enc(projectId)}`, (v) => a(o(v).connections).map(parseConnection));
export const readExternalProjects = (connectionId: string, q = "") =>
  ticketRead(`${conn(connectionId)}/projects${q ? `?q=${enc(q)}` : ""}`, (v) => a(o(v).projects).map((p) => {
    const y = o(p);
    return { key: s(y.key), name: s(y.name), id: s(y.id), url: s(y.url), open_count: n(y.open_count), epic_count: n(y.epic_count) };
  }).filter((p) => p.key));
const tickets = (v: unknown) => a(o(v).tickets).map(parseTicket);
export const readEpics = (connectionId: string, projectKey: string) => ticketRead(`${conn(connectionId)}/projects/${enc(projectKey)}/epics`, tickets);
export const readUnparented = (connectionId: string, projectKey: string) => ticketRead(`${conn(connectionId)}/projects/${enc(projectKey)}/unparented`, tickets);
export const readChildren = (connectionId: string, key: string) => ticketRead(`${conn(connectionId)}/tickets/${enc(key)}/children`, tickets);
export const readTicket = (connectionId: string, key: string) =>
  ticketRead(`${conn(connectionId)}/tickets/${enc(key)}`, (v) => ({ ...parseTicket(o(v).ticket), connection_id: connectionId }));
export function searchTickets(connectionId: string, q: string, f: { project?: string; status?: string; type?: string } = {}) {
  const p = new URLSearchParams({ q });
  if (f.project) p.set("project", f.project);
  if (f.status) p.set("status", f.status);
  if (f.type) p.set("type", f.type);
  return ticketRead(`${conn(connectionId)}/search?${p}`, tickets);
}
export const readResolve = (projectId: string, text: string) =>
  ticketRead(`/v1/tickets/resolve?project_id=${enc(projectId)}&text=${enc(text.slice(0, 20_000))}`,
             (v) => ({ tickets: tickets(v), errors: parseErrors(o(v).errors), refs: a(o(v).refs).length,
                       notConnected: o(v).not_connected ? s(o(o(v).not_connected).next) || "Connect a ticket system first." : "" }));
export const readWaiting = (projectId: string) =>
  ticketRead(`/v1/projects/${enc(projectId)}/tickets/waiting`, (v) => ({ tickets: tickets(v), errors: parseErrors(o(v).errors) }));
export const readSync = (workItemId: string) => ticketRead(`/v1/work-items/${enc(workItemId)}/ticket-sync`, parseTimeline);
export const readIdentities = (projectId?: string) =>
  ticketRead(`/v1/me/ticket-identities${projectId ? `?project_id=${enc(projectId)}` : ""}`, parseIdentities);

// ── writes ───────────────────────────────────────────────────────────────────────────────────
const press = (scope: string, label: string) => ({ scope: `tickets:${scope}`, label });

export type ConnectInput = {
  provider: string; base_url: string; fields: Record<string, string>; secrets: Record<string, string>; settings?: Partial<Record<string, unknown>>;
};
export type ConnectAnswer = { connection: Connection; test: { ok: boolean; account: string; message: string; capabilities: Record<string, boolean>; project_keys: string[] } };
export const parseConnectAnswer = (v: unknown): ConnectAnswer => {
  const x = o(v), t = o(x.test);
  return { connection: parseConnection(x.connection),
           test: { ok: t.ok === true, account: s(t.account), message: s(t.message),
                   capabilities: Object.fromEntries(Object.entries(o(t.capabilities)).map(([k, val]) => [k, val === true])), project_keys: strs(t.project_keys) } };
};

/** Connect: BOSS tests first (nothing is stored when the test fails); the token goes to the vault. */
export const connectTicketSystem = (projectId: string, input: ConnectInput): Promise<WriteResult> =>
  obsWrite("/v1/ticket-systems", { project_id: projectId, ...input }, press("new", "Connect"));
export const testTicketSystem = (id: string): Promise<WriteResult> => obsWrite(`${conn(id)}/test`, {}, press(`${id}:test`, "Test"));
/** Change settings: only what is named changes (mappings, write_back, status_map, pickup, inbound). */
export const saveTicketSettings = (id: string, patch: Partial<Record<keyof TicketSettings | "oauth_app", unknown>>): Promise<WriteResult> =>
  obsWrite(conn(id), patch as R, press(`${id}:save`, "Save"), "PATCH");
export const disconnectTicketSystem = (id: string): Promise<WriteResult> => obsWrite(conn(id), {}, press(`${id}:off`, "Disconnect"), "DELETE");
/** Create or rotate the inbound webhook: the URL and secret come back ONCE. */
export const rotateTicketWebhook = (id: string): Promise<WriteResult> => obsWrite(`${conn(id)}/webhook`, {}, press(`${id}:hook`, "Webhook"));
/** "Connect with Atlassian": the POST twin answers {authorize_url} for a tab the console opens. */
export const startTicketOauth = (id: string): Promise<WriteResult> => obsWrite(`${conn(id)}/oauth/start/load`, {}, press(`${id}:oauth`, "Connect with Atlassian"));
export const disconnectTicketOauth = (id: string): Promise<WriteResult> => obsWrite(`${conn(id)}/oauth/me`, {}, press(`${id}:oauth-off`, "Disconnect"), "DELETE");
/** "It's mine": you confirm the ticket account you signed in as is yours (its email is not your
 *  BOSS email). Kept by the API on your own sign-in, audited; yours only. */
export const confirmTicketIdentity = (connectionId: string): Promise<WriteResult> =>
  obsWrite(`/v1/me/ticket-identities/${enc(connectionId)}/confirm`, {}, press(`confirm:${connectionId}`, "It's mine"));
/** Which connection an import goes through: this project's own by id; one added from another
 *  project of the org (reach "org") by its system, since a body id is confined to the project. */
export type ImportVia = string | { id: string; provider: string; reach?: string };
export const importVia = (via: ImportVia): R =>
  typeof via === "string" ? { connection_id: via } : via.reach === "org" ? { provider: via.provider } : { connection_id: via.id };
export const importTickets = (projectId: string, via: ImportVia, items: { key: string; with_children?: boolean }[]): Promise<WriteResult> =>
  obsWrite(`/v1/projects/${enc(projectId)}/tickets/import`, { ...importVia(via), items: items.map((i) => ({ key: i.key, with_children: Boolean(i.with_children) })) },
           press(`import:${projectId}`, "Import to Work"));
/** `profile` (BOSSAPP-1334): lean | balanced | strict, carried to the build; omitted = the project's default. */
export const startFromTicket = (workItemId: string, body: { project_id?: string; workflow_key?: string; start_anyway?: string[]; profile?: string | null } = {}): Promise<WriteResult> =>
  obsWrite(`/v1/work-items/${enc(workItemId)}/start-from-ticket`, body as R, press(`start:${workItemId}`, "Start"));
export const retryTicketSync = (workItemId: string): Promise<WriteResult> =>
  obsWrite(`/v1/work-items/${enc(workItemId)}/ticket-sync/retry`, {}, press(`retry:${workItemId}`, "Retry"));
/** "Include it" (approve) or "Next ticket" (reject) on a "the ticket changed" card. */
export const answerTicketChanged = (decisionId: string, include: boolean): Promise<WriteResult> =>
  obsWrite(`/v1/decisions/${enc(decisionId)}/${include ? "approve" : "reject"}`,
           { note: include ? "Include the new acceptance criteria in this build." : "Leave the change for the next ticket." },
           press(`changed:${decisionId}`, include ? "Include it" : "Next ticket"));

export const parseImport = (v: unknown) => {
  const x = o(v);
  const row = (r: unknown) => ({ key: s(o(r).key), work_item_id: s(o(r).work_item_id), parent_work_item_id: s(o(r).parent_work_item_id) || null });
  return { imported: a(x.imported).map(row), already_in_boss: a(x.already_in_boss).map(row) };
};
export const parseWebhook = (v: unknown): WebhookSecret => {
  const x = o(v);
  return { url: s(x.url), secret: s(x.secret), last4: s(x.last4), rotated: x.rotated === true, note: s(x.note) };
};

/**
 * Import then start, as one press (T3, T4, T5's Start): a ticket not yet in BOSS is imported
 * (a work item + binding), then started on the workflow its type calls for (or the one picked).
 */
export async function importAndStart(projectId: string, t: Pick<Ticket, "key" | "connection_id" | "in_boss" | "type" | "provider"> & { connection_reach?: string }, workflowKey?: string,
                                     startAnyway: readonly string[] = [], profile?: string | null,
): Promise<{ ok: boolean; error: string | null; workItemId: string; runId: string; note: string; readiness: Readiness | null }> {
  let workItemId = t.in_boss?.work_item_id ?? "";
  if (!workItemId) {
    const imp = await importTickets(projectId, { id: t.connection_id, provider: t.provider, reach: t.connection_reach }, [{ key: t.key, with_children: t.type === "epic" }]);
    if (!imp.ok) return { ok: false, error: imp.error, workItemId: "", runId: "", note: "", readiness: null };
    const done = parseImport(imp.body);
    workItemId = (done.imported.find((r) => r.key === t.key) ?? done.already_in_boss.find((r) => r.key === t.key) ?? done.imported[0])?.work_item_id ?? "";
    if (!workItemId) return { ok: false, error: `${t.key} was not imported.`, workItemId: "", runId: "", note: "", readiness: null };
  }
  const st = await startFromTicket(workItemId, { project_id: projectId, workflow_key: workflowKey || ticketWorkflow(t),
                                                 ...(startAnyway.length ? { start_anyway: [...startAnyway] } : {}),
                                                 ...(profile ? { profile } : {}) });
  if (!st.ok) return { ok: false, error: st.error, workItemId, runId: "", note: "", readiness: refusalReadiness(st.refusal) };
  const b = o(st.body);
  return { ok: true, error: null, workItemId, runId: s(b.run_id) || s(o(b.run).id), note: s(b.note), readiness: null };
}

/**
 * The project checklist a refused start carried (409 `{detail: {message, readiness}}`), or null.
 * A ticket start is refused like any other start when the project lacks a repo or a deploy
 * target; the console shows the checklist with "Start anyway" ticks, never only the sentence.
 */
export function refusalReadiness(refusal: unknown): Readiness | null {
  const detail = o(o(refusal).detail);
  return asReadiness(detail.readiness ?? o(refusal).readiness);
}

/** A pending "the ticket changed while BOSS was building" card (T6). */
export type ChangedCard = { id: string; question: string; added: string[] };

/**
 * The pending "ticket changed" card for a work item or run, or null. The decision inbox is a
 * writing GET (it reaps as it answers), so it is read through its POST twin `/load`
 * (lib/writing-get.ts), which carries the person's CSRF token.
 */
export async function loadTicketChangedCard(projectId: string, workItemId: string | null, runId?: string | null): Promise<Read<ChangedCard | null>> {
  const r = await obsWrite(`/v1/decisions/inbox/load?project_id=${enc(projectId)}&limit=100`, {}, press(`inbox:${projectId}`, "Read decisions"));
  if (!r.ok) {
    return { ok: false, code: "", status: r.status, denied: r.status === 403, missing: r.status === 404 || r.status === 405,
             error: r.error ?? "The decisions did not load." };
  }
  for (const it of a(o(r.body).items)) {
    const x = o(it), rec = o(x.recommendation);
    if (s(rec.action) !== "ticket_changed_mid_run" || !["pending", "delegated"].includes(s(x.status))) continue;
    const mine = (workItemId && s(rec.work_item_id) === workItemId) || (runId && (s(rec.run_workflow_id) === runId || s(rec.root_run_id) === runId));
    if (mine) return { ok: true, data: { id: s(x.id), question: s(x.question), added: strs(rec.added) } };
  }
  return { ok: true, data: null };
}
