"use client";

/**
 * SERVICE ACCOUNTS AND KEYS (mockup f, IDENTITY.md §5, §9.5).
 *
 * Non-human accounts for CI and scripts: owners, an access role, and keys. A key is identified
 * by its random public id and its name — BOSS never shows any part of a secret again after the
 * one dialog that shows it whole (D20). Creating a key: name, scopes (a preset or a custom pick,
 * never more than the account holds), projects, networks, expiry; rotating is the same form with
 * the old key and an overlap. The secret lives only in this component's state while the dialog
 * is open, and is dropped when it closes.
 */
import React, { useEffect, useMemo, useState } from "react";
import {
  ArrowsClockwise, Check, CheckCircle, Copy, GitBranch, Key, LockSimple, Plus, Prohibit, Trash, UsersThree, Warning, X,
} from "@phosphor-icons/react";

import { CanButton, useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { EmptyState } from "../../../components/identity/states";
import { keyCount, permissionName } from "../../../lib/identity/words";
import { PeoplePick, type PickPerson } from "./people-pick";
import { adaptBinding, adaptServiceAccount, roleIndex, type NameIndex } from "../../../lib/identity/adapt-people";
import { Ago, ErrorLine, Face, Perm, RoleChip, Sheet, daysUntil, shortDate, useNow } from "../../../components/identity/kit";
import { showToast } from "../../client-forms";
import { IdentityError, idDelete, idGet, idPatch, idPost } from "../../../lib/identity/api";
import { PERMISSION_META, isPermission, type Permission } from "../../../lib/identity/permissions.gen";
import type { AccessRole, ApiKey, IssuedSecret, ServiceAccount } from "../../../lib/identity/types";

type Preset = "read" | "mcp" | "ci" | "custom";
const PRESETS: { id: Preset; label: string; tip: string }[] = [
  { id: "read", label: "Read-only", tip: "Every read permission the account holds" },
  { id: "mcp", label: "MCP client", tip: "Delegable permissions, never the critical ones" },
  { id: "ci", label: "CI deploy", tip: "Deploy, read deployments, read builds" },
  { id: "custom", label: "Custom", tip: "Pick each scope yourself" },
];
const CI: Permission[] = ["deployment:create", "deployment:read", "code_task:read"];
const EXPIRY = [30, 90, 180, 365] as const;
const OVERLAP: { h: number; label: string }[] = [{ h: 0, label: "None" }, { h: 1, label: "1 h" }, { h: 24, label: "24 h" }, { h: 168, label: "7 d" }];

export function presetScopes(preset: Preset, held: Permission[]): Permission[] {
  if (preset === "read") return held.filter((p) => p.endsWith(":read"));
  if (preset === "mcp") return held.filter((p) => PERMISSION_META[p].delegable && !PERMISSION_META[p].critical);
  if (preset === "ci") return CI.filter((p) => held.includes(p));
  return [];
}

function message(e: unknown, fallback: string): string {
  return e instanceof IdentityError ? (e.code === "forbidden" && e.permission ? `You don't hold “${permissionName(e.permission)}”` : e.message) : fallback;
}

function Expires({ k }: { k: ApiKey }) {
  const now = useNow();
  if (k.status === "revoked") return <span className="idn-pill bad">revoked</span>;
  if (k.status === "grace" && k.grace_until) {
    const h = Math.max(1, Math.round((Date.parse(k.grace_until) - now) / 3_600_000));
    return <span className="idn-pill mute" title="Stops working when the overlap ends">grace {h}h</span>;
  }
  if (!k.expires_at) return <span className="idn-pill warn" title="No expiry · flagged in posture">never</span>;
  const d = daysUntil(k.expires_at, now) ?? 0;
  if (d <= 0) return <span className="idn-pill bad">expired</span>;
  return <span className={`idn-pill ${d <= 14 ? "warn" : "ok"}`} title={`Expires ${shortDate(k.expires_at)}`}>in {d} {d === 1 ? "day" : "days"}</span>;
}

function SaIcon({ sa, size = 16 }: { sa: ServiceAccount; size?: number }) {
  if (sa.kind === "legacy") return <Warning size={size} aria-hidden />;
  if (sa.kind === "scim") return <UsersThree size={size} aria-hidden />;
  if (sa.name.includes("github")) return <GitBranch size={size} aria-hidden />;
  return <Key size={size} aria-hidden />;
}

function Badge({ sa }: { sa: ServiceAccount }) {
  const now = useNow();
  if (sa.kind === "legacy") return <span className="idn-pill bad">legacy</span>;
  if (sa.status === "disabled") return <span className="idn-pill mute">disabled</span>;
  const next = sa.keys.filter((k) => k.status === "active" && k.expires_at).map((k) => daysUntil(k.expires_at, now) ?? 999).sort((a, b) => a - b)[0];
  if (next !== undefined && next <= 14) return <span className="idn-pill warn" title="Next key expiry">{next}d</span>;
  return <span className="idn-pill ok">ok</span>;
}

/** THE SHOWN-ONCE DIALOG. Cannot be dismissed until the person says they stored it. */
export function ShownOnce({ issued, account, onClose }: { issued: IssuedSecret; account: string; onClose: () => void }) {
  const [stored, setStored] = useState(false);
  const [copied, setCopied] = useState(false);
  const k = issued.key;
  return (
    <Sheet title="Copy your new key now" icon={<span className="idn-ok-ico"><CheckCircle size={22} weight="fill" aria-hidden /></span>}
      onClose={() => { if (stored) onClose(); }} locked={!stored} wide testId="shown-once">
      <p className="idn-lede2" data-copy="prose">This is the only time BOSS will show it. Store it in your CI's secrets, for example as <code className="idn-mono">BOSS_API_KEY</code>.</p>
      <div className="idn-secret">
        <code data-testid="secret" data-copy="data" aria-label="The new key">{issued.secret}</code>
        <button type="button" className="idn-btn sm primary" data-autofocus onClick={async () => {
          try { await navigator.clipboard.writeText(issued.secret); setCopied(true); showToast("success", "Copied"); }
          catch { showToast("error", "Copy blocked · select it"); }
        }}>
          {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="idn-note idn-mt" data-copy="prose">
        <Warning size={16} aria-hidden />
        <span>Close this and the key is gone for good: BOSS keeps only a hash. Lost it? Rotate.{issued.replaces ? " The old key keeps working for the overlap you chose, then stops." : ""}</span>
      </div>
      <dl className="idn-sum">
        <dt>Name</dt><dd data-copy="data">{k.name}{k.rotated_from ? " (rotated)" : null} · {account}</dd>
        <dt>Key id</dt><dd data-raw-ok title="The public part of the key: BOSS shows it in logs and lists"><code className="idn-kid">{k.id}</code></dd>
        <dt>Scopes</dt><dd>{k.scopes.map((p) => <Perm key={p} p={p} sm />)}</dd>
        <dt>Limits</dt><dd data-copy="data">{k.projects?.length ? `project ${k.projects.join(", ")}` : "all projects"}{k.networks ? ` · ${k.networks}` : ""} · {k.expires_at ? `expires ${shortDate(k.expires_at)}` : "no expiry"}</dd>
        {issued.replaces ? <><dt>Overlap</dt><dd data-copy="data">the old key {issued.replaces.stops_at ? <>stops <Ago iso={issued.replaces.stops_at} /></> : "stopped now"}</dd></> : null}
      </dl>
      <div className="idn-mf">
        <label className="idn-check"><input type="checkbox" checked={stored} onChange={(e) => setStored(e.target.checked)} data-testid="stored" /> I've stored this key somewhere safe</label>
        <span className="idn-spacer" />
        <button type="button" className="idn-btn primary" disabled={!stored} onClick={onClose} data-testid="done">Done</button>
      </div>
    </Sheet>
  );
}

type KeyDraft = { name: string; preset: Preset; scopes: Permission[]; projects: string; networks: string; days: number | null; replaces: string | null; overlap: number };

function KeyForm({ sa, held, noExpiryAllowed, maxDays, replacing, onCancel, onIssued }: {
  sa: ServiceAccount; held: Permission[]; noExpiryAllowed: boolean; maxDays: number; replacing: ApiKey | null;
  onCancel: () => void; onIssued: (i: IssuedSecret) => void;
}) {
  const guarded = useGuardedCall();
  const [d, setD] = useState<KeyDraft>(() => replacing
    ? { name: replacing.name.replace(/ \(rotated\)$/, ""), preset: "custom", scopes: replacing.scopes.filter((p) => held.includes(p)),
        projects: replacing.projects?.join(", ") ?? "", networks: replacing.networks ?? "", days: 90, replaces: replacing.id, overlap: 24 }
    : { name: "", preset: "ci", scopes: presetScopes("ci", held), projects: "", networks: "", days: 90, replaces: null, overlap: 24 });
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<KeyDraft>) => setD((x) => ({ ...x, ...patch }));
  const addable = held.filter((p) => !d.scopes.includes(p));
  const stepUpInScopes = d.scopes.filter((p) => PERMISSION_META[p].stepUp);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const issued = await guarded(`A key for ${sa.name} can act with its scopes.`, () =>
        idPost<IssuedSecret>(`/v1/identity/service-accounts/${encodeURIComponent(sa.id)}/keys`, {
          name: d.name.trim(), scopes: d.scopes,
          projects: d.projects.trim() ? d.projects.split(/[\s,]+/).filter(Boolean) : null,
          networks: d.networks.trim() || null, expires_days: d.days, replaces: d.replaces, overlap_hours: d.replaces ? d.overlap : 0,
        }));
      onIssued(issued);
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(message(e, "Not created"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="idn-card" aria-labelledby="idn-newkey-h" data-testid="key-form">
      <div className="idn-card-h">
        <h3 id="idn-newkey-h" title={replacing?.id}>{replacing ? `Rotate “${replacing.name}”` : `New key for ${sa.name}`}</h3>
        <span className="idn-sub" title="Never more than this account holds, or than you hold">Scopes ≤ {sa.name}'s</span>
      </div>
      <div className="idn-form">
        <div className="idn-field">
          <label htmlFor="key-name">Name</label>
          <input id="key-name" className="idn-input" value={d.name} onChange={(e) => set({ name: e.target.value })} placeholder="GitHub Actions · deploy" data-autofocus />
        </div>
        <div className="idn-field">
          <span className="idn-label" id="preset-l">Preset</span>
          <div className="idn-segs" role="group" aria-labelledby="preset-l">
            {PRESETS.map((p) => (
              <button key={p.id} type="button" aria-pressed={d.preset === p.id} title={p.tip}
                onClick={() => set({ preset: p.id, scopes: p.id === "custom" ? d.scopes : presetScopes(p.id, held) })}>{p.label}</button>
            ))}
          </div>
        </div>
        <div className="idn-field idn-full">
          <span className="idn-label">Scopes · {d.scopes.length} of {sa.name}'s {held.length}</span>
          <div className="idn-row idn-scopes-edit" data-testid="scopes">
            {d.scopes.map((p) => (
              <button key={p} type="button" className="idn-scope" title={`Remove ${p}`} aria-label={`Remove ${permissionName(p)}`}
                onClick={() => set({ preset: "custom", scopes: d.scopes.filter((x) => x !== p) })}>
                <Perm p={p} /> <X size={11} aria-hidden />
              </button>
            ))}
            <label className="idn-add-scope">
              <span className="idn-sr">Add a scope</span>
              <select value={adding} onChange={(e) => {
                const v = e.target.value;
                if (isPermission(v)) set({ preset: "custom", scopes: [...d.scopes, v] });
                setAdding("");
              }} disabled={!addable.length}>
                <option value="">+ Add</option>
                {addable.map((p) => <option key={p} value={p}>{permissionName(p)}</option>)}
              </select>
            </label>
          </div>
          <span className="idn-tipline" data-copy="prose"><Key size={11} className={stepUpInScopes.length ? "idn-warn" : undefined} aria-hidden /> Step-up permissions are unavailable to keys unless listed here.</span>
        </div>
        <div className="idn-field">
          <label htmlFor="key-projects">Projects</label>
          <input id="key-projects" className="idn-input" value={d.projects} onChange={(e) => set({ projects: e.target.value })} placeholder="all projects" />
        </div>
        <div className="idn-field">
          <label htmlFor="key-nets" title="IP ranges in CIDR form, e.g. 10.0.0.0/8">Allowed networks</label>
          <input id="key-nets" className="idn-input" value={d.networks} onChange={(e) => set({ networks: e.target.value })} placeholder="any · or CIDRs" />
        </div>
        <div className="idn-field">
          <span className="idn-label" id="exp-l">Expires</span>
          <div className="idn-segs" role="group" aria-labelledby="exp-l">
            {EXPIRY.filter((n) => n <= maxDays).map((n) => (
              <button key={n} type="button" aria-pressed={d.days === n} onClick={() => set({ days: n })}>{n} d</button>
            ))}
            <button type="button" aria-pressed={d.days === null} aria-disabled={!noExpiryAllowed || undefined}
              title={noExpiryAllowed ? "Flagged in posture" : "The security policy requires an expiry"}
              onClick={() => { if (noExpiryAllowed) set({ days: null }); }}>Never</button>
          </div>
        </div>
        {d.replaces ? (
          <div className="idn-field">
            <span className="idn-label" id="ovl-l">Old key keeps working for</span>
            <div className="idn-segs" role="group" aria-labelledby="ovl-l">
              {OVERLAP.map((o) => (
                <button key={o.h} type="button" aria-pressed={d.overlap === o.h} title={o.h ? "Both keys work until the overlap ends" : "The old key stops now"}
                  onClick={() => set({ overlap: o.h })}>{o.label}</button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn" onClick={onCancel}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={busy || !d.name.trim() || !d.scopes.length} onClick={create} data-testid="create-key">
          <Key size={14} aria-hidden /> {busy ? "Creating…" : replacing ? "Rotate key" : "Create key"}
        </button>
      </div>
    </section>
  );
}

/**
 * The accounts again, joined as the server page joins them (lib/identity/directory.ts): keys from
 * /service-accounts/{id}/keys, bindings from /role-bindings, owners named from what was shown.
 */
async function fetchServiceAccounts(roles: AccessRole[], shown: ServiceAccount[], people: PickPerson[] = []): Promise<ServiceAccount[]> {
  const [r, all] = await Promise.all([
    idGet<{ service_accounts: unknown[] }>("/v1/identity/service-accounts"),
    idGet<{ bindings: unknown[] }>("/v1/identity/role-bindings").catch(() => null),
  ]);
  const index = roleIndex(roles);
  const bindings = all ? (all.bindings ?? []).map((b) => adaptBinding(b, index)) : null;
  const names: NameIndex = new Map([...people.map((p) => [p.id, { name: p.name, hue: p.hue }] as const),
    ...shown.flatMap((s) => s.owners).map((o) => [o.id, { name: o.name, hue: o.hue }] as const)]);
  return Promise.all((r.service_accounts ?? []).map(async (raw) => {
    const id = String((raw as { id?: unknown }).id ?? "");
    const keys = await idGet<{ keys: ApiKey[] }>(`/v1/identity/service-accounts/${encodeURIComponent(id)}/keys`).then((k) => k.keys).catch(() => undefined);
    return adaptServiceAccount(raw, {
      keys, names, roles,
      bindings: bindings ? bindings.filter((b) => b.subject_id === id) : shown.find((s) => s.id === id)?.bindings,
    });
  }));
}

/** Roles a new account may hold: never Owner or a system role; reachable only if you hold all of it. */
function bindable(roles: AccessRole[]): AccessRole[] {
  return roles.filter((r) => r.key !== "owner" && !r.system);
}

function useHeld(): (r: AccessRole) => boolean {
  const { org } = useIdentity();
  return (r: AccessRole) => !org || r.permissions.every((p) => org.permissions.has(p));
}

type FirstKey = { name: string; preset: Exclude<Preset, "custom">; days: number };

function NewAccount({ roles, people, maxDays, onClose, onMade }: {
  roles: AccessRole[]; people: PickPerson[]; maxDays: number;
  onClose: () => void; onMade: (sa: { id: string }, issued: IssuedSecret | null) => void;
}) {
  const guarded = useGuardedCall();
  const { me } = useIdentity();
  const reachable = useHeld();
  const meId = me?.principal_id ?? null;
  const pickable: PickPerson[] = useMemo(() => {
    const list = [...people];
    if (meId && !list.some((p) => p.id === meId)) list.unshift({ id: meId, name: me?.display_name ?? "You", hue: 0, email: me?.email ?? null });
    return list;
  }, [people, meId, me]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owners, setOwners] = useState<string[]>(meId ? [meId] : []);
  const [role, setRole] = useState(() => bindable(roles).find((r) => r.key === "viewer" && reachable(r))?.key ?? "");
  const [withKey, setWithKey] = useState(false);
  const [key, setKey] = useState<FirstKey>({ name: "", preset: "ci", days: Math.min(90, maxDays) });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const chosen = roles.find((r) => r.key === role) ?? null;
  const keyScopes = chosen ? presetScopes(key.preset, chosen.permissions) : [];
  const ready = name.trim() && owners.length > 0 && (!withKey || (key.name.trim() && keyScopes.length > 0));

  const create = async () => {
    setBusy(true); setError(null);
    // Three calls (the API binds no role and issues no key on create). A retry after a step-up
    // skips what already happened, so nothing is made twice.
    let made: { id: string } | null = null;
    let bound = false;
    let issued: IssuedSecret | null = null;
    try {
      await guarded("A service account acts with its access role.", async () => {
        made ??= await idPost<{ id: string }>("/v1/identity/service-accounts", {
          name: name.trim(), description: description.trim() || null, owner_principal_ids: owners,
        });
        if (chosen && !bound) {
          await idPost("/v1/identity/role-bindings", { subject_type: "principal", subject_id: made.id, role_id: chosen.id });
          bound = true;
        }
        if (withKey && !issued) {
          issued = await idPost<IssuedSecret>(`/v1/identity/service-accounts/${encodeURIComponent(made.id)}/keys`, {
            name: key.name.trim(), scopes: keyScopes, projects: null, networks: null, expires_days: key.days,
          });
        }
      });
      onMade(made!, issued);
    } catch (e) {
      if (made) onMade(made, issued);
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(message(e, made ? "Created, but not finished" : "Not created"));
      setBusy(false);
    }
  };

  return (
    <Sheet title="New service account" icon={<Key size={18} aria-hidden />} onClose={onClose} wide testId="new-sa">
      <div className="idn-form">
        <div className="idn-field"><label htmlFor="sa-name">Name</label>
          <input id="sa-name" className="idn-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="ci-deployer" data-autofocus /></div>
        <div className="idn-field"><label htmlFor="sa-desc">What it's for</label>
          <input id="sa-desc" className="idn-input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Deploys from GitHub Actions" /></div>
        <div className="idn-field idn-full">
          <span className="idn-label" title="People accountable for it: they rotate and revoke its keys">Owners</span>
          <PeoplePick people={pickable} chosen={owners} onChange={setOwners} label="Add an owner" testId="sa-owners" />
        </div>
        <div className="idn-field idn-full">
          <span className="idn-label" id="sa-role-l">Access role</span>
          <div className="idn-segs" role="radiogroup" aria-labelledby="sa-role-l">
            {bindable(roles).map((r) => {
              const ok = reachable(r);
              return (
                <button key={r.id} type="button" role="radio" aria-checked={role === r.key} aria-pressed={role === r.key}
                  aria-disabled={!ok || undefined} title={ok ? r.description : "You don't hold all of it"}
                  onClick={() => { if (ok) setRole(r.key); }}><RoleChip roleKey={r.key} name={r.name} /></button>
              );
            })}
          </div>
        </div>
        <label className="idn-check idn-full"><input type="checkbox" checked={withKey} onChange={(e) => setWithKey(e.target.checked)} data-testid="sa-first-key" /> Create its first key now</label>
        {withKey ? (
          <>
            <div className="idn-field"><label htmlFor="sa-key-name">Key name</label>
              <input id="sa-key-name" className="idn-input" value={key.name} onChange={(e) => setKey({ ...key, name: e.target.value })} placeholder="GitHub Actions · deploy" /></div>
            <div className="idn-field">
              <span className="idn-label" id="sa-key-preset">Scopes</span>
              <div className="idn-segs" role="group" aria-labelledby="sa-key-preset">
                {PRESETS.filter((p) => p.id !== "custom").map((p) => (
                  <button key={p.id} type="button" aria-pressed={key.preset === p.id} title={p.tip}
                    onClick={() => setKey({ ...key, preset: p.id as FirstKey["preset"] })}>{p.label}</button>
                ))}
              </div>
              <span className="idn-dim idn-small">{keyScopes.length ? `${keyScopes.length} permissions from ${chosen?.name ?? "its role"}` : "This role gives this preset nothing"}</span>
            </div>
            <div className="idn-field">
              <span className="idn-label" id="sa-key-exp">Expires</span>
              <div className="idn-segs" role="group" aria-labelledby="sa-key-exp">
                {EXPIRY.filter((n) => n <= maxDays).map((n) => (
                  <button key={n} type="button" aria-pressed={key.days === n} onClick={() => setKey({ ...key, days: n })}>{n} d</button>
                ))}
              </div>
            </div>
          </>
        ) : null}
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-dim">{owners.length ? `${owners.length} ${owners.length === 1 ? "owner" : "owners"}` : "Add an owner"}</span>
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!ready || busy} onClick={create} data-testid="create-sa">{busy ? "Creating…" : "Create"}</button>
      </div>
    </Sheet>
  );
}

export function ServiceAccounts({ initial, roles, noExpiryAllowed, maxDays, selected, people = [] }: {
  initial: ServiceAccount[]; roles: AccessRole[]; noExpiryAllowed: boolean; maxDays: number; selected: string | null;
  /** Who may own an account (the directory, when the viewer can read it; else just them). */
  people?: PickPerson[];
}) {
  const guarded = useGuardedCall();
  const [list, setList] = useState(initial);
  const [current, setCurrent] = useState<string | null>(selected && initial.some((s) => s.id === selected) ? selected : initial[0]?.id ?? null);
  const [form, setForm] = useState<{ replacing: ApiKey | null } | null>(null);
  const [issued, setIssued] = useState<{ issued: IssuedSecret; account: string } | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [editing, setEditing] = useState<false | "all" | "owners">(false);
  const [deleting, setDeleting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [held, setHeld] = useState<Permission[]>([]);
  const [error, setError] = useState<string | null>(null);
  const sa = list.find((s) => s.id === current) ?? null;

  useEffect(() => {
    if (!sa) return;
    let live = true;
    idGet<{ permissions: string[] }>(`/v1/identity/service-accounts/${encodeURIComponent(sa.id)}/scopes`)
      .then((r) => { if (live) setHeld(r.permissions.filter(isPermission)); })
      .catch(() => {
        // Seam not answered: fall back to the roles this account is bound to.
        if (!live) return;
        const keys = new Set(sa.bindings.map((b) => b.role_key));
        setHeld([...new Set(roles.filter((r) => keys.has(r.key)).flatMap((r) => r.permissions))]);
      });
    return () => { live = false; };
  }, [sa, roles]);

  const reload = async (id: string | null = current) => {
    const next = (await fetchServiceAccounts(roles, list, people)).filter((x) => x.status !== "revoked");
    setList(next);
    setCurrent(id && next.some((x) => x.id === id) ? id : next[0]?.id ?? null);
  };
  const choose = (id: string) => {
    setCurrent(id); setForm(null); setError(null);
    try { const u = new URL(window.location.href); u.searchParams.set("sa", id); window.history.replaceState(null, "", u.toString()); } catch { /* fine */ }
  };

  const active = useMemo(() => sa?.keys.filter((k) => k.status !== "revoked") ?? [], [sa]);

  return (
    <div className="idn-sacols">
      <aside aria-labelledby="idn-sa-h">
        <div className="idn-sec-h">
          <h1 id="idn-sa-h" className="idn-h1">Service accounts</h1>
          <span className="idn-spacer" />
          <CanButton permission="service_account:manage" className="idn-btn sm primary" onClick={() => setCreating(true)} tip="An account for CI, scripts or integrations">
            <Plus size={13} weight="bold" aria-hidden /> New
          </CanButton>
        </div>
        {!list.length ? (
          <EmptyState compact icon={<Key size={18} />} title="No service accounts yet" testId="sa-empty">
            Give CI, scripts and integrations their own scoped keys, owned by a person.
          </EmptyState>
        ) : null}
        <div className="idn-sal" role="list">
          {list.map((x) => (
            <button key={x.id} type="button" role="listitem" className="idn-sai" aria-current={x.id === current ? "true" : undefined} onClick={() => choose(x.id)} data-testid={`sa-${x.name}`}>
              <span className={`idn-ico${x.kind === "legacy" ? " warn" : ""}`}><SaIcon sa={x} /></span>
              <span>
                <b>{x.name}</b>
                <small data-copy="data">{x.kind === "scim" ? "System · directory sync only" : x.kind === "legacy" ? x.description
                  : `Owner ${x.owners.map((o) => o.name).join(", ") || "none"} · ${keyCount(x.keys.filter((k) => k.status !== "revoked").length)}${x.bindings[0] ? ` · ${x.bindings[0].role_name}` : ""}`}</small>
              </span>
              <Badge sa={x} />
            </button>
          ))}
        </div>
        <p className="idn-note info idn-mt" data-copy="prose"><LockSimple size={14} aria-hidden /> Service accounts never sign in. Each has a human owner and authenticates only with keys you can scope, rotate and revoke.</p>
      </aside>

      {sa ? (
        <div className="idn-colr">
          <section className="idn-card" aria-labelledby="idn-sa-name">
            <div className="idn-dh2">
              <span className="idn-bigico"><SaIcon sa={sa} size={24} /></span>
              <div>
                <h2 id="idn-sa-name">{sa.name}</h2>
                <span className="idn-dim idn-small" data-copy="data">{sa.description}{sa.created_by ? ` · created by ${sa.created_by}, ${shortDate(sa.created_at)}` : ""}</span>
              </div>
              <span className="idn-spacer" />
              <CanButton permission="service_account:manage" className="idn-btn sm" onClick={() => setEditing("all")} disabled={sa.kind !== "service"} tip="Name, owners and access roles" data-testid="edit-sa-open">Edit</CanButton>
              <CanButton permission="service_account:manage" className="idn-btn sm danger" disabled={sa.kind !== "service"}
                tip={sa.status === "disabled" ? "Its keys work again" : "Its keys stop working"}
                onClick={async () => {
                  try {
                    await guarded(`${sa.status === "disabled" ? "Enabling" : "Disabling"} ${sa.name} changes what its keys can do.`,
                      () => idPost(`/v1/identity/service-accounts/${encodeURIComponent(sa.id)}/${sa.status === "disabled" ? "enable" : "disable"}`, {}));
                    await reload();
                    showToast("success", sa.status === "disabled" ? "Enabled" : "Disabled");
                  } catch (e) { setError(message(e, "Not changed")); }
                }}>
                <Prohibit size={13} aria-hidden /> {sa.status === "disabled" ? "Enable" : "Disable"}
              </CanButton>
              <CanButton permission="service_account:manage" className="idn-btn sm danger" disabled={sa.kind !== "service"}
                onClick={() => setDeleting(true)} tip="Deletes it; every key stops" aria-label={`Delete ${sa.name}`} data-testid="delete-sa-open">
                <Trash size={13} aria-hidden />
              </CanButton>
            </div>
            <div className="idn-facts">
              <div><span className="idn-label">Owners</span>
                <span>{sa.owners.length ? sa.owners.map((o) => <React.Fragment key={o.id}><Face name={o.name} tone={o.hue} size={22} /> {o.name}</React.Fragment>) : (
                  <>
                    <span className="idn-warn" title="Nobody is accountable for its keys">No owner</span>
                    {sa.kind === "service" ? (
                      <CanButton permission="service_account:manage" className="idn-btn sm" onClick={() => setEditing("owners")} tip="Someone accountable for its keys" data-testid="add-owner">
                        <Plus size={12} aria-hidden /> Add owner
                      </CanButton>
                    ) : null}
                  </>
                )}</span></div>
              <div><span className="idn-label">Access role</span>
                <span>{sa.bindings.length ? sa.bindings.map((b) => <RoleChip key={b.id} roleKey={b.role_key} name={b.role_name} scope={b.scope} />) : <span className="idn-dim">{sa.kind === "scim" ? "Directory sync only" : "none"}</span>}</span></div>
              <div><span className="idn-label">Effective</span>
                <span title="Each key gets only the part its scopes allow">{sa.effective} permissions, narrowed per key</span></div>
              <div><span className="idn-label">Last used</span>
                <span>{sa.last_used ? <><Ago iso={sa.last_used.at} /> · <span data-copy="data">{sa.last_used.client}</span></> : "never"}</span></div>
            </div>
            <ErrorLine error={error} />

            <div className="idn-card-h">
              <h3>Keys</h3>
              <span className="idn-sub" title="BOSS never shows a key again after it's created">Shown once, then only its name</span>
              <span className="idn-spacer" />
              <CanButton permission="service_account_key:manage" className="idn-btn sm primary" disabled={sa.kind !== "service"}
                onClick={() => setForm({ replacing: null })} data-testid="new-key" tip="Scopes never exceed what this account holds">
                <Plus size={13} weight="bold" aria-hidden /> New key
              </CanButton>
            </div>
            {sa.kind === "legacy" ? (
              <div className="idn-note" data-copy="prose"><Warning size={15} aria-hidden /> The shared BOSS_API_KEY maps here until it is retired. Rotate it into a real key with New key on a service account.</div>
            ) : null}
            {active.length ? (
              <div className="idn-scroll">
                <table className="idn-t idn-keys" aria-label={`Keys of ${sa.name}`}>
                  <thead><tr><th>Key</th><th>Scopes</th><th>Projects</th><th>Last used</th><th>Expires</th><th><span className="idn-sr">Actions</span></th></tr></thead>
                  <tbody>
                    {active.map((k) => (
                      <tr key={k.id} data-key={k.id} data-selected={form?.replacing?.id === k.id || undefined}>
                        <td><span className="idn-kname" data-copy="data" title={`Key ${k.id}`}>{k.name}</span></td>
                        <td><span className="idn-scopes">{k.scopes.map((p) => <Perm key={p} p={p} sm />)}</span></td>
                        <td>{k.projects?.join(", ") ?? "all"}</td>
                        <td className="idn-small">{k.last_used_at ? <><Ago iso={k.last_used_at} />{k.last_used_ip ? <span className="idn-mono idn-dim"> · {k.last_used_ip}</span> : null}</> : "never"}</td>
                        <td><Expires k={k} /></td>
                        <td>
                          <span className="idn-row idn-nowrap">
                            <CanButton permission="service_account_key:manage" className="idn-btn sm" onClick={() => setForm({ replacing: k })} tip="A new key with these scopes, with an overlap">
                              <ArrowsClockwise size={13} aria-hidden /> Rotate
                            </CanButton>
                            <CanButton permission="service_account_key:manage" className="idn-btn sm danger" onClick={() => setRevoking(k)} aria-label={`Revoke ${k.name}`} tip="Stops working immediately">
                              <Prohibit size={13} aria-hidden />
                            </CanButton>
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : sa.kind === "service" ? (
              <EmptyState compact icon={<Key size={16} />} title="No keys yet" testId="keys-empty">New key makes one; it is shown once.</EmptyState>
            ) : <span className="idn-dim">No keys</span>}
          </section>

          {form ? (
            <KeyForm key={`${form.replacing?.id ?? "new"}-${held.length}`} sa={sa} held={held} noExpiryAllowed={noExpiryAllowed} maxDays={maxDays} replacing={form.replacing}
              onCancel={() => setForm(null)}
              onIssued={(i) => { setIssued({ issued: i, account: sa.name }); setForm(null); reload().catch(() => undefined); }} />
          ) : null}
        </div>
      ) : (
        <EmptyState icon={<Key size={22} />} title="Nothing selected" testId="sa-none"
          action={<CanButton permission="service_account:manage" className="idn-btn primary" onClick={() => setCreating(true)} tip="An account for CI, scripts or integrations" data-testid="sa-empty-create"><Plus size={13} weight="bold" aria-hidden /> New service account</CanButton>}>
          A service account never signs in: it has an owner and keys you scope, rotate and revoke.
        </EmptyState>
      )}

      {issued ? <ShownOnce issued={issued.issued} account={issued.account} onClose={() => setIssued(null)} /> : null}
      {revoking && sa ? (
        <Sheet title={`Revoke “${revoking.name}”?`} icon={<Prohibit size={18} className="idn-bad" aria-hidden />} onClose={() => setRevoking(null)} testId="revoke-key">
          <p className="idn-lede2" data-copy="prose">“{revoking.name}” stops working immediately, everywhere it is used. This cannot be undone.</p>
          <div className="idn-mf">
            <span className="idn-spacer" />
            <button type="button" className="idn-btn ghost" onClick={() => setRevoking(null)}>Cancel</button>
            <button type="button" className="idn-btn danger solid" data-autofocus onClick={async () => {
              try {
                await guarded(`Revoking “${revoking.name}” stops it immediately.`, () =>
                  idPost(`/v1/identity/service-accounts/${encodeURIComponent(sa.id)}/keys/${encodeURIComponent(revoking.id)}/revoke`, {}));
                setRevoking(null);
                await reload();
                showToast("success", "Key revoked");
              } catch (e) { setError(message(e, "Not revoked")); setRevoking(null); }
            }}>Revoke key</button>
          </div>
        </Sheet>
      ) : null}
      {editing && sa ? (
        <EditAccount sa={sa} roles={roles} people={people} focus={editing === "owners" ? "owners" : undefined}
          onClose={() => setEditing(false)} onSaved={() => { setEditing(false); reload().catch(() => undefined); }} />
      ) : null}
      {deleting && sa ? (
        <DeleteAccount sa={sa} onClose={() => setDeleting(false)} onDeleted={() => { setDeleting(false); reload(null).catch(() => undefined); }} />
      ) : null}
      {creating ? (
        <NewAccount roles={roles} people={people} maxDays={maxDays} onClose={() => setCreating(false)}
          onMade={(made, first) => {
            setCreating(false);
            const name = list.find((x) => x.id === made.id)?.name;
            if (first) setIssued({ issued: first, account: name ?? first.key.owner.name });
            reload(made.id).catch(() => undefined);
          }} />
      ) : null}
    </div>
  );
}

function EditAccount({ sa, roles, people, focus, onClose, onSaved }: {
  sa: ServiceAccount; roles: AccessRole[]; people: PickPerson[]; focus?: "owners";
  onClose: () => void; onSaved: () => void;
}) {
  const guarded = useGuardedCall();
  const reachable = useHeld();
  const pickable: PickPerson[] = useMemo(() => {
    const list = [...people];
    for (const o of sa.owners) if (!list.some((p) => p.id === o.id)) list.push({ id: o.id, name: o.name, hue: o.hue });
    return list;
  }, [people, sa.owners]);
  const [name, setName] = useState(sa.name);
  const [description, setDescription] = useState(sa.description);
  const [owners, setOwners] = useState<string[]>(sa.owners.map((o) => o.id));
  const boundIds = sa.bindings.filter((b) => !b.synthetic && b.scope === "org").map((b) => b.role_id);
  const [chosen, setChosen] = useState<string[]>(boundIds);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toggle = (id: string) => setChosen((xs) => (xs.includes(id) ? xs.filter((x) => x !== id) : [...xs, id]));
  const ownersChanged = owners.join() !== sa.owners.map((o) => o.id).join();

  const save = async () => {
    setBusy(true); setError(null);
    const add = chosen.filter((id) => !boundIds.includes(id));
    const drop = sa.bindings.filter((b) => !b.synthetic && b.scope === "org" && !chosen.includes(b.role_id));
    const done = new Set<string>();
    try {
      await guarded("Changing a service account changes what its keys can do.", async () => {
        if (!done.has("patch")) {
          await idPatch(`/v1/identity/service-accounts/${encodeURIComponent(sa.id)}`, {
            name: name.trim(), description, ...(ownersChanged ? { owner_principal_ids: owners } : {}),
          });
          done.add("patch");
        }
        for (const role_id of add) {
          if (done.has(`+${role_id}`)) continue;
          await idPost("/v1/identity/role-bindings", { subject_type: "principal", subject_id: sa.id, role_id });
          done.add(`+${role_id}`);
        }
        for (const b of drop) {
          if (done.has(`-${b.id}`)) continue;
          await idDelete(`/v1/identity/role-bindings/${encodeURIComponent(b.id)}`);
          done.add(`-${b.id}`);
        }
      });
      showToast("success", "Saved");
      onSaved();
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(message(e, "Not saved"));
      setBusy(false);
    }
  };

  return (
    <Sheet title={`Edit ${sa.name}`} onClose={onClose} wide testId="edit-sa">
      <div className="idn-form">
        <div className="idn-field"><label htmlFor="edit-name">Name</label>
          <input id="edit-name" className="idn-input" value={name} onChange={(e) => setName(e.target.value)} data-autofocus={focus ? undefined : true} /></div>
        <div className="idn-field"><label htmlFor="edit-desc">What it's for</label>
          <input id="edit-desc" className="idn-input" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
        <div className="idn-field idn-full">
          <span className="idn-label" title="People accountable for it: they rotate and revoke its keys">Owners</span>
          <PeoplePick people={pickable} chosen={owners} onChange={setOwners} label="Add an owner" testId="edit-owners" />
        </div>
        <div className="idn-field idn-full">
          <span className="idn-label" id="edit-roles-l" title="Its keys can never hold more than these">Access roles</span>
          <div className="idn-segs" role="group" aria-labelledby="edit-roles-l" data-testid="edit-roles">
            {bindable(roles).map((r) => {
              const on = chosen.includes(r.id);
              const ok = reachable(r);
              return (
                <button key={r.id} type="button" aria-pressed={on} aria-disabled={!ok || undefined}
                  title={ok ? r.description : "You don't hold all of it"} data-role={r.key}
                  onClick={() => { if (ok) toggle(r.id); }}><RoleChip roleKey={r.key} name={r.name} /></button>
              );
            })}
          </div>
        </div>
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        {!owners.length ? <span className="idn-warn">Needs at least one owner</span> : null}
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!name.trim() || !owners.length || busy} onClick={save} data-testid="save-sa">{busy ? "Saving…" : "Save"}</button>
      </div>
    </Sheet>
  );
}

function DeleteAccount({ sa, onClose, onDeleted }: { sa: ServiceAccount; onClose: () => void; onDeleted: () => void }) {
  const guarded = useGuardedCall();
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const live = sa.keys.filter((k) => k.status !== "revoked").length;
  return (
    <Sheet title={`Delete ${sa.name}?`} icon={<Trash size={18} className="idn-bad" aria-hidden />} onClose={onClose} testId="delete-sa">
      <p className="idn-lede2" data-copy="prose">
        {live ? `Its ${live} ${live === 1 ? "key stops" : "keys stop"} working immediately, everywhere they are used.` : "It has no working keys."} This cannot be undone.
      </p>
      <div className="idn-field">
        <label htmlFor="delete-sa-name">Type {sa.name} to confirm</label>
        <input id="delete-sa-name" className="idn-input" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" data-autofocus />
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger solid" disabled={typed !== sa.name || busy} data-testid="delete-sa-confirm" onClick={async () => {
          setBusy(true); setError(null);
          try {
            await guarded(`Deleting ${sa.name} stops all its keys.`, () => idDelete(`/v1/identity/service-accounts/${encodeURIComponent(sa.id)}`));
            showToast("success", "Service account deleted");
            onDeleted();
          } catch (e) {
            if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(message(e, "Not deleted"));
            setBusy(false);
          }
        }}>Delete for good</button>
      </div>
    </Sheet>
  );
}
