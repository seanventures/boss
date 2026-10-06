/**
 * The audit filter: the API's query parameters (IDENTITY.md §10 GET /v1/identity/audit), and the
 * search box's tokens (`outcome:denied,failed`, `grant:dlg_…`, `actor:usr_…`, `chain:Sam`,
 * `jti:rt_7R`) read into them. Isomorphic: the server page and the client screen share it.
 */
export type AuditFilters = { q: string; outcome: string; actor: string; grant: string };

export const EMPTY_FILTERS: AuditFilters = { q: "", outcome: "", actor: "", grant: "" };

export function queryFrom(f: AuditFilters): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v) p.set(k, v);
  return p.toString();
}

/** Split typed search into filters. `chain:<name>` is resolved by the caller (names → ids). */
export function parseSearch(text: string, resolveName: (name: string) => string | null): AuditFilters {
  const out: AuditFilters = { ...EMPTY_FILTERS };
  const words: string[] = [];
  for (const raw of text.trim().split(/\s+/).filter(Boolean)) {
    const m = /^(outcome|grant|actor|chain|jti):(.+)$/i.exec(raw);
    if (!m) { words.push(raw); continue; }
    const key = m[1].toLowerCase();
    const value = m[2];
    if (key === "outcome") out.outcome = value.toLowerCase().replace(/success/g, "ok");
    else if (key === "grant") out.grant = value;
    else if (key === "actor") out.actor = value;
    else if (key === "chain") {
      const id = resolveName(value);
      if (id) out.actor = id; else words.push(value);
    } else words.push(value);
  }
  out.q = words.join(" ");
  return out;
}

/** The search box text that shows these filters back. */
export function searchText(f: AuditFilters, nameOf: (id: string) => string | null): string {
  const parts: string[] = [];
  if (f.actor) parts.push(`chain:${nameOf(f.actor) ?? f.actor}`);
  if (f.outcome) parts.push(`outcome:${f.outcome}`);
  if (f.grant) parts.push(`grant:${f.grant}`);
  if (f.q) parts.push(f.q);
  return parts.join(" ");
}

export type EventGroup = "all" | "signin" | "sessions" | "credentials" | "admin" | "keys" | "agents" | "authz" | "policy";
export const EVENT_GROUPS: { id: EventGroup; label: string; prefixes: string[] }[] = [
  { id: "all", label: "Event: all", prefixes: [] },
  { id: "signin", label: "Sign-in", prefixes: ["auth."] },
  { id: "sessions", label: "Sessions", prefixes: ["session."] },
  { id: "credentials", label: "Credentials", prefixes: ["password.", "mfa.", "recovery_code", "passkey."] },
  { id: "admin", label: "Admin", prefixes: ["user.", "group.", "access_role.", "role_binding.", "owner."] },
  { id: "keys", label: "Keys", prefixes: ["api_key."] },
  { id: "agents", label: "Agents", prefixes: ["agent.", "delegation.", "token.", "credential.", "egress."] },
  { id: "authz", label: "Authorization", prefixes: ["authz."] },
  { id: "policy", label: "Policy and system", prefixes: ["policy.", "bootstrap.", "legacy.", "audit.", "sso.", "scim.", "vault."] },
];

export function inGroup(eventType: string, group: EventGroup): boolean {
  if (group === "all") return true;
  return (EVENT_GROUPS.find((g) => g.id === group)?.prefixes ?? []).some((p) => eventType.startsWith(p));
}

/** The screen's dropdowns, as the API filters them (GET /v1/identity/audit `kind`, `event`, `since`). */
export type AuditView = { group: EventGroup; kind: "all" | "people" | "agents" | "keys" | "system"; rangeMs: number };

const KIND_PARAM: Partial<Record<AuditView["kind"], string>> = { people: "user", agents: "agent", system: "system" };

/**
 * The query the API is asked, filters plus what it can filter for the dropdowns: a group that is
 * one prefix becomes `event=<prefix>*`, a kind other than "keys" becomes `kind=`, a time range
 * becomes `since=`. What the API cannot express (multi-prefix groups, "keys") is still filtered
 * on screen. `cursor` asks for the next (older) page.
 */
export function apiQuery(f: AuditFilters, view: AuditView, now: number, cursor: string | null = null): string {
  const p = new URLSearchParams(queryFrom(f));
  const prefixes = EVENT_GROUPS.find((g) => g.id === view.group)?.prefixes ?? [];
  if (prefixes.length === 1 && prefixes[0].endsWith(".")) p.set("event", `${prefixes[0]}*`);
  const kind = KIND_PARAM[view.kind];
  if (kind) p.set("kind", kind);
  if (Number.isFinite(view.rangeMs)) p.set("since", new Date(now - view.rangeMs).toISOString());
  if (cursor) p.set("cursor", cursor);
  return p.toString();
}
