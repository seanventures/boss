"use client";

/**
 * The Models list: ONE row per model source — a Models row (the server's last real proof) joined
 * to its source record — each a single line: name, pill, one plain sentence, one primary action,
 * then Details (what it is, in words; the ids in a tooltip) and ⋯ (everything else).
 *
 * Connected, uncertain and attention sources stay in view. Sources proven unused are offered in
 * the Add a model drawer under "More sources", only after both reads prove absence; a picked one
 * appears here with its key or sign-in form open. A key that is a Models row is replaced through
 * the same drawer (no second key form); a source with no key takes one here, into the secret
 * backend you pick. Official logins are started, completed (the session reference and the
 * capacity it reports, as fields, not raw JSON) and disconnected here. The data is read from the
 * API (GET /v1/entitlements/sources, /v1/entitlement-connection-sessions) whenever the board's
 * view changes; every write goes through apiWrite, then the board is read again.
 */
import { GatedButton } from "../../components/gated";
import { LockedPanel, lockedReason } from "../../components/locked-panel";
import { isLocked } from "../../lib/identity/read-gates";
import React, { forwardRef, useCallback, useEffect, useState } from "react";

import { CaretDown } from "@phosphor-icons/react";

import { apiWrite } from "../../lib/api-write";
import { fetchSetup, SetupFailure, setupWrite } from "../../lib/setup-client";
import { ageLabel } from "../../lib/setup-stepper";
import type { ModelEntry, Pill as PillT, SetupView, Tone } from "../../lib/setup-types";
import { useLocalTime } from "../../lib/local-time";
import { needsSentence, subscriptionNext } from "./model-status";
import { DefaultModel, KIND, signsIn, usedForLabel, usedForTip, useModelsSection } from "./models-row";
import { AskBoss, RowMenu, toast, useBoard, type MenuItem } from "./parts";
import { Dot, Out, Tip } from "./pill";

// ── the wire, parsed ────────────────────────────────────────────────────────────────────────

type Obj = Record<string, unknown>;
const o = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const s = (v: unknown, d = "") => (typeof v === "string" ? v : d);
const list = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export type CapSource = {
  sourceId: string; provider: string; kind: string; effective: string; auth: string; connection: string;
  credentialPresent: boolean; credentialAbsent: boolean; capacity: string; models: string[]; modes: string[]; scopes: string[]; pooled: boolean;
  /** Anthropic rows only: which Anthropic credential `claude` runs on (never a value). */
  claudeActive?: string | null;
  /** The Models row's own pill for this source, from its last real proof (BOSSAPP-769). When
   *  present it IS the badge: a failed proof never reads "Active · Available". */
  pill?: SourcePill | null;
  /** The subscription BOSS signs in INSIDE the server for this source (codex | claude_code |
   *  grok), from the API — its Sign in runs that login, never the official-session form. */
  signInProvider?: string | null;
};
export type SourcePill = { state: string; label: string; tone: Tone; checked_at: string | null };
const TONES: readonly Tone[] = ["green", "amber", "red", "grey", "busy"];
function parsePill(v: unknown): SourcePill | null {
  const p = o(v);
  const label = s(p.label);
  const tone = TONES.find((t) => t === p.tone);
  if (!label || !tone) return null;
  return { state: s(p.state), label, tone, checked_at: s(p.checked_at) || null };
}
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
      claudeActive: s(o(r.claude_credential).active) || null,
      pill: parsePill(r.pill),
      signInProvider: s(r.sign_in_provider) || null,
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
/** The badge: the source's proven pill when the API sends one, else its connection record. */
export function badgeOf(src: CapSource): { label: string; tone: Tone; proven: boolean } {
  if (src.pill) return { label: src.pill.label, tone: src.pill.tone, proven: true };
  return { ...stateOf(src), proven: false };
}
/** A subscription BOSS signs in inside the server, whose login is missing or refused: its row
 *  offers Sign in (a fresh in-container login — the stale session is signed out first). */
export function offersSignIn(src: CapSource): boolean {
  if (!src.signInProvider) return false;
  const state = src.pill?.state ?? "";
  if (["login_expired", "key_rejected", "needs_sign_in", "not_checked", "legacy_token"].includes(state)) return true;
  if (["login_rejected", "key_rejected"].includes(src.effective)) return true;
  return src.connection !== "connected";
}
export function stateOf(src: CapSource): { label: string; tone: "green" | "amber" | "red" | "grey" } {
  // A stored key reads as what it is: connected. The key itself is never shown.
  if (src.effective === "active" && takesKey(src) && src.connection === "connected") return { label: "Connected", tone: "green" };
  if (src.effective === "active") return { label: "Active", tone: "green" };
  if (src.effective === "blocked") return { label: "Blocked", tone: "red" };
  // The last proof refused this source's login or key (the API's one status): red, never Active.
  if (src.effective === "login_rejected") return { label: "Login expired", tone: "red" };
  if (src.effective === "key_rejected") return { label: "Key rejected", tone: "red" };
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
function takesKey(src: CapSource) { return src.auth === "api_key" || src.auth === "gateway_token"; }

/** Which Anthropic credential runs Claude (BOSSAPP-530), said on the two rows it concerns. */
const ON_KEY = ["stored_key", "env_key"];
export function claudeCopy(src: CapSource): { line: string; tip: string; billing?: string } | null {
  if (!src.claudeActive) return null;
  const onKey = ON_KEY.includes(src.claudeActive);
  if (src.sourceId === "anthropic_api") {
    return onKey ? { line: "Claude runs on this key.", billing: "Billed to your API account",
                     tip: "Claude runs on this key. Each call is billed to your API account." } : null;
  }
  if (src.sourceId === "anthropic_claude_code") {
    const line = onKey ? "Claude runs on the API key." : src.claudeActive === "subscription"
      ? "Claude runs on this subscription." : "Claude runs on nothing yet.";
    return { line, tip: "Subscription first, then the Anthropic API key. Disconnect to use the key." };
  }
  return null;
}
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

// ── one row ─────────────────────────────────────────────────────────────────────────────────

/** "claude-sonnet" → "Claude Sonnet": model ids in words (the ids stay in the tooltip). */
const modelWords = (id: string) => id.split(/[-_]+/).filter(Boolean)
  .map((w) => (/^\d/.test(w) ? w : w[0].toUpperCase() + w.slice(1))).join(" ");
const KIND_WORDS: Record<string, string> = {
  api_key: "API key", gateway: "Gateway", subscription: "Subscription", official_cli: "Subscription",
  official_api: "API key", official_coding_agent: "Coding agent", approved_local_model: "Local",
  local_capacity: "Local", local: "Local", managed_cloud_model_gateway: "Gateway", api: "API key",
};
const PROVIDER_WORDS: Record<string, string> = {
  anthropic: "Anthropic", openai: "OpenAI", google: "Google", xai: "xAI", cursor: "Cursor",
  local: "Your hardware", boss: "BOSS", claude_code: "Anthropic", codex: "OpenAI", grok: "xAI",
  ollama: "Ollama", openrouter: "OpenRouter", typesafe: "TypeSafe",
};

type Item = { id: string; entry?: ModelEntry; src?: CapSource };

type SourceActions = {
  who: string;
  /** After a write: toast, then the board is re-read (the list follows the board). */
  after: (failed: string | null, done: string) => Promise<boolean>;
};

function SourceRow({ item, sessions, onBoard, formOpen, onForm, actions }: {
  item: Item; sessions: CapSession[]; onBoard: boolean; formOpen: boolean;
  onForm: (open: boolean) => void; actions: SourceActions;
}) {
  const lt = useLocalTime();
  const { projectId, view, apply, checking, setChecking } = useBoard();
  const section = useModelsSection();
  const { id, entry, src } = item;
  const [details, setDetails] = useState(false);
  const [picking, setPicking] = useState(false);
  const [removing, setRemoving] = useState<string[] | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);

  const checkKey = `model:${id}`;
  const busy = checking.has(checkKey) || checking.has("all");
  const signInProvider = entry && signsIn(entry) ? entry.provider : src?.signInProvider ?? null;
  const editing = section.editing === id;
  const name = entry?.label ?? sourceName(id);
  const provider = entry?.provider ?? src?.provider ?? "";

  // THE PILL: the Models row's (the server's last real proof) when it has one, else the
  // source's proven pill, else its connection record. Checking/editing replace it while they run.
  const serverPill: PillT | null = entry ? entry.pill : null;
  const badge = src ? badgeOf(src) : null;
  const pill: PillT = busy ? { state: "checking", tone: "busy", label: "Checking…", tip: "Asking one tiny question. Budgeted." }
    : editing && signInProvider ? { state: "editing", tone: "grey", label: "Signing in…", tip: "Signing in again. The old check no longer applies." }
    : editing ? { state: "editing", tone: "grey", label: "Editing…", tip: "Testing a new key. The old check no longer applies." }
    : serverPill ?? { state: src?.pill?.state ?? "", tone: badge!.tone, label: badge!.label,
                      tip: src?.pill ? (src.pill.checked_at ? `Checked ${lt.full(src.pill.checked_at)}.` : "Not checked yet.")
                                     : "From the source's connection record." };
  const red = pill.tone === "red";
  // A bare "Working" that hasn't been proven in a while is a stale green: the pill stays green
  // (nothing failed) but the row still owes a Test action and an honest "checked Xh ago", same
  // as an amber/red row — a login can die silently between checks.
  const checkedAt = serverPill?.at ?? src?.pill?.checked_at ?? null;
  const checkedAgeMs = checkedAt ? Date.now() - Date.parse(checkedAt) : null;
  const staleGreen = pill.tone === "green" && checkedAgeMs != null && checkedAgeMs > 2 * 3600 * 1000;
  const needs = red || pill.tone === "amber" || staleGreen;

  // ONE PLAIN SENTENCE. A row that needs something says what (the server's full fix in the
  // tooltip); a working one says what it does; Claude's two rows say which one Claude runs on.
  const said = src ? claudeCopy(src) : null;
  let sentence: { text: string; tip: string; testid: string } | null = null;
  if (busy || editing) sentence = null;
  else if (serverPill && needs && staleGreen && !red && pill.tone !== "amber") {
    sentence = { text: checkedAt ? `checked ${ageLabel(checkedAt)} ago` : "Not checked yet.",
                tip: serverPill.tip, testid: "su-model-said" };
  }
  else if (serverPill && needs) sentence = { text: needsSentence(serverPill), tip: serverPill.tip, testid: "su-model-said" };
  else if (said) sentence = { text: said.line, tip: said.tip, testid: "setup-cap-claude" };
  else if (entry && entry.used_for.length) sentence = { text: `Used for ${usedForLabel(entry.used_for)}`, tip: usedForTip(entry.used_for), testid: "su-model-used" };
  else if (entry) sentence = { text: "Auto uses it when needed.", tip: "No job is pinned to it; Auto picks it when it fits.", testid: "su-model-used" };
  else if (badge && !badge.proven && needs) sentence = { text: "Not connected yet.", tip: "Add its key or sign in to use it.", testid: "su-model-said" };
  // A capacity word beside a failed proof would read "Available" on a dead login.
  const capacityWord = src && src.capacity !== "unknown" && src.capacity !== "available"
    && (!badge?.proven || badge.tone === "green") ? words(src.capacity) : null;

  const test = async () => {
    setChecking(checkKey, true);
    try {
      apply(await setupWrite<SetupView>(projectId, "checks", { target: checkKey }, { label: "Test" }));
    } catch (error) {
      toast((error as SetupFailure).refusal?.headline ?? "Check failed", "error");
    } finally { setChecking(checkKey, false); }
  };
  const remove = async (confirmed: boolean) => {
    try {
      apply(await setupWrite<SetupView>(projectId, "model_remove", {
        source_id: id, confirm: confirmed ? "true" : undefined }, { label: "Remove" }));
      setRemoving(null);
    } catch (error) {
      const failure = error as SetupFailure;
      if (failure.status === 409 && failure.refusal?.jobs?.length) setRemoving(failure.refusal.jobs);
      else toast(failure.refusal?.headline ?? "Not removed", "error");
    }
  };
  const signIn = () => section.open({ provider: signInProvider!, sourceId: id, signIn: true });
  const connect = () => section.open({ provider: signInProvider!, connect: true });
  const replace = () => section.open({ provider, sourceId: id });
  const canPick = !!entry && entry.kind !== "subscription" && entry.source === "connected";
  const login = !!src && isLogin(src);
  const keyed = !!src && takesKey(src);

  // ONE PRIMARY ACTION, only when the row needs something.
  let primary: React.ReactNode = null;
  if (!busy && !editing && signInProvider && (entry ? needs : offersSignIn(src!))) {
    const next = subscriptionNext(view, signInProvider);
    primary = next === "connect" ? (
      <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-primary"
                   data-testid="su-model-connect-row" onClick={connect}
                   title="The CLI is signed in on the server. Connect checks it; nothing signs out."
                   data-tip="The CLI is signed in on the server. Connect checks it; nothing signs out.">
        Connect
      </GatedButton>
    ) : (
      <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-primary"
                   data-testid={entry ? "su-model-sign-in" : "setup-cap-sign-in"} onClick={signIn}
                   title="Runs the provider's sign-in on the BOSS server, then Connect."
                   data-tip="Runs the provider's sign-in on the BOSS server, then Connect.">
        Sign in
      </GatedButton>
    );
  } else if (!busy && !editing && entry && needs) {
    const action = entry.pill.action;
    if (action === "replace") {
      primary = <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-primary"
                             data-testid="su-model-replace" onClick={replace}>Replace key</GatedButton>;
    } else if (action === "fix_link" && entry.pill.fix_url) {
      primary = <Out href={entry.pill.fix_url} testid="su-model-fix" tip={entry.pill.tip}>
        {entry.pill.state === "no_credit" ? "Top up" : "Fix"}</Out>;
    } else if (action === "default_model" && canPick) {
      primary = <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm"
                             onClick={() => setPicking(true)}>Default model…</GatedButton>;
    } else if (action === "budget") {
      primary = <a className="su-link" href="?area=setup&focus=budget">Budget</a>;
    } else {
      primary = <GatedButton permission="setup:manage" type="button" className="su-btn su-btn-sm"
                             data-testid="su-model-test" onClick={test}>Test</GatedButton>;
    }
  } else if (!entry && keyed && !onBoard) {
    primary = (
      <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-sm" aria-expanded={formOpen}
                   data-testid="setup-cap-key-open" onClick={() => onForm(!formOpen)}>
        {src!.credentialPresent ? "Replace key" : "Add key"}
      </GatedButton>
    );
  } else if (!entry && login && src!.connection !== "connected" && !src!.signInProvider) {
    primary = (
      <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-sm" aria-expanded={formOpen}
                   data-testid="setup-cap-start-open" onClick={() => onForm(!formOpen)}>Sign in</GatedButton>
    );
  }

  // EVERYTHING ELSE behind ⋯ — named for what it does.
  const menu: MenuItem[] = [];
  if (entry) menu.push({ label: "Test", onSelect: test, testid: "su-menu-test" });
  if (signInProvider && (entry || src?.connection === "connected")) {
    menu.push({ label: "Sign in again", onSelect: signIn, testid: "su-menu-sign-in" });
  } else if (entry || (keyed && onBoard)) {
    menu.push({ label: "Replace key", onSelect: replace, testid: "su-menu-replace" });
  }
  if (canPick) menu.push({ label: "Default model…", onSelect: () => setPicking(true), testid: "su-menu-default-model" });
  if (login && src!.connection === "connected") {
    menu.push({ label: "Disconnect session", onSelect: () => setDisconnecting(true), danger: true, testid: "setup-cap-disconnect" });
  }
  if (entry?.source === "connected") {
    menu.push({ label: "Remove", danger: true, onSelect: () => void remove(false), testid: "su-menu-remove" });
  }

  const ids = `Source ${id}${provider ? ` · provider ${provider}` : ""}${src?.kind ? ` · ${src.kind}` : ""}`;
  const mine = sessions.filter((x) => x.sourceId === id);
  const models = src?.models ?? [];
  const kindWord = entry ? KIND[entry.kind].label : KIND_WORDS[src?.kind ?? ""] ?? words(src?.kind ?? "");
  return (
    <div className="su-src" data-testid="setup-cap-source" data-source={id} data-tone={pill.tone}>
      <div className="su-src-line" data-testid={entry ? `su-model-${id}` : undefined}>
        <Tip tip={ids} className="su-src-name su-strong"><span data-user-text>{name}</span></Tip>
        {entry?.credential?.label ? (
          <Tip tip={entry.credential.kind === "long_lived_token"
                 ? "BOSS's own sign-in obtained this token on the BOSS server. Every Claude run uses it."
                 : "Claude Code's own short-lived login. Sign in again for a long-lived token."}
               className="su-faint su-src-credential" testid="su-model-credential">
            · {entry.credential.label}
          </Tip>
        ) : null}
        <span className={`su-pill su-tone-${pill.tone}`} role="status" data-testid="su-model-pill"
              data-state={pill.state || undefined} data-tip={pill.tip} title={pill.tip}
              data-connection-summary={!entry && ["Needs key", "Needs sign-in", "Not configured", "Not connected"].includes(pill.label) || undefined}>
          <Dot tone={pill.tone} />{pill.label}
        </span>
        {sentence ? (
          <Tip tip={sentence.tip} className="su-src-said" testid={sentence.testid}>
            {sentence.text}
          </Tip>
        ) : null}
        {capacityWord ? <Tip tip="The capacity this source last reported." className="su-faint">{capacityWord}</Tip> : null}
        <span className="su-spacer" />
        {primary}
        {red && entry ? <AskBoss row={entry.label} pillLabel={entry.pill.label} tip={entry.pill.tip} /> : null}
        <button type="button" className="su-details-btn" aria-expanded={details} data-testid="setup-cap-details"
                title="What this source is: kind, provider, models." data-tip="What this source is: kind, provider, models."
                onClick={() => setDetails((v) => !v)}>
          Details<CaretDown size={12} aria-hidden="true" className={details ? "su-caret-open" : undefined} />
        </button>
        {menu.length ? (
          <RowMenu label={`${name} actions`} testid="su-model-menu" items={menu}
                   open={section.menuSource === id} />
        ) : <span className="su-menu-gap" aria-hidden="true" />}
      </div>
      {details ? (
        <dl className="su-src-details" data-testid="setup-cap-details-panel" title={ids}>
          <dt>Kind</dt><dd><Tip tip={entry ? KIND[entry.kind].tip : ids}>{kindWord}</Tip></dd>
          {provider ? <><dt>Provider</dt><dd>{PROVIDER_WORDS[provider] ?? words(provider)}</dd></> : null}
          {entry?.model ? <><dt>Model</dt><dd className="su-mono" data-user-text>{entry.model}</dd></> : null}
          {!entry?.model && models.length ? <><dt>Models</dt><dd><Tip tip={models.join(", ")}>{models.map(modelWords).join(", ")}</Tip></dd></> : null}
          {entry?.used_for.length ? <><dt>Used for</dt><dd>{entry.used_for.join(", ")}</dd></> : null}
          {said?.billing ? <><dt>Billing</dt><dd>{said.billing}</dd></> : null}
          {entry?.checked_at ? <><dt>Checked</dt><dd>{ageLabel(entry.checked_at)} ago</dd></> : null}
          {entry?.scope === "org" ? <><dt>Scope</dt><dd><Tip tip="The org's connection: set once, every project in this org uses it." testid="su-model-scope">All projects</Tip></dd></> : null}
          {entry?.source === "install" ? <><dt>Set by</dt><dd><Tip tip="Set in the server's settings file. Replace here to manage it in BOSS.">The install</Tip></dd></> : null}
        </dl>
      ) : null}
      {picking && entry ? <DefaultModel entry={entry} onDone={() => setPicking(false)} /> : null}
      {removing ? (
        <div className="su-confirm" role="alertdialog" data-testid="su-remove-confirm">
          <strong>Remove {name}?</strong>
          <span>{removing.map((job) => `${job} goes back to Auto.`).join(" ")}</span>
          <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-danger"
                  data-testid="su-remove-confirm-yes" onClick={() => void remove(true)}>Remove</GatedButton>
          <button type="button" className="su-link" onClick={() => setRemoving(null)}>Keep</button>
        </div>
      ) : null}
      {disconnecting && src ? (
        <div className="su-confirm" role="alertdialog" data-testid="setup-cap-disconnect-confirm">
          <strong>Disconnect {name}?</strong>
          <GatedButton permission="entitlement:manage" type="button" className="su-btn su-btn-sm su-btn-danger" data-testid="setup-cap-disconnect-yes"
                  onClick={async () => {
                    setDisconnecting(false);
                    await actions.after(await post(`/v1/entitlements/sources/${encodeURIComponent(id)}/disconnect`,
                                                   { project_id: projectId, reason: "Disconnected in Repo and models.", actor: actions.who },
                                                   "Disconnect session", `setup:cap:${id}`), "Session disconnected");
                  }}>Disconnect</GatedButton>
          <button type="button" className="su-link" onClick={() => setDisconnecting(false)}>Keep</button>
        </div>
      ) : null}
      {formOpen && src && keyed && !onBoard ? (
        <KeyForm src={src} onSave={async (value, backend) => actions.after(
          await post(`/v1/entitlements/sources/${encodeURIComponent(id)}/credential`,
                     { project_id: projectId, value, backend, actor: actions.who }, src.credentialPresent ? "Rotate credential" : "Store credential",
                     `setup:cap:${id}`),
          src.credentialPresent ? "Credential rotated" : "Credential stored")} />
      ) : null}
      {formOpen && src && login && src.connection !== "connected" && !src.signInProvider ? (
        <StartForm src={src} onStart={async (mode, scopes) => { await actions.after(
          await post(`/v1/entitlements/sources/${encodeURIComponent(id)}/connection-sessions`,
                     { project_id: projectId, connection_mode: mode || undefined, requested_scopes: scopes, actor: actions.who },
                     "Start official session", `setup:cap:${id}`),
          "Session started"); }} />
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
                <CompleteForm session={x} onDone={async (body) => { await actions.after(
                  await post(`/v1/entitlement-connection-sessions/${encodeURIComponent(x.id)}/complete`,
                             { project_id: projectId, ...body, actor: actions.who }, "Complete session", `setup:cap:${x.id}`),
                  "Session completed"); }} />
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// ── the list ────────────────────────────────────────────────────────────────────────────────

export const CapacityRow = forwardRef<HTMLElement, { actor?: string; focused: boolean }>(function CapacityRow({ actor, focused }, ref) {
  const { projectId, view, apply } = useBoard();
  const section = useModelsSection();
  const [sources, setSources] = useState<CapSource[] | null>(null);
  const [sessions, setSessions] = useState<CapSession[]>([]);
  const [readsComplete, setReadsComplete] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [edited, setEdited] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [locked, setLocked] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
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
  // THE LIST FOLLOWS THE BOARD: every new view (a connect, a sign-in's Connect, a check, a
  // disconnect, a drawer closing) re-reads the sources, so no row keeps an older answer than
  // the header above it.
  useEffect(() => { void load(); }, [load, view]);

  /** After a write: toast, then the board is re-read — and the list with it. */
  const after = async (failed: string | null, done: string) => {
    if (failed) { toast(failed.length > 80 ? "Not saved" : failed, "error"); return false; }
    toast(done);
    setOpen(null);
    try { apply(await fetchSetup(projectId)); } catch { await load(); }
    return true;
  };
  const onBoard = new Set(view.models.map((m) => m.source_id));
  const confirmedUnused = (src: CapSource) => readsComplete && !error && !locked && src.credentialAbsent
    && src.connection === "not_connected"
    && ["declared", "needs_connection", "missing_credentials", "missing_oauth_connection"].includes(src.effective)
    && src.capacity === "unknown" && (takesKey(src) || isLogin(src))
    && !onBoard.has(src.sourceId) && !sessions.some((x) => x.sourceId === src.sourceId);
  const picked = section.picked;
  const unused = (src: CapSource) => confirmedUnused(src) && open !== src.sourceId && !edited.has(src.sourceId)
    && picked !== src.sourceId;
  const choices = (sources ?? []).filter(confirmedUnused);
  const choiceKey = choices.map((c) => c.sourceId).join(",");
  // The drawer offers the unused sources ("More sources"); the list keeps only what is in use.
  useEffect(() => {
    section.setMore(choices.map((c) => ({ id: c.sourceId, label: sourceName(c.sourceId) })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [choiceKey]);
  // A source picked there appears here — kept in view, scrolled to, its action focused. A form
  // already open on another source keeps what was typed in it.
  useEffect(() => {
    if (!picked) return;
    setEdited((prev) => new Set(prev).add(picked));
    section.clearPicked();
    window.setTimeout(() => {
      const row = document.querySelector<HTMLElement>(`[data-testid="setup-cap-source"][data-source="${picked.replace(/["\\]/g, "")}"]`);
      row?.scrollIntoView?.({ behavior: "smooth", block: "center" });
      row?.querySelector<HTMLElement>(".su-src-line .su-btn")?.focus?.();
    }, 50);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picked]);

  // ONE LIST: every Models row (connected first, in the board's order) joined to its source,
  // then the sources not yet in use that are not proven unused.
  const byId = new Map((sources ?? []).map((s) => [s.sourceId, s]));
  const items: Item[] = [
    ...view.models.map((entry) => ({ id: entry.source_id, entry, src: byId.get(entry.source_id) })),
    ...(sources ?? []).filter((src) => !onBoard.has(src.sourceId) && !unused(src))
      .map((src) => ({ id: src.sourceId, src })),
  ];

  return (
    <section ref={ref} className="su-model-sources" id="su-capacity" data-testid="su-row-capacity"
             data-focused={focused || undefined} aria-label="Model sources">
      <div className="su-src-list" data-testid="setup-entitlements">
        {sources === null && !view.models.length ? <span className="su-faint">Loading…</span> : null}
        {locked ? <LockedPanel inline reason={locked} testId="setup-entitlements-locked" /> : null}
        {error ? <Tip tip={error} className="su-faint">Can&apos;t load model sources</Tip> : null}
        {sources && !error && !locked && sources.length === 0 && !view.models.length ? <span className="su-faint">No sources yet</span> : null}
        {items.map((item) => (
          <SourceRow key={item.id} item={item} sessions={sessions} onBoard={onBoard.has(item.id)}
                     formOpen={open === item.id}
                     onForm={(v) => { setEdited((prev) => new Set(prev).add(item.id)); setOpen(v ? item.id : null); }}
                     actions={{ who, after }} />
        ))}
        <div className="su-line su-src-foot" data-testid="setup-entitlement-session">
          <Tip tip="Official logins started from a plan above." className="su-faint su-cap-label">Sessions</Tip>
          <span className="su-faint" data-testid="setup-entitlement-session-count">
            {sessionError ? <Tip tip={sessionError}>Can&apos;t load sessions</Tip>
              : sessions.length ? `${sessions.length} · ${sessions.filter((x) => x.status === "operator_action_required").length} waiting`
              // HONEST "NONE": a subscription signed in on the BOSS server is not a session here;
              // its state is the source's own badge above.
              : <Tip tip="None started here. Server sign-ins show on their source above.">None started here</Tip>}
          </span>
          <span className="su-spacer" />
          <a className="su-link" href="?area=skills" data-testid="setup-broker-skills-link">Skills ↗</a>
        </div>
      </div>
    </section>
  );
});
