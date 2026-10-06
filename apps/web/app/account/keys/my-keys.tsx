"use client";

/**
 * MY ACCOUNT › API KEYS (IDENTITY.md §5): personal access tokens for the CLI and MCP clients.
 * A key is named by its random public id and never by any part of its secret (D20); its scopes
 * can never exceed what you hold, and they shrink when your access does. The secret is shown
 * once, in the same dialog service-account keys use, and cannot be dismissed until stored.
 */
import React, { useMemo, useState } from "react";
import { Key, MagnifyingGlass, Plus, Prohibit } from "@phosphor-icons/react";

import { IdentityError, idGet, idPost } from "../../../lib/identity/api";
import { PERMISSION_META, PERMISSIONS, type Permission } from "../../../lib/identity/permissions.gen";
import type { ApiKey, IssuedSecret } from "../../../lib/identity/types";
import { CanButton, useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { Ago, ErrorLine, Perm, Pill, Sheet, daysUntil, useNow } from "../../../components/identity/kit";
import { ShownOnce } from "../../identity/service-accounts/service-accounts";
import { showToast } from "../../client-forms";

type Preset = "read" | "mcp" | "ci" | "custom";
const PRESETS: { id: Preset; label: string; tip: string }[] = [
  { id: "read", label: "Read-only", tip: "Every read permission you hold" },
  { id: "mcp", label: "MCP client", tip: "What agents may hold, minus critical ones" },
  { id: "ci", label: "CI deploy", tip: "Deploy and read builds" },
  { id: "custom", label: "Custom", tip: "Pick each permission" },
];
const CI: Permission[] = ["deployment:create", "deployment:read", "code_task:read"];

/** The scopes a preset means, limited to what the owner holds (§5.2). */
export function presetScopes(preset: Preset, held: readonly Permission[]): Permission[] {
  if (preset === "read") return held.filter((p) => p.endsWith(":read"));
  if (preset === "mcp") return held.filter((p) => PERMISSION_META[p].delegable && !PERMISSION_META[p].critical);
  if (preset === "ci") return CI.filter((p) => held.includes(p));
  return [];
}

function said(e: unknown): string {
  return e instanceof IdentityError ? e.message : "That didn't work";
}

function keyStatus(k: ApiKey, now: number) {
  if (k.status === "revoked") return <Pill tone="mute">revoked</Pill>;
  if (k.status === "expired") return <Pill tone="mute">expired</Pill>;
  if (k.status === "grace") return <Pill tone="mute">grace</Pill>;
  const d = daysUntil(k.expires_at, now);
  if (d !== null && d <= 14) return <Pill tone="warn">in {d} days</Pill>;
  return <Pill tone="ok">{d !== null ? `in ${d} days` : "no expiry"}</Pill>;
}

export function MyKeys({ keys: initial }: { keys: ApiKey[] }) {
  const now = useNow();
  const [keys, setKeys] = useState(initial);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<IssuedSecret | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const reload = async () => setKeys((await idGet<{ keys: ApiKey[] }>("/v1/identity/me/api-keys")).keys);

  return (
    <section className="idn-card" aria-labelledby="keys-h">
      <div className="idn-card-h">
        <h2 id="keys-h">API keys</h2>
        <span className="idn-sub">Shown once, then only its id</span>
        <span className="idn-spacer" />
        <CanButton permission="api_key:manage_own" className="idn-btn primary sm" onClick={() => setCreating(true)} data-testid="new-key" tip="For the boss CLI or an MCP client">
          <Plus size={14} weight="bold" aria-hidden /> New key
        </CanButton>
      </div>
      {keys.length ? (
        <div className="idn-scroll">
          <table className="idn-t" data-testid="my-keys">
            <thead><tr><th>Key</th><th>Scopes</th><th>Last used</th><th>Expires</th><th><span className="idn-sr">Actions</span></th></tr></thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id} data-status={k.status}>
                  <td><code className="idn-kid">{k.id}</code><span data-copy="data">{k.name}</span></td>
                  <td><span className="idn-scopes">{k.scopes.slice(0, 4).map((p) => <Perm key={p} p={p} sm />)}{k.scopes.length > 4 ? <span className="idn-chip">+{k.scopes.length - 4}</span> : null}</span></td>
                  <td className="idn-mut" data-copy="data">{k.last_used_at ? <><Ago iso={k.last_used_at} />{k.last_used_client ? ` · ${k.last_used_client}` : ""}</> : "never"}</td>
                  <td>{keyStatus(k, now)}</td>
                  <td>{k.status === "active" ? (
                    <button type="button" className="idn-btn sm ghost" aria-label={`Revoke ${k.id}`} title="Revoke" onClick={() => setRevoking(k)}><Prohibit size={14} aria-hidden /></button>
                  ) : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="idn-alone"><Key size={20} aria-hidden /> <span>No personal keys yet</span></div>}
      <ErrorLine error={error} />

      {creating ? <NewKeySheet onClose={() => setCreating(false)} onIssued={(i) => { setCreating(false); setIssued(i); reload().catch(() => undefined); }} /> : null}
      {issued ? <ShownOnce issued={issued} account="personal" onClose={() => setIssued(null)} /> : null}
      {revoking ? (
        <Sheet title={`Revoke ${revoking.id}?`} icon={<Prohibit size={18} className="idn-bad" aria-hidden />} onClose={() => setRevoking(null)} testId="revoke-key">
          <p className="idn-lede2" data-copy="prose">Anything using it stops at its next call. This can&apos;t be undone.</p>
          <div className="idn-mf">
            <span className="idn-spacer" />
            <button type="button" className="idn-btn ghost" onClick={() => setRevoking(null)}>Cancel</button>
            <button type="button" className="idn-btn danger solid" onClick={() => {
              const k = revoking;
              guarded("Revoking a key.", () => idPost(`/v1/identity/me/api-keys/${encodeURIComponent(k.id)}/revoke`, {}))
                .then(() => { setRevoking(null); showToast("success", "Key revoked"); return reload(); })
                .catch((e) => { setRevoking(null); setError(said(e)); });
            }}>Revoke</button>
          </div>
        </Sheet>
      ) : null}
    </section>
  );
}

function NewKeySheet({ onClose, onIssued }: { onClose: () => void; onIssued: (i: IssuedSecret) => void }) {
  const { org } = useIdentity();
  const guarded = useGuardedCall();
  const held = useMemo(() => PERMISSIONS.filter((p) => !PERMISSION_META[p].systemOnly && (!org || org.permissions.has(p))), [org]);
  const [name, setName] = useState("");
  const [preset, setPreset] = useState<Preset>("read");
  const [custom, setCustom] = useState<Permission[]>([]);
  const [filter, setFilter] = useState("");
  const [days, setDays] = useState(90);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scopes = preset === "custom" ? custom : presetScopes(preset, held);
  const shown = held.filter((p) => !filter || p.includes(filter.toLowerCase()));

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      onIssued(await guarded("Creating a key that acts as you.", () =>
        idPost<IssuedSecret>("/v1/identity/me/api-keys", { name: name.trim(), scopes, projects: null, expires_days: days })));
    } catch (e) {
      setError(said(e));
      setBusy(false);
    }
  };

  return (
    <Sheet title="New personal key" icon={<Key size={18} aria-hidden />} onClose={onClose} wide testId="new-key-sheet">
      <p className="idn-lede2" data-copy="prose">It acts as you, limited to these scopes, and never more than you hold at each call.</p>
      <div className="idn-form">
        <div className="idn-field">
          <label htmlFor="key-name">Name</label>
          <input id="key-name" className="idn-input" placeholder="Claude Code" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} data-autofocus />
        </div>
        <div className="idn-field">
          <span className="idn-label" id="key-preset">Preset</span>
          <div className="idn-segs" role="radiogroup" aria-labelledby="key-preset">
            {PRESETS.map((p) => (
              <button key={p.id} type="button" role="radio" aria-checked={preset === p.id} aria-pressed={preset === p.id} title={p.tip}
                onClick={() => { setPreset(p.id); if (p.id === "custom" && !custom.length) setCustom(presetScopes("read", held).slice(0, 3)); }}>{p.label}</button>
            ))}
          </div>
        </div>
        <div className="idn-field idn-full">
          <span className="idn-label">Scopes · {scopes.length} of yours {held.length}</span>
          {preset === "custom" ? (
            <>
              <div className="idn-fb"><span className="idn-q"><MagnifyingGlass size={14} aria-hidden />
                <input aria-label="Filter permissions" placeholder="Filter permissions" value={filter} onChange={(e) => setFilter(e.target.value)} /></span></div>
              <div className="idn-key-picker" role="group" aria-label="Scopes">
                {shown.map((p) => (
                  <button key={p} type="button" className="idn-tg" aria-pressed={custom.includes(p)} data-crit={PERMISSION_META[p].critical || undefined}
                    title={PERMISSION_META[p].title} onClick={() => setCustom((xs) => (xs.includes(p) ? xs.filter((x) => x !== p) : [...xs, p]))}>
                    <span className="idn-sw" aria-hidden /> {p}
                  </button>
                ))}
              </div>
            </>
          ) : (
            <div className="idn-need" data-testid="key-scopes">
              {scopes.slice(0, 14).map((p) => <Perm key={p} p={p} sm />)}
              {scopes.length > 14 ? <span className="idn-chip">+{scopes.length - 14}</span> : null}
            </div>
          )}
          <span className="idn-dim idn-hint" data-copy="prose">Step-up permissions don&apos;t work from a key unless the key is a service account&apos;s.</span>
        </div>
        <div className="idn-field">
          <span className="idn-label" id="key-exp">Expires</span>
          <div className="idn-segs" role="radiogroup" aria-labelledby="key-exp">
            {[30, 90, 180, 365].map((n) => (
              <button key={n} type="button" role="radio" aria-checked={days === n} aria-pressed={days === n} onClick={() => setDays(n)}>{n} d</button>
            ))}
            <button type="button" aria-disabled title="Personal keys always expire">Never</button>
          </div>
        </div>
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!name.trim() || !scopes.length || busy} onClick={create} data-testid="create-key">
          <Key size={14} aria-hidden /> {busy ? "Creating…" : "Create key"}
        </button>
      </div>
    </Sheet>
  );
}
