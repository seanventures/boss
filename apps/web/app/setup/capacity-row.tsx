"use client";

/**
 * Capacity: where model capacity comes from (was System Configuration › AI Capacity; operator
 * decision §11.2 moved it here, beside the Model row it overlaps).
 *
 *   Capacity   ● Active  anthropic_api   anthropic · key · available      On Model row  Replace ↗
 *              ● Blocked openai_gateway  openai · gateway                 [Store credential]
 *              ● Needs login claude_max  anthropic · plan                 [Start official session]
 *              ● Active  chatgpt_pro     openai · plan · connected        [Disconnect session]
 *              SESSIONS  claude_cli_login · Waiting on you · `claude login`  [Complete session]
 *              Broker skills  Skills ↗
 *
 * One source per line, every source the project has (the Model row shows only the connected
 * ones). A key that is already a Model row is replaced THERE (`Replace ↗` opens its menu) — no
 * second key form; a source with no key yet takes one here, into the secret backend you pick.
 * Official logins are started, completed (the session reference and the capacity it reports,
 * as fields, not raw JSON) and disconnected here. The data is read client-side from the API
 * (GET /v1/entitlements/sources, /v1/entitlement-connection-sessions); every write goes through
 * apiWrite, then the list and the board are read again.
 */
import React, { forwardRef, useCallback, useEffect, useState } from "react";

import { apiWrite } from "../../lib/api-write";
import { fetchSetup } from "../../lib/setup-client";
import { toast, useBoard } from "./parts";
import { Dot, Tip } from "./pill";
import { SetupRow } from "./setup-row";

// ── the wire, parsed ────────────────────────────────────────────────────────────────────────

type Obj = Record<string, unknown>;
const o = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const s = (v: unknown, d = "") => (typeof v === "string" ? v : d);
const list = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export type CapSource = {
  sourceId: string; provider: string; kind: string; effective: string; auth: string; connection: string;
  credentialPresent: boolean; capacity: string; models: string[]; modes: string[]; scopes: string[]; pooled: boolean;
};
export type CapSession = {
  id: string; sourceId: string; mode: string; status: string; scopes: string[]; login: string | null; action: string | null; at: string;
};

export function parseSources(w: unknown): CapSource[] {
  return (Array.isArray(o(w).items) ? (o(w).items as unknown[]) : []).map((x) => {
    const r = o(x);
    const cap = o(r.observed_capacity);
    return {
      sourceId: s(r.source_id), provider: s(r.provider), kind: s(r.source_kind), effective: s(r.effective_status, s(r.status)),
      auth: s(r.auth_method), connection: s(r.connection_status), credentialPresent: cap.credential_present === true,
      capacity: s(cap.capacity_state, "unknown"), models: list(r.supported_models),
      modes: list(o(r.connection_metadata).supported_connection_modes), scopes: list(r.oauth_scopes),
      pooled: r.multi_tenant_allowed === true,
    };
  }).filter((x) => x.sourceId);
}
export function parseSessions(w: unknown): CapSession[] {
  return (Array.isArray(o(w).items) ? (o(w).items as unknown[]) : []).map((x) => {
    const r = o(x);
    const ins = o(r.instructions);
    return {
      id: s(r.id), sourceId: s(r.source_id), mode: s(r.connection_mode), status: s(r.status), scopes: list(r.requested_scopes),
      login: s(r.login_command) || null, action: s(ins.operator_action) || null, at: s(r.created_at),
    };
  }).filter((x) => x.id);
}

const words = (v: string) => v.replace(/[_-]+/g, " ").replace(/^\w/, (c) => c.toUpperCase());
const KIND: Record<string, string> = { api_key: "key", gateway: "gateway", subscription: "plan", local_capacity: "local", local: "local" };
export function stateOf(src: CapSource): { label: string; tone: "green" | "amber" | "red" | "grey" } {
  if (src.effective === "active") return { label: "Active", tone: "green" };
  if (src.effective === "blocked") return { label: "Blocked", tone: "red" };
  if (src.effective === "needs_connection" || src.connection === "not_connected") return { label: "Needs login", tone: "amber" };
  return { label: words(src.effective || "unknown"), tone: "grey" };
}
const SESSION_WORD: Record<string, string> = {
  operator_action_required: "Waiting on you", pending: "Starting", completed: "Connected", expired: "Expired", failed: "Failed",
};
const takesKey = (src: CapSource) => src.auth === "api_key" || src.auth === "gateway_token";
const isLogin = (src: CapSource) => src.auth.includes("oauth");

async function post(path: string, body: Obj, label: string, scope: string): Promise<string | null> {
  try {
    const r = await apiWrite(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
                             { press: { scope, label } });
    if (r.ok) return null;
    const d = o(await r.json().catch(() => ({})));
    return s(d.detail) || s(d.error) || `HTTP ${r.status}`;
  } catch (e) {
    return e instanceof Error ? e.message : String(e);
  }
}

// ── one source ──────────────────────────────────────────────────────────────────────────────

const BACKENDS = [
  { id: "platform_secret_store", label: "Platform secret store" }, { id: "vault", label: "Vault" }, { id: "local_runner", label: "Local runner" },
];

function KeyForm({ src, onSave }: { src: CapSource; onSave: (value: string, backend: string) => Promise<boolean> }) {
  const [value, setValue] = useState("");
  const [backend, setBackend] = useState(BACKENDS[0].id);
  const [busy, setBusy] = useState(false);
  const label = src.credentialPresent ? "Rotate credential" : "Store credential";
  return (
    <form className="su-line su-cap-form" data-testid="setup-cap-key-form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!value.trim()) return;
            setBusy(true);
            const ok = await onSave(value.trim(), backend);
            setBusy(false);
            if (ok) setValue("");
          }}>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Key</span>
        <input type="password" autoComplete="new-password" value={value} aria-label="Key" placeholder="Never shown again"
               data-testid="setup-cap-key" onChange={(e) => setValue(e.target.value)} />
      </label>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Store in</span>
        <select value={backend} aria-label="Store in" data-testid="setup-cap-backend" onChange={(e) => setBackend(e.target.value)}>
          {BACKENDS.map((b) => <option key={b.id} value={b.id}>{b.label}</option>)}
        </select>
      </label>
      <Tip tip={src.pooled ? "Jobs get leases, never this value." : "Yours alone. This value is never pooled."} className="su-faint">
        {src.pooled ? "Shared" : "Single owner"}
      </Tip>
      <button type="submit" className="su-btn su-btn-sm su-btn-primary" disabled={busy || !value.trim()} data-testid="setup-cap-key-save">{label}</button>
    </form>
  );
}

function StartForm({ src, onStart }: { src: CapSource; onStart: (mode: string, scopes: string[]) => Promise<void> }) {
  const [mode, setMode] = useState(src.modes[0] ?? "");
  const [scopes, setScopes] = useState(src.scopes.join(", "));
  const [busy, setBusy] = useState(false);
  return (
    <form className="su-line su-cap-form" data-testid="setup-cap-start-form"
          onSubmit={async (e) => { e.preventDefault(); setBusy(true); await onStart(mode, scopes.split(/[\s,]+/).filter(Boolean)); setBusy(false); }}>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Login</span>
        <select value={mode} aria-label="Login" onChange={(e) => setMode(e.target.value)}>
          {src.modes.length ? src.modes.map((m) => <option key={m} value={m}>{words(m)}</option>) : <option value="">Official login</option>}
        </select>
      </label>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Scopes</span>
        <input value={scopes} aria-label="Scopes" placeholder="Comma separated" onChange={(e) => setScopes(e.target.value)} />
      </label>
      <button type="submit" className="su-btn su-btn-sm" disabled={busy} data-testid="setup-cap-start"
              title="Get the official login steps for this plan" data-tip="Get the official login steps for this plan">Start official session</button>
    </form>
  );
}

const CAPACITY_STATES = ["available", "limited", "exhausted", "unknown"];

/** `Complete session`: the reference the login produced, the scopes, and the capacity it
 *  reports — validated fields, assembled into observed_capacity here, never typed as JSON. */
export function completeBody(f: { ref: string; scopes: string; state: string; resetAt: string }): Obj | null {
  const ref = f.ref.trim();
  if (!ref || !CAPACITY_STATES.includes(f.state)) return null;
  const capacity: Obj = { capacity_state: f.state };
  if (f.resetAt) {
    const t = Date.parse(f.resetAt);
    if (!Number.isFinite(t)) return null;
    capacity.reset_at = new Date(t).toISOString();
  }
  return { connection_ref: ref, oauth_scopes: f.scopes.split(/[\s,]+/).filter(Boolean), observed_capacity: capacity };
}

function CompleteForm({ session, onDone }: { session: CapSession; onDone: (body: Obj) => Promise<void> }) {
  const [ref, setRef] = useState("");
  const [scopes, setScopes] = useState(session.scopes.join(", "));
  const [state, setState] = useState("available");
  const [resetAt, setResetAt] = useState("");
  const [busy, setBusy] = useState(false);
  const body = completeBody({ ref, scopes, state, resetAt });
  return (
    <form className="su-line su-cap-form" data-testid="setup-cap-complete-form"
          onSubmit={async (e) => { e.preventDefault(); if (!body) return; setBusy(true); await onDone(body); setBusy(false); }}>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Session ref</span>
        <input value={ref} aria-label="Session ref" placeholder="Secret or local ref" required data-testid="setup-cap-ref"
               onChange={(e) => setRef(e.target.value)} />
      </label>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Scopes</span>
        <input value={scopes} aria-label="Scopes" onChange={(e) => setScopes(e.target.value)} />
      </label>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Capacity</span>
        <select value={state} aria-label="Capacity" data-testid="setup-cap-state" onChange={(e) => setState(e.target.value)}>
          {CAPACITY_STATES.map((c) => <option key={c} value={c}>{words(c)}</option>)}
        </select>
      </label>
      <label className="su-field su-field-inline">
        <span className="su-field-label">Resets</span>
        <input type="datetime-local" value={resetAt} aria-label="Resets" onChange={(e) => setResetAt(e.target.value)} />
      </label>
      <button type="submit" className="su-btn su-btn-sm su-btn-primary" disabled={busy || !body} data-testid="setup-cap-complete">Complete session</button>
    </form>
  );
}

// ── the row ─────────────────────────────────────────────────────────────────────────────────

export const CapacityRow = forwardRef<HTMLElement, { actor?: string; focused: boolean }>(function CapacityRow({ actor, focused }, ref) {
  const { projectId, view, apply } = useBoard();
  const [sources, setSources] = useState<CapSource[] | null>(null);
  const [sessions, setSessions] = useState<CapSession[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const who = actor || "ceo";

  const load = useCallback(async () => {
    const pid = encodeURIComponent(projectId);
    try {
      const [a, b] = await Promise.all([
        fetch(`/v1/entitlements/sources?project_id=${pid}`, { cache: "no-store" }),
        fetch(`/v1/entitlement-connection-sessions?project_id=${pid}&limit=20`, { cache: "no-store" }),
      ]);
      if (!a.ok) throw new Error(`HTTP ${a.status}`);
      setSources(parseSources(await a.json()));
      setSessions(b.ok ? parseSessions(await b.json()) : []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSources((prev) => prev ?? []);
    }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  /** After a write: this list and the board (a stored key becomes a Model row). */
  const after = async (failed: string | null, done: string) => {
    if (failed) { toast(failed.length > 80 ? "Not saved" : failed, "error"); return false; }
    toast(done);
    setOpen(null);
    await load();
    try { apply(await fetchSetup(projectId)); } catch { /* the board keeps its view */ }
    return true;
  };
  const onBoard = new Set(view.models.map((m) => m.source_id));

  return (
    <SetupRow ref={ref} row="capacity" label="Capacity" focused={focused} mode="open"
              tip="Where model capacity comes from: keys, plans and official logins.">
      <div className="su-stack" data-testid="setup-entitlements">
        {sources === null ? <span className="su-faint">Loading…</span> : null}
        {error ? <Tip tip={error} className="su-faint">Can&apos;t load capacity</Tip> : null}
        {sources && !error && sources.length === 0 ? <span className="su-faint">No sources yet</span> : null}
        {(sources ?? []).map((src) => {
          const st = stateOf(src);
          const mine = sessions.filter((x) => x.sourceId === src.sourceId);
          const editing = open === src.sourceId;
          return (
            <div key={src.sourceId} className="su-cap" data-testid="setup-cap-source" data-source={src.sourceId}>
              <div className="su-line">
                <span className={`su-pill su-tone-${st.tone}`} role="status"><Dot tone={st.tone} />{st.label}</span>
                <span className="su-mono su-strong" data-user-text>{src.sourceId}</span>
                <span className="su-faint" data-user-text>{src.provider}</span>
                <span className="su-chip">{KIND[src.kind] ?? words(src.kind)}</span>
                {src.capacity !== "unknown" ? <Tip tip="The capacity this source last reported." className="su-faint">{words(src.capacity)}</Tip> : null}
                {src.models.length ? (
                  <Tip tip={src.models.join(", ")} className="su-faint su-mono">
                    {src.models[0]}{src.models.length > 1 ? ` +${src.models.length - 1}` : ""}
                  </Tip>
                ) : null}
                <span className="su-spacer" />
                {takesKey(src) && onBoard.has(src.sourceId) ? (
                  <>
                    <Tip tip="Its key is managed on the Model row above." className="su-chip">On Model row</Tip>
                    <a className="su-link" href={`?area=setup&source=${encodeURIComponent(src.sourceId)}`} data-testid="setup-cap-replace">Replace ↗</a>
                  </>
                ) : null}
                {takesKey(src) && !onBoard.has(src.sourceId) ? (
                  <button type="button" className="su-btn su-btn-sm" aria-expanded={editing} data-testid="setup-cap-key-open"
                          onClick={() => setOpen(editing ? null : src.sourceId)}>
                    {src.credentialPresent ? "Rotate credential" : "Store credential"}
                  </button>
                ) : null}
                {isLogin(src) && src.connection === "connected" ? (
                  <button type="button" className="su-btn su-btn-sm" data-testid="setup-cap-disconnect"
                          onClick={() => setDisconnecting(src.sourceId)}>Disconnect session</button>
                ) : null}
                {isLogin(src) && src.connection !== "connected" ? (
                  <button type="button" className="su-btn su-btn-sm" aria-expanded={editing} data-testid="setup-cap-start-open"
                          onClick={() => setOpen(editing ? null : src.sourceId)}>Start official session</button>
                ) : null}
              </div>
              {editing && takesKey(src) ? (
                <KeyForm src={src} onSave={async (value, backend) => after(
                  await post(`/v1/entitlements/sources/${encodeURIComponent(src.sourceId)}/credential`,
                             { project_id: projectId, value, backend, actor: who }, src.credentialPresent ? "Rotate credential" : "Store credential",
                             `setup:cap:${src.sourceId}`),
                  src.credentialPresent ? "Credential rotated" : "Credential stored")} />
              ) : null}
              {editing && isLogin(src) && src.connection !== "connected" ? (
                <StartForm src={src} onStart={async (mode, scopes) => { await after(
                  await post(`/v1/entitlements/sources/${encodeURIComponent(src.sourceId)}/connection-sessions`,
                             { project_id: projectId, connection_mode: mode || undefined, requested_scopes: scopes, actor: who },
                             "Start official session", `setup:cap:${src.sourceId}`),
                  "Session started"); }} />
              ) : null}
              {disconnecting === src.sourceId ? (
                <div className="su-confirm" role="alertdialog" data-testid="setup-cap-disconnect-confirm">
                  <strong>Disconnect {src.sourceId}?</strong>
                  <button type="button" className="su-btn su-btn-sm su-btn-danger" data-testid="setup-cap-disconnect-yes"
                          onClick={async () => {
                            setDisconnecting(null);
                            await after(await post(`/v1/entitlements/sources/${encodeURIComponent(src.sourceId)}/disconnect`,
                                                   { project_id: projectId, reason: "Disconnected in Repo and models.", actor: who },
                                                   "Disconnect session", `setup:cap:${src.sourceId}`), "Session disconnected");
                          }}>Disconnect</button>
                  <button type="button" className="su-link" onClick={() => setDisconnecting(null)}>Keep</button>
                </div>
              ) : null}
              {mine.length ? (
                <ul className="su-cap-sessions">
                  {mine.map((x) => (
                    <li key={x.id} className="su-stack" data-testid="setup-cap-session-row">
                      <span className="su-line">
                        <span className="su-strong">{words(x.mode || "login")}</span>
                        <Tip tip={x.action ?? `Requested ${new Date(x.at).toLocaleString()}`} className="su-chip">
                          {SESSION_WORD[x.status] ?? words(x.status)}
                        </Tip>
                        {x.login ? <code className="su-mono" data-user-text>{x.login}</code> : null}
                      </span>
                      {x.status === "operator_action_required" ? (
                        <CompleteForm session={x} onDone={async (body) => { await after(
                          await post(`/v1/entitlement-connection-sessions/${encodeURIComponent(x.id)}/complete`,
                                     { project_id: projectId, ...body, actor: who }, "Complete session", `setup:cap:${x.id}`),
                          "Session completed"); }} />
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          );
        })}
        <div className="su-line" data-testid="setup-entitlement-session">
          <Tip tip="Official logins started from a plan above." className="su-faint su-cap-label">Sessions</Tip>
          <span className="su-faint">
            {sessions.length ? `${sessions.length} · ${sessions.filter((x) => x.status === "operator_action_required").length} waiting` : "None"}
          </span>
          <span className="su-spacer" />
          <Tip tip="Broker and model skills live in Skills and MCP." className="su-faint">Broker skills</Tip>
          <a className="su-link" href="?area=skills" data-testid="setup-broker-skills-link">Skills ↗</a>
        </div>
      </div>
    </SetupRow>
  );
});
