"use client";

/**
 * Capacity: where model capacity comes from (was System Configuration › AI Capacity; operator
 * decision §11.2 moved it here, now under the Models heading).
 *
 * Source names open optional identity/model details. Status, reported capacity and actions
 * stay visible, as do every session and its completion controls.
 *
 * Connected, uncertain and attention sources stay in view. Positively unused sources are
 * offered through Add source, only after both reads prove absence (the Model row shows connected
 * ones). A key that is already a Model row is replaced THERE (`Replace key ↗` opens its menu) — no
 * second key form; a source with no key yet takes one here, into the secret backend you pick.
 * Official logins are started, completed (the session reference and the capacity it reports,
 * as fields, not raw JSON) and disconnected here. The data is read client-side from the API
 * (GET /v1/entitlements/sources, /v1/entitlement-connection-sessions); every write goes through
 * apiWrite, then the list and the board are read again.
 */
import { GatedButton } from "../../components/gated";
import { LockedPanel, lockedReason } from "../../components/locked-panel";
import { isLocked } from "../../lib/identity/read-gates";
import React, { forwardRef, useCallback, useEffect, useState } from "react";

import { apiWrite } from "../../lib/api-write";
import { fetchSetup } from "../../lib/setup-client";
import { toast, useBoard } from "./parts";
import { Dot, Tip } from "./pill";
import { useLocalTime } from "../../lib/local-time";

// ── the wire, parsed ────────────────────────────────────────────────────────────────────────

type Obj = Record<string, unknown>;
const o = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const s = (v: unknown, d = "") => (typeof v === "string" ? v : d);
const list = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export type CapSource = {
  sourceId: string; provider: string; kind: string; effective: string; auth: string; connection: string;
  credentialPresent: boolean; credentialAbsent: boolean; capacity: string; models: string[]; modes: string[]; scopes: string[]; pooled: boolean;
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
      // Seeded credential_ref/connection_ref can name an absent environment variable.
      credentialAbsent: cap.credential_present === false && !r.credential_secret_ref_id,
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
// Display names only: source IDs remain the identity for every control and request.
const SOURCE_NAMES: Record<string, string> = {
  openai_codex_chatgpt: "Codex (ChatGPT)", openai_api: "OpenAI API",
  anthropic_claude_code: "Claude Code", anthropic_api: "Anthropic API",
  typesafe_jev: "TypeSafe Jev", google_gemini_cli: "Gemini CLI", google_api: "Google API",
  cursor_cli: "Cursor CLI", cursor_api: "Cursor API", xai_grok_build: "Grok Build", xai_api: "xAI API",
  approved_local_model_host: "Local model", managed_cloud_model_gateway: "Managed gateway",
};
const sourceName = (id: string) => Object.hasOwn(SOURCE_NAMES, id) ? SOURCE_NAMES[id] : id;
const KIND: Record<string, string> = { api_key: "key", gateway: "gateway", subscription: "plan", local_capacity: "local", local: "local" };
export function stateOf(src: CapSource): { label: string; tone: "green" | "amber" | "red" | "grey" } {
  if (src.effective === "active") return { label: "Active", tone: "green" };
  if (src.effective === "blocked") return { label: "Blocked", tone: "red" };
  // A disconnected flag must not replace an unknown, unsupported or failed verdict.
  const unused = ["declared", "needs_connection", "missing_credentials", "missing_oauth_connection"].includes(src.effective);
  if (unused && src.connection === "not_connected") {
    if (takesKey(src) && src.credentialAbsent) return { label: "Needs key", tone: "amber" };
    if (isLogin(src)) return { label: "Needs sign-in", tone: "amber" };
    if (src.auth === "local_host") return { label: "Not configured", tone: "amber" };
    return { label: "Not connected", tone: "amber" };
  }
  if (unused && ["failed", "expired", "revoked"].includes(src.connection)) {
    return { label: words(src.connection), tone: src.connection === "failed" ? "red" : "amber" };
  }
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
  const label = src.credentialPresent ? "Replace key" : "Save key";
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
      <GatedButton permission="entitlement:manage" type="submit" className="su-btn su-btn-sm su-btn-primary" disabled={busy || !value.trim()} data-testid="setup-cap-key-save">{label}</GatedButton>
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
      <GatedButton permission="entitlement:manage" type="submit" className="su-btn su-btn-sm" disabled={busy} data-testid="setup-cap-start"
              title="Get the official login steps for this plan" data-tip="Get the official login steps for this plan">Get sign-in steps</GatedButton>
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
      <GatedButton permission="entitlement:manage" type="submit" className="su-btn su-btn-sm su-btn-primary" disabled={busy || !body} data-testid="setup-cap-complete">Complete session</GatedButton>
    </form>
  );
}

// ── the row ─────────────────────────────────────────────────────────────────────────────────

export const CapacityRow = forwardRef<HTMLElement, { actor?: string; focused: boolean }>(function CapacityRow({ actor, focused }, ref) {
  const lt = useLocalTime();
  const { projectId, view, apply } = useBoard();
  const [sources, setSources] = useState<CapSource[] | null>(null);
  const [sessions, setSessions] = useState<CapSession[]>([]);
  const [readsComplete, setReadsComplete] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const [edited, setEdited] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [locked, setLocked] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const who = actor || "ceo";

  const load = useCallback(async () => {
    const pid = encodeURIComponent(projectId);
    setReadsComplete(false);
    try {
      const [a, b] = await Promise.all([
        fetch(`/v1/entitlements/sources?project_id=${pid}`, { cache: "no-store" }),
        fetch(`/v1/entitlement-connection-sessions?project_id=${pid}&limit=20`, { cache: "no-store" }).catch(() => null),
      ]);
      if (isLocked(a)) { setLocked(await lockedReason(a)); setSources([]); setError(null); return; }
      if (!a.ok) throw new Error(`HTTP ${a.status}`);
      setLocked(null);
      const sourceWire = await a.json();
      const parsed = parseSources(sourceWire);
      setSources(parsed);
      const sourceItems = o(sourceWire).items;
      const sourcesKnown = Array.isArray(sourceItems) && parsed.length === sourceItems.length;
      try {
        if (!b) throw new Error("Not readable now");
        if (!b.ok) throw new Error(`HTTP ${b.status}`);
        const sessionWire = await b.json();
        const items = o(sessionWire).items;
        const parsedSessions = parseSessions(sessionWire);
        if (!Array.isArray(items) || parsedSessions.length !== items.length
            || parsedSessions.some((x) => !x.sourceId)) throw new Error("Not reported");
        setSessions(parsedSessions);
        setSessionError(null);
        // The API returns at most 20 sessions. A full page cannot prove an older session absent.
        setReadsComplete(sourcesKnown && items.length < 20);
      } catch (e) {
        setSessionError(e instanceof Error ? e.message : String(e));
      }
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
  const confirmedUnused = (src: CapSource) => readsComplete && !error && !locked && src.credentialAbsent
    && src.connection === "not_connected"
    && ["declared", "needs_connection", "missing_credentials", "missing_oauth_connection"].includes(src.effective)
    && src.capacity === "unknown" && (takesKey(src) || isLogin(src))
    && !onBoard.has(src.sourceId) && !sessions.some((x) => x.sourceId === src.sourceId);
  const unused = (src: CapSource) => confirmedUnused(src) && open !== src.sourceId && disconnecting !== src.sourceId && !edited.has(src.sourceId);
  const choices = (sources ?? []).filter(confirmedUnused);
  const shown = (sources ?? []).filter((src) => !unused(src) || selected === src.sourceId);
  const edit = (id: string, editing: boolean) => {
    setEdited((prev) => new Set(prev).add(id));
    setOpen(editing ? null : id);
  };

  return (
    <section ref={ref} className="su-model-sources" id="su-capacity" data-testid="su-row-capacity"
             data-focused={focused || undefined} aria-label="Source connections">
      <div className="su-stack" data-testid="setup-entitlements">
        {sources === null ? <span className="su-faint">Loading…</span> : null}
        {locked ? <LockedPanel inline reason={locked} testId="setup-entitlements-locked" /> : null}
        {error ? <Tip tip={error} className="su-faint">Can&apos;t load model sources</Tip> : null}
        {sources && !error && !locked && sources.length === 0 ? <span className="su-faint">No sources yet</span> : null}
        {choices.length ? (
          <details className="su-add-source" data-testid="setup-cap-add">
            <summary>Add source</summary>
            <label className="su-field">
              <span className="su-field-label">Source</span>
              <select value={choices.some((src) => src.sourceId === selected) ? selected : ""} aria-label="Source" data-testid="setup-cap-chooser"
                      onChange={(e) => setSelected(e.target.value)}>
                <option value="">Choose a source</option>
                {choices.map((src) => <option key={src.sourceId} value={src.sourceId}>{sourceName(src.sourceId)}</option>)}
              </select>
            </label>
          </details>
        ) : null}
        {shown.map((src) => {
          const st = stateOf(src);
          const mine = sessions.filter((x) => x.sourceId === src.sourceId);
          const editing = open === src.sourceId;
          return (
            <div key={src.sourceId} className="su-cap" data-testid="setup-cap-source" data-source={src.sourceId}>
              <div className="su-line su-cap-head">
                <details className="mr-criteria" style={{ minWidth: 0, maxWidth: "100%" }} data-testid="setup-cap-details">
                  <summary style={{ maxWidth: "100%", overflowWrap: "anywhere" }}><span data-user-text>{sourceName(src.sourceId)}</span><span className="su-sr"> details</span></summary>
                  <div className="su-stack su-faint" style={{ overflowWrap: "anywhere" }}>
                    <span>Source ID: <span className="su-mono" data-user-text>{src.sourceId}</span></span>
                    <span>Provider: <span data-user-text>{src.provider}</span></span>
                    <span>Type: <span data-user-text>{KIND[src.kind] ?? words(src.kind)}</span></span>
                    {src.models.length ? <span>Models: <span className="su-mono" data-user-text>{src.models.join(", ")}</span></span> : null}
                  </div>
                </details>
                <span className={`su-pill su-tone-${st.tone}`} role="status"
                      data-connection-summary={["Needs key", "Needs sign-in", "Not configured", "Not connected"].includes(st.label) || undefined}>
                  <Dot tone={st.tone} />{st.label}
                </span>
                {src.capacity !== "unknown" ? <Tip tip="The capacity this source last reported." className="su-faint">{words(src.capacity)}</Tip> : null}
                <span className="su-spacer" />
                {takesKey(src) && onBoard.has(src.sourceId) ? (
                  <a className="su-link" href={`?area=setup&source=${encodeURIComponent(src.sourceId)}`} data-testid="setup-cap-replace"
                     title="Replace its key on the Model row">Replace key ↗</a>
                ) : null}
                {takesKey(src) && !onBoard.has(src.sourceId) ? (
                  <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-sm" aria-expanded={editing} data-testid="setup-cap-key-open"
                          onClick={() => edit(src.sourceId, editing)}>
                    {src.credentialPresent ? "Replace key" : "Add key"}
                  </GatedButton>
                ) : null}
                {isLogin(src) && src.connection === "connected" ? (
                  <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-sm" data-testid="setup-cap-disconnect"
                          onClick={() => setDisconnecting(src.sourceId)}>Disconnect session</GatedButton>
                ) : null}
                {isLogin(src) && src.connection !== "connected" ? (
                  <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-sm" aria-expanded={editing} data-testid="setup-cap-start-open"
                          onClick={() => edit(src.sourceId, editing)}>Sign in</GatedButton>
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
                  <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-sm su-btn-danger" data-testid="setup-cap-disconnect-yes"
                          onClick={async () => {
                            setDisconnecting(null);
                            await after(await post(`/v1/entitlements/sources/${encodeURIComponent(src.sourceId)}/disconnect`,
                                                   { project_id: projectId, reason: "Disconnected in Repo and models.", actor: who },
                                                   "Disconnect session", `setup:cap:${src.sourceId}`), "Session disconnected");
                          }}>Disconnect</GatedButton>
                  <button type="button" className="su-link" onClick={() => setDisconnecting(null)}>Keep</button>
                </div>
              ) : null}
              {mine.length ? (
                <ul className="su-cap-sessions">
                  {mine.map((x) => (
                    <li key={x.id} className="su-stack" data-testid="setup-cap-session-row">
                      <span className="su-line">
                        <span className="su-strong">{words(x.mode || "login")}</span>
                        <Tip tip={x.action ?? `Requested ${lt.full(x.at)}`} className="su-chip">
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
            {sessionError ? <Tip tip={sessionError}>Can&apos;t load sessions</Tip>
              : sessions.length ? `${sessions.length} · ${sessions.filter((x) => x.status === "operator_action_required").length} waiting` : "None"}
          </span>
          <span className="su-spacer" />
          <a className="su-link" href="?area=skills" data-testid="setup-broker-skills-link">Skills ↗</a>
        </div>
      </div>
    </section>
  );
});
