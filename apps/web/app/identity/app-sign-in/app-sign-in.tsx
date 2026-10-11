"use client";

/**
 * IDENTITY › APP SIGN-IN (F6 §6.4): BOSS as the sign-in provider of the apps it builds.
 *
 * One surface, four tabs: Pools (who may sign in: BOSS members, or an app's own users), Clients
 * (one per app: return addresses, scopes, MFA, secret), Users (a customer pool's people) and Roles
 * (app roles and who holds them). Every row opens a side sheet. Reading needs a fresh second factor
 * (the admin permissions are step-up), so the lists load through the step-up guard. A client
 * secret is shown once, in the sheet that created or rotated it, and dropped when it closes.
 *
 * Who reads what: Owners, Admins and Auditors read every client (org-wide); an Operator reads the
 * clients of the projects they manage (oidc_client:read there), so when the org-wide list is
 * refused the lists are read project by project. Plain words everywhere: an app is picked from the
 * project's apps, scopes and client types are named for people, the raw value is the tooltip.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Copy, Key, Plus, ShieldCheck, UsersThree } from "@phosphor-icons/react";

import { useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { EmptyState } from "../../../components/identity/states";
import { ListHeader } from "../../../components/identity/list-header";
import { ErrorLine, Sheet } from "../../../components/identity/kit";
import { showToast } from "../../client-forms";
import { IdentityError, enc, idDelete, idGet, idPatch, idPost } from "../../../lib/identity/api";

export type Pool = { id: string; name: string; kind: "workforce" | "customer"; signup: string; signup_domains: string[]; mfa: string; passkeys: boolean; password_policy: Record<string, unknown> };
export type Client = {
  client_id: string; project_id: string; app_key: string; pool_id: string; name: string; type: "confidential" | "public";
  redirect_uris: string[]; post_logout_redirect_uris: string[]; allowed_scopes: string[]; token_endpoint_auth_method: string;
  id_token_alg: string; subject_type: string; require_mfa: boolean; first_party: boolean; status: string; has_secret: boolean;
  client_secret?: string;
};
type PoolUser = { id: string; email: string; name: string | null; status: string; email_verified: boolean; factors?: string[]; last_sign_in_at: string | null };
type Role = { role_key: string; name: string };
type Assignment = { subject_ref: string; role_key: string };
type Tab = "pools" | "clients" | "users" | "roles";
const TABS: { id: Tab; label: string; tip: string }[] = [
  { id: "pools", label: "Pools", tip: "Who may sign in: BOSS members, or an app's own users" },
  { id: "clients", label: "Clients", tip: "One per app: return addresses, scopes, MFA, secret" },
  { id: "users", label: "Users", tip: "People in a customer pool" },
  { id: "roles", label: "Roles", tip: "App roles and who holds them" },
];
const SCOPES = ["openid", "profile", "email", "roles", "offline_access"] as const;
/** What each scope gives the app, in people's words (the raw scope is the tooltip). */
export const SCOPE_WORDS: Record<string, string> = {
  openid: "Who they are", profile: "Name", email: "Email address", roles: "App roles", offline_access: "Stay signed in",
};
export const TYPE_WORDS: Record<string, string> = { confidential: "Server app", public: "Browser app" };
const SIGNUP_WORDS: Record<string, string> = { open: "Anyone", domain: "Email domains", invite: "Invitation only", closed: "Closed" };
const MFA_WORDS: Record<string, string> = { optional: "Optional", required: "Required", off: "Off" };
const STATUS_WORDS: Record<string, string> = { active: "Active", disabled: "Disabled" };
const FACTOR_WORDS: Record<string, string> = { totp: "Authenticator app", webauthn: "Passkey", passkey: "Passkey" };
const say = (table: Record<string, string>, v: string) => table[v] ?? v;

/** An app's name as a client's app key: lowercase words joined by dashes (what the API accepts). */
export function appKeyOf(name: string): string {
  return name.normalize("NFKD").replace(/\p{M}+/gu, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80).replace(/-+$/, "");
}
export const APP_KEY = /^[a-z0-9][a-z0-9_-]{0,79}$/;

export function words(e: unknown, fallback: string): string {
  if (e instanceof IdentityError) return e.status === 403 ? "You don't hold the permission for this." : e.message;
  return fallback;
}

/** "one per line" text -> trimmed, non-empty lines. */
export function lines(text: string): string[] {
  return text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
}

type Project = { id: string; name: string };

/** Each project's list, merged (an Operator reads only where they manage clients). */
async function perProject<T>(projects: Project[], read: (p: Project) => Promise<T[]>, key: (t: T) => string): Promise<T[]> {
  const out = new Map<string, T>();
  for (const list of await Promise.all(projects.map((p) => read(p).catch(() => [] as T[])))) for (const t of list) out.set(key(t), t);
  return [...out.values()];
}

export function AppSignIn({ projects, readProjects, publicOrigin }: {
  projects: Project[]; readProjects?: Project[]; publicOrigin: string | null;
}) {
  const guarded = useGuardedCall();
  const { me } = useIdentity();
  const issuer = publicOrigin && me?.org?.id ? `${publicOrigin}/oidc/${me.org.id}` : null;
  const [tab, setTab] = useState<Tab>("clients");
  const [pools, setPools] = useState<Pool[] | null>(null);
  const [clients, setClients] = useState<Client[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const why = "Viewing app sign-in needs a fresh check.";
      const scoped = readProjects ?? projects;
      const orgWide = async <T,>(path: string, field: string, key: (t: T) => string): Promise<T[]> => {
        try {
          return (await guarded(why, () => idGet<Record<string, T[]>>(path)))[field] ?? [];
        } catch (e) {
          // Refused org-wide: read it where this person may (an Operator's projects).
          if (!(e instanceof IdentityError && e.status === 403) || !scoped.length) throw e;
          return perProject(scoped, async (proj) =>
            (await guarded(why, () => idGet<Record<string, T[]>>(`${path}?project_id=${enc(proj.id)}`)))[field] ?? [], key);
        }
      };
      const [p, c] = await Promise.all([
        orgWide<Pool>("/v1/oidc/pools", "pools", (x) => x.id).catch(() => [] as Pool[]),
        orgWide<Client>("/v1/oidc/clients", "clients", (x) => x.client_id),
      ]);
      setPools(p);
      setClients(c);
    } catch (e) {
      setError(words(e, "App sign-in could not be loaded."));
    }
  }, [guarded, projects, readProjects]);
  useEffect(() => { void load(); }, [load]);

  return (
    <section className="idn-app-signin" data-testid="app-sign-in">
      <ListHeader title="App sign-in" sub={issuer ? <>Issuer <code data-copy="data">{issuer}</code></> : "BOSS needs its public address before apps can sign in."} empty={false} />
      <div className="idn-tabs" role="tablist" aria-label="App sign-in">
        {TABS.map((t) => (
          <a key={t.id} role="tab" href={`#${t.id}`} title={t.tip} aria-current={tab === t.id ? "page" : undefined} aria-selected={tab === t.id}
            onClick={(e) => { e.preventDefault(); setTab(t.id); }}>{t.label}</a>
        ))}
      </div>
      <ErrorLine error={error} />
      {pools && clients ? (
        tab === "pools" ? <Pools pools={pools} reload={load} />
          : tab === "clients" ? <Clients clients={clients} pools={pools} projects={projects} issuer={issuer} reload={load} />
            : tab === "users" ? <Users pools={pools.filter((p) => p.kind === "customer")} />
              : <Roles clients={clients} pools={pools} />
      ) : !error ? <p className="idn-hint">Loading…</p> : null}
    </section>
  );
}

// ---- pools ---------------------------------------------------------------------------------------

function Pools({ pools, reload }: { pools: Pool[]; reload: () => Promise<void> }) {
  const [open, setOpen] = useState<Pool | "new" | null>(null);
  const create = <button type="button" className="idn-btn primary sm" onClick={() => setOpen("new")}><Plus size={14} aria-hidden /> New pool</button>;
  return (
    <>
      <ListHeader level={2} title="Pools" sub="Who may sign in to an app" empty={!pools.length} create={create} />
      {pools.length ? (
        <div className="idn-scroll"><table className="idn-t"><thead><tr><th>Name</th><th>Who</th><th className="idn-narrow-hide">Sign-up</th><th>Second factor</th><th className="idn-narrow-hide">Passkeys</th></tr></thead>
          <tbody>{pools.map((p) => (
            <tr key={p.id} onClick={() => setOpen(p)} className="idn-click" data-testid="pool-row">
              <td>{p.name}</td><td>{p.kind === "workforce" ? "BOSS members" : "The app's own users"}</td>
              <td className="idn-narrow-hide">{p.kind === "workforce" ? "BOSS members only" : say(SIGNUP_WORDS, p.signup)}</td><td>{say(MFA_WORDS, p.mfa)}</td><td className="idn-narrow-hide">{p.passkeys ? "On" : "Off"}</td>
            </tr>))}</tbody></table></div>
      ) : <EmptyState icon={<UsersThree size={22} />} title="No pools yet" action={create}>A pool says who may sign in: your BOSS members, or an app's own users.</EmptyState>}
      {open ? <PoolSheet pool={open === "new" ? null : open} onClose={() => setOpen(null)} reload={reload} /> : null}
    </>
  );
}

function PoolSheet({ pool, onClose, reload }: { pool: Pool | null; onClose: () => void; reload: () => Promise<void> }) {
  const guarded = useGuardedCall();
  const [d, setD] = useState({
    name: pool?.name ?? "", kind: pool?.kind ?? "customer", signup: pool?.signup ?? "open",
    domains: (pool?.signup_domains ?? []).join(", "), mfa: pool?.mfa ?? "optional", passkeys: pool?.passkeys ?? true,
    verify: (pool?.password_policy?.verify_email as boolean | undefined) ?? true,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true); setError(null);
    const body = { name: d.name, signup: d.kind === "workforce" ? "closed" : d.signup, mfa: d.mfa, passkeys: d.passkeys,
      signup_domains: d.domains.split(",").map((x) => x.trim()).filter(Boolean), password_policy: { verify_email: d.verify } };
    try {
      if (pool) await guarded("Changing a pool needs a fresh check.", () => idPatch(`/v1/oidc/pools/${enc(pool.id)}`, body));
      else await guarded("Creating a pool needs a fresh check.", () => idPost("/v1/oidc/pools", { ...body, kind: d.kind }));
      showToast("success", pool ? "Pool saved" : "Pool created");
      await reload(); onClose();
    } catch (e) { setError(words(e, "The pool was not saved.")); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!pool) return;
    try {
      await guarded("Deleting a pool needs a fresh check.", () => idDelete(`/v1/oidc/pools/${enc(pool.id)}`));
      showToast("success", "Pool deleted"); await reload(); onClose();
    } catch (e) { setError(words(e, "The pool was not deleted.")); }
  };
  return (
    <Sheet side title={pool ? pool.name : "New pool"} icon={<UsersThree size={18} aria-hidden />} onClose={onClose} testId="pool-sheet">
      <div className="idn-field"><label htmlFor="pool-name">Name</label><input id="pool-name" className="idn-input" value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} /></div>
      {!pool ? (
        <div className="idn-field"><label htmlFor="pool-kind">Who signs in</label>
          <select id="pool-kind" className="idn-input" value={d.kind} onChange={(e) => setD({ ...d, kind: e.target.value as Pool["kind"] })}>
            <option value="customer">The app's own users (sign-up, MFA, passkeys)</option>
            <option value="workforce">My BOSS members (their BOSS sign-in)</option>
          </select></div>
      ) : null}
      {d.kind === "customer" ? (<>
        <div className="idn-field"><label htmlFor="pool-signup">Sign-up</label>
          <select id="pool-signup" className="idn-input" value={d.signup} onChange={(e) => setD({ ...d, signup: e.target.value })}>
            <option value="open">Anyone</option><option value="domain">Email domains only</option><option value="invite">Invitation only</option><option value="closed">Closed</option>
          </select></div>
        {d.signup === "domain" ? <div className="idn-field"><label htmlFor="pool-domains">Domains</label><input id="pool-domains" className="idn-input" placeholder="example.com, example.org" value={d.domains} onChange={(e) => setD({ ...d, domains: e.target.value })} /></div> : null}
        <label className="idn-check"><input type="checkbox" checked={d.verify} onChange={(e) => setD({ ...d, verify: e.target.checked })} /> Confirm email addresses by code</label>
        <label className="idn-check"><input type="checkbox" checked={d.passkeys} onChange={(e) => setD({ ...d, passkeys: e.target.checked })} /> Passkeys</label>
      </>) : null}
      <div className="idn-field"><label htmlFor="pool-mfa">Second factor</label>
        <select id="pool-mfa" className="idn-input" value={d.mfa} onChange={(e) => setD({ ...d, mfa: e.target.value })}>
          <option value="optional">Optional</option><option value="required">Required</option><option value="off">Off</option>
        </select></div>
      <ErrorLine error={error} />
      <div className="idn-row">
        <button type="button" className="idn-btn primary" disabled={busy || !d.name.trim()} onClick={save}>{pool ? "Save" : "Create pool"}</button>
        {pool ? <button type="button" className="idn-btn danger" onClick={remove}>Delete</button> : null}
      </div>
    </Sheet>
  );
}

// ---- clients -------------------------------------------------------------------------------------

function Clients({ clients, pools, projects, issuer, reload }: {
  clients: Client[]; pools: Pool[]; projects: { id: string; name: string }[]; issuer: string | null; reload: () => Promise<void>;
}) {
  const [open, setOpen] = useState<Client | "new" | null>(null);
  const poolName = (id: string) => pools.find((p) => p.id === id)?.name ?? id;
  const create = <button type="button" className="idn-btn primary sm" disabled={!pools.length || !projects.length} title={!pools.length ? "Create a pool first" : undefined} onClick={() => setOpen("new")}><Plus size={14} aria-hidden /> Register app</button>;
  return (
    <>
      <ListHeader level={2} title="Clients" sub="Each app that signs in with BOSS" empty={!clients.length} create={create} />
      {clients.length ? (
        <div className="idn-scroll"><table className="idn-t"><thead><tr><th>App</th><th className="idn-narrow-hide">Pool</th><th className="idn-narrow-hide">Kind</th><th className="idn-narrow-hide">Gets</th><th>Status</th></tr></thead>
          <tbody>{clients.map((c) => (
            <tr key={c.client_id} onClick={() => setOpen(c)} className="idn-click" data-testid="client-row">
              <td>{c.name} <span className="idn-hint">{c.app_key}</span></td><td className="idn-narrow-hide">{poolName(c.pool_id)}</td>
              <td className="idn-narrow-hide" title={c.type}>{say(TYPE_WORDS, c.type)}</td>
              <td className="idn-narrow-hide" title={c.allowed_scopes.join(" ")}>{c.allowed_scopes.map((x) => say(SCOPE_WORDS, x)).join(", ")}</td>
              <td><span className={`idn-pill ${c.status === "active" ? "ok" : "mute"}`}>{say(STATUS_WORDS, c.status)}</span></td>
            </tr>))}</tbody></table></div>
      ) : <EmptyState icon={<Key size={22} />} title="No apps sign in with BOSS yet" action={create}>Register an app to give it a client id and secret.</EmptyState>}
      {open ? <ClientSheet client={open === "new" ? null : open} pools={pools} projects={projects} issuer={issuer} onClose={() => setOpen(null)} reload={reload} /> : null}
    </>
  );
}

function SecretOnce({ client, issuer }: { client: Client; issuer: string | null }) {
  const copy = (v: string) => { void navigator.clipboard?.writeText(v); showToast("success", "Copied"); };
  return (
    <div className="idn-card" data-testid="client-secret" role="status">
      <p><ShieldCheck size={16} aria-hidden /> Copy the secret now. BOSS will not show it again.</p>
      {[["Issuer", issuer ?? "—"], ["Client id", client.client_id], ["Client secret", client.client_secret ?? ""]].map(([k, v]) => (
        <div className="idn-row" key={k}><span className="idn-label">{k}</span><code data-copy="data">{v}</code>
          {v && v !== "—" ? <button type="button" className="idn-btn ghost sm" aria-label={`Copy ${k}`} onClick={() => copy(v)}><Copy size={14} aria-hidden /></button> : null}</div>
      ))}
    </div>
  );
}

function ClientSheet({ client, pools, projects, issuer, onClose, reload }: {
  client: Client | null; pools: Pool[]; projects: { id: string; name: string }[]; issuer: string | null; onClose: () => void; reload: () => Promise<void>;
}) {
  const guarded = useGuardedCall();
  const [d, setD] = useState({
    project_id: client?.project_id ?? projects[0]?.id ?? "", app_key: client?.app_key ?? "", name: client?.name ?? "",
    pool_id: client?.pool_id ?? pools[0]?.id ?? "", type: client?.type ?? "confidential",
    redirect: (client?.redirect_uris ?? []).join("\n"), logout: (client?.post_logout_redirect_uris ?? []).join("\n"),
    scopes: client?.allowed_scopes ?? ["openid", "profile", "email"], require_mfa: client?.require_mfa ?? false,
    first_party: client?.first_party ?? false, alg: client?.id_token_alg ?? "RS256", status: client?.status ?? "active",
  });
  const [shown, setShown] = useState<Client | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // THE APP PICKER: the project's apps (the Factory's rows), each as its app key; "another app"
  // types a key for an app BOSS did not build.
  const [apps, setApps] = useState<{ name: string; key: string }[] | null>(null);
  const [other, setOther] = useState(false);
  useEffect(() => {
    if (client || !d.project_id) return;
    let live = true;
    setApps(null);
    idGet<{ apps?: { app?: string }[] }>(`/v1/factory/apps?project_id=${enc(d.project_id)}`)
      .then((r) => {
        const seen = new Set<string>();
        const list = (r.apps ?? []).map((a) => ({ name: String(a.app ?? ""), key: appKeyOf(String(a.app ?? "")) }))
          .filter((a) => APP_KEY.test(a.key) && !seen.has(a.key) && Boolean(seen.add(a.key)));
        if (live) setApps(list);
      })
      .catch(() => { if (live) setApps([]); });
    return () => { live = false; };
  }, [client, d.project_id]);
  const typing = other || (apps !== null && apps.length === 0);
  const pick = (key: string) => {
    if (key === "__other") { setOther(true); setD({ ...d, app_key: "" }); return; }
    setOther(false);
    const app = apps?.find((a) => a.key === key);
    setD({ ...d, app_key: key, name: d.name || app?.name || "" });
  };
  const toggle = (s: string) => setD({ ...d, scopes: d.scopes.includes(s) ? d.scopes.filter((x) => x !== s) : [...d.scopes, s] });
  const save = async () => {
    setBusy(true); setError(null);
    const common = { name: d.name, redirect_uris: lines(d.redirect), post_logout_redirect_uris: lines(d.logout),
      allowed_scopes: d.scopes, require_mfa: d.require_mfa, first_party: d.first_party, id_token_alg: d.alg };
    try {
      if (client) {
        await guarded("Changing an app's sign-in needs a fresh check.", () => idPatch(`/v1/oidc/clients/${enc(client.client_id)}`, { ...common, status: d.status }));
        showToast("success", "Saved"); await reload(); onClose();
      } else {
        const made = await guarded("Registering an app needs a fresh check.", () => idPost<Client>("/v1/oidc/clients", {
          ...common, project_id: d.project_id, app_key: d.app_key, pool_id: d.pool_id, type: d.type,
          token_endpoint_auth_method: d.type === "public" ? "none" : "client_secret_basic" }));
        setShown(made); await reload();
      }
    } catch (e) { setError(words(e, "The app was not saved.")); } finally { setBusy(false); }
  };
  const rotate = async () => {
    if (!client) return;
    try {
      setShown(await guarded("Rotating a secret needs a fresh check.", () => idPost<Client>(`/v1/oidc/clients/${enc(client.client_id)}/rotate-secret`)));
    } catch (e) { setError(words(e, "The secret was not rotated.")); }
  };
  const remove = async () => {
    if (!client) return;
    try {
      await guarded("Removing an app needs a fresh check.", () => idDelete(`/v1/oidc/clients/${enc(client.client_id)}`));
      showToast("success", "App removed; its tokens no longer work"); await reload(); onClose();
    } catch (e) { setError(words(e, "The app was not removed.")); }
  };
  return (
    <Sheet side title={client ? client.name : "Register an app"} icon={<Key size={18} aria-hidden />} onClose={onClose} wide testId="client-sheet" locked={Boolean(shown)}>
      {shown ? (<>
        <SecretOnce client={shown} issuer={issuer} />
        <div className="idn-row"><button type="button" className="idn-btn primary" onClick={onClose}>I copied it</button></div>
      </>) : (<>
        {!client ? (<>
          <div className="idn-field"><label htmlFor="c-project">Project</label>
            <select id="c-project" className="idn-input" value={d.project_id} onChange={(e) => setD({ ...d, project_id: e.target.value })}>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
          {apps?.length ? (
            <div className="idn-field"><label htmlFor="c-app">App</label>
              <select id="c-app" className="idn-input" value={other ? "__other" : d.app_key} onChange={(e) => pick(e.target.value)}>
                <option value="">Choose an app…</option>
                {apps.map((a) => <option key={a.key} value={a.key}>{a.name}</option>)}
                <option value="__other">Another app (type its key)</option>
              </select></div>
          ) : apps === null ? <p className="idn-hint">Loading the project's apps…</p> : null}
          {typing ? (
            <div className="idn-field"><label htmlFor="c-app-key">App key</label>
              <input id="c-app-key" className="idn-input" placeholder="bank" value={d.app_key} onChange={(e) => setD({ ...d, app_key: e.target.value.toLowerCase() })} />
              <span className="idn-hint">Lowercase letters, digits and dashes{apps?.length === 0 ? ". This project has no apps yet." : "."}</span></div>
          ) : null}
          <div className="idn-field"><label htmlFor="c-pool">Pool</label>
            <select id="c-pool" className="idn-input" value={d.pool_id} onChange={(e) => setD({ ...d, pool_id: e.target.value })}>
              {pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
          <div className="idn-field"><label htmlFor="c-type">Type</label>
            <select id="c-type" className="idn-input" value={d.type} onChange={(e) => setD({ ...d, type: e.target.value as Client["type"] })}>
              <option value="confidential">Server app (keeps a secret)</option><option value="public">Browser or mobile app (no secret)</option></select></div>
        </>) : null}
        <div className="idn-field"><label htmlFor="c-name">Name shown to people</label><input id="c-name" className="idn-input" value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} /></div>
        <div className="idn-field"><label htmlFor="c-redirect">Return addresses, one per line (exact match)</label>
          <textarea id="c-redirect" className="idn-input" value={d.redirect} placeholder="https://app.example.com/callback" onChange={(e) => setD({ ...d, redirect: e.target.value })} /></div>
        <div className="idn-field"><label htmlFor="c-logout">After sign-out, one per line</label>
          <textarea id="c-logout" className="idn-input" value={d.logout} onChange={(e) => setD({ ...d, logout: e.target.value })} /></div>
        <div className="idn-field"><span className="idn-label">What the app gets</span><div className="idn-row">
          {SCOPES.map((s) => <label key={s} className="idn-check" title={s}><input type="checkbox" checked={d.scopes.includes(s)} disabled={s === "openid"} onChange={() => toggle(s)} /> {SCOPE_WORDS[s]}</label>)}</div></div>
        <label className="idn-check"><input type="checkbox" checked={d.require_mfa} onChange={(e) => setD({ ...d, require_mfa: e.target.checked })} /> Require a second factor</label>
        <label className="idn-check" title="Skips the consent screen (never for staying signed in). Admins only."><input type="checkbox" checked={d.first_party} onChange={(e) => setD({ ...d, first_party: e.target.checked })} /> First-party app</label>
        <div className="idn-field"><label htmlFor="c-alg">ID token signature</label>
          <select id="c-alg" className="idn-input" value={d.alg} onChange={(e) => setD({ ...d, alg: e.target.value })}><option value="RS256">RS256 (every library)</option><option value="EdDSA">EdDSA</option></select></div>
        {client ? (
          <div className="idn-field"><label htmlFor="c-status">Status</label>
            <select id="c-status" className="idn-input" value={d.status} onChange={(e) => setD({ ...d, status: e.target.value })}><option value="active">Active</option><option value="disabled">Disabled (ends its tokens)</option></select></div>
        ) : null}
        <ErrorLine error={error} />
        <div className="idn-row">
          <button type="button" className="idn-btn primary" disabled={busy || !d.name.trim() || !lines(d.redirect).length || (!client && !APP_KEY.test(d.app_key))} onClick={save}>{client ? "Save" : "Register"}</button>
          {client?.has_secret ? <button type="button" className="idn-btn" onClick={rotate}>Rotate secret</button> : null}
          {client ? <button type="button" className="idn-btn danger" onClick={remove}>Remove</button> : null}
        </div>
      </>)}
    </Sheet>
  );
}

// ---- users (customer pools) ------------------------------------------------------------------------

function Users({ pools }: { pools: Pool[] }) {
  const guarded = useGuardedCall();
  const [poolId, setPoolId] = useState(pools[0]?.id ?? "");
  const [users, setUsers] = useState<PoolUser[] | null>(null);
  const [open, setOpen] = useState<PoolUser | "invite" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    if (!poolId) return;
    setError(null);
    try { setUsers((await guarded("Viewing a pool's users needs a fresh check.", () => idGet<{ users: PoolUser[] }>(`/v1/oidc/pools/${enc(poolId)}/users`))).users); }
    catch (e) { setError(words(e, "Users could not be loaded.")); }
  }, [guarded, poolId]);
  useEffect(() => { void load(); }, [load]);
  if (!pools.length) return <EmptyState icon={<UsersThree size={22} />} title="No customer pools">Users live in a pool of the app's own users. Create one on Pools.</EmptyState>;
  const create = <button type="button" className="idn-btn primary sm" onClick={() => setOpen("invite")}><Plus size={14} aria-hidden /> Invite</button>;
  return (
    <>
      <ListHeader level={2} title="Users" sub={<select aria-label="Pool" className="idn-input" value={poolId} onChange={(e) => setPoolId(e.target.value)}>{pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>}
        empty={!users?.length} create={create} />
      <ErrorLine error={error} />
      {users?.length ? (
        <div className="idn-scroll"><table className="idn-t"><thead><tr><th>Email</th><th className="idn-narrow-hide">Name</th><th className="idn-narrow-hide">Sign-in methods</th><th>Status</th></tr></thead>
          <tbody>{users.map((u) => (
            <tr key={u.id} onClick={() => setOpen(u)} className="idn-click" data-testid="pool-user-row">
              <td>{u.email}{u.email_verified ? null : <span className="idn-pill warn">unconfirmed</span>}</td><td className="idn-narrow-hide">{u.name ?? "—"}</td>
              <td className="idn-narrow-hide">{["Password", ...(u.factors ?? []).map((f) => FACTOR_WORDS[f] ?? f)].join(", ")}</td>
              <td><span className={`idn-pill ${u.status === "active" ? "ok" : "mute"}`}>{say(STATUS_WORDS, u.status)}</span></td>
            </tr>))}</tbody></table></div>
      ) : users ? <EmptyState icon={<UsersThree size={22} />} title="No users yet" action={create}>They appear when they sign up, or when you invite them.</EmptyState> : null}
      {open ? <UserSheet poolId={poolId} user={open === "invite" ? null : open} onClose={() => setOpen(null)} reload={load} /> : null}
    </>
  );
}

function UserSheet({ poolId, user, onClose, reload }: { poolId: string; user: PoolUser | null; onClose: () => void; reload: () => Promise<void> }) {
  const guarded = useGuardedCall();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const act = async (why: string, call: () => Promise<unknown>, done: string) => {
    setError(null);
    try { await guarded(why, call); showToast("success", done); await reload(); onClose(); } catch (e) { setError(words(e, "That did not work.")); }
  };
  const base = `/v1/oidc/pools/${enc(poolId)}/users`;
  return (
    <Sheet side title={user ? user.email : "Invite a user"} icon={<UsersThree size={18} aria-hidden />} onClose={onClose} testId="pool-user-sheet">
      {user ? (<>
        <p className="idn-hint">Disabling or resetting MFA signs them out of every app at once.</p>
        <div className="idn-row">
          {user.status === "active"
            ? <button type="button" className="idn-btn danger" onClick={() => act("Disabling a user needs a fresh check.", () => idPatch(`${base}/${enc(user.id)}`, { status: "disabled" }), "User disabled")}>Disable</button>
            : <button type="button" className="idn-btn" onClick={() => act("Enabling a user needs a fresh check.", () => idPatch(`${base}/${enc(user.id)}`, { status: "active" }), "User enabled")}>Enable</button>}
          <button type="button" className="idn-btn" onClick={() => act("Resetting MFA needs a fresh check.", () => idPatch(`${base}/${enc(user.id)}`, { reset_mfa: true }), "MFA reset")}>Reset MFA</button>
        </div>
      </>) : (<>
        <div className="idn-field"><label htmlFor="u-email">Email</label><input id="u-email" className="idn-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></div>
        <div className="idn-field"><label htmlFor="u-name">Name</label><input id="u-name" className="idn-input" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <p className="idn-hint">They get an email and choose a password with “Forgot password”.</p>
        <div className="idn-row"><button type="button" className="idn-btn primary" disabled={!email.includes("@")} onClick={() => act("Inviting needs a fresh check.", () => idPost(base, { email, name: name || undefined }), "Invited")}>Invite</button></div>
      </>)}
      <ErrorLine error={error} />
    </Sheet>
  );
}

// ---- roles -----------------------------------------------------------------------------------------

function Roles({ clients, pools }: { clients: Client[]; pools: Pool[] }) {
  const guarded = useGuardedCall();
  const [clientId, setClientId] = useState(clients[0]?.client_id ?? "");
  const [roles, setRoles] = useState<Role[]>([]);
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [newRole, setNewRole] = useState("");
  const [assign, setAssign] = useState({ subject_ref: "", role_key: "" });
  const [error, setError] = useState<string | null>(null);
  const client = clients.find((c) => c.client_id === clientId);
  const pool = pools.find((p) => p.id === client?.pool_id);
  const load = useCallback(async () => {
    if (!clientId) return;
    setError(null);
    try {
      const [r, a] = await Promise.all([
        guarded("Viewing roles needs a fresh check.", () => idGet<{ roles: Role[] }>(`/v1/oidc/clients/${enc(clientId)}/roles`)),
        guarded("Viewing roles needs a fresh check.", () => idGet<{ assignments: Assignment[] }>(`/v1/oidc/clients/${enc(clientId)}/role-assignments`)),
      ]);
      setRoles(r.roles); setAssignments(a.assignments);
    } catch (e) { setError(words(e, "Roles could not be loaded.")); }
  }, [guarded, clientId]);
  useEffect(() => { void load(); }, [load]);
  if (!clients.length) return <EmptyState icon={<ShieldCheck size={22} />} title="No apps yet">App roles belong to an app. Register one on Clients.</EmptyState>;
  const run = async (why: string, call: () => Promise<unknown>) => {
    setError(null);
    try { await guarded(why, call); await load(); } catch (e) { setError(words(e, "That did not work.")); }
  };
  return (
    <>
      <ListHeader level={2} title="Roles" sub={<select aria-label="App" className="idn-input" value={clientId} onChange={(e) => setClientId(e.target.value)}>{clients.map((c) => <option key={c.client_id} value={c.client_id}>{c.name}</option>)}</select>} empty={!roles.length} />
      <p className="idn-hint">Roles reach the app in the <code>roles</code> claim. {pool?.kind === "workforce" ? "Assign them to BOSS members by their id (usr_…)." : "Assign them to the pool's users by their id (opu_…)."}</p>
      <div className="idn-row">
        <input aria-label="New role key" className="idn-input" placeholder="teller" value={newRole} onChange={(e) => setNewRole(e.target.value)} />
        <button type="button" className="idn-btn sm" disabled={!newRole} onClick={() => run("Adding a role needs a fresh check.", () => idPost(`/v1/oidc/clients/${enc(clientId)}/roles`, { role_key: newRole }).then(() => setNewRole("")))}>Add role</button>
      </div>
      <div className="idn-scroll"><table className="idn-t"><thead><tr><th>Role</th><th>Held by</th></tr></thead>
        <tbody>{roles.map((r) => (
          <tr key={r.role_key}><td>{r.name} <span className="idn-hint">{r.role_key}</span></td>
            <td>{assignments.filter((a) => a.role_key === r.role_key).map((a) => (
              <span key={a.subject_ref} className="idn-pill info">{a.subject_ref}
                <button type="button" className="idn-btn ghost sm" aria-label={`Remove ${r.role_key} from ${a.subject_ref}`} onClick={() => run("Removing a role needs a fresh check.", () => idPost(`/v1/oidc/clients/${enc(clientId)}/role-assignments`, { ...a, remove: true }))}>×</button></span>))}</td></tr>))}</tbody></table></div>
      {roles.length ? (
        <div className="idn-row">
          <input aria-label="Subject id" className="idn-input" placeholder={pool?.kind === "workforce" ? "usr_…" : "opu_…"} value={assign.subject_ref} onChange={(e) => setAssign({ ...assign, subject_ref: e.target.value })} />
          <select aria-label="Role" className="idn-input" value={assign.role_key} onChange={(e) => setAssign({ ...assign, role_key: e.target.value })}>
            <option value="">Role…</option>{roles.map((r) => <option key={r.role_key} value={r.role_key}>{r.name}</option>)}</select>
          <button type="button" className="idn-btn sm" disabled={!assign.subject_ref || !assign.role_key} onClick={() => run("Assigning a role needs a fresh check.", () => idPost(`/v1/oidc/clients/${enc(clientId)}/role-assignments`, assign))}>Assign</button>
        </div>
      ) : null}
      <ErrorLine error={error} />
    </>
  );
}
