"use client";

/**
 * MY ACCOUNT › API KEYS (IDENTITY.md §5): personal access tokens for the CLI and MCP clients.
 * A key is named by its random public id and never by any part of its secret (D20); its scopes
 * can never exceed what you hold, and they shrink when your access does. The secret is shown
 * once, in the same dialog service-account keys use, and cannot be dismissed until stored.
 */
import React, { useMemo, useState } from "react";
import { ArrowsClockwise, Key, MagnifyingGlass, Plus, Prohibit } from "@phosphor-icons/react";

import { IdentityError, idGet, idPost } from "../../../lib/identity/api";
import { PERMISSION_META, PERMISSIONS, type Permission } from "../../../lib/identity/permissions.gen";
import type { ApiKey, IssuedSecret } from "../../../lib/identity/types";
import { CanButton, useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { Ago, ErrorLine, Perm, Pill, Sheet, daysUntil, useNow } from "../../../components/identity/kit";
import { ShownOnce } from "../../identity/service-accounts/service-accounts";
import { EmptyState } from "../../../components/identity/states";
import { ListHeader } from "../../../components/identity/list-header";
import { permissionName } from "../../../lib/identity/words";
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
  const [rotating, setRotating] = useState<ApiKey | null>(null);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const reload = async () => setKeys((await idGet<{ keys: ApiKey[] }>("/v1/identity/me/api-keys")).keys);
  const newKey = (
    <CanButton permission="api_key:manage_own" className="idn-btn primary sm" onClick={() => setCreating(true)} data-testid="new-key" tip="For the BOSS CLI or an MCP client">
      <Plus size={14} weight="bold" aria-hidden /> New key
    </CanButton>
  );

  return (
    <section className="idn-card" aria-labelledby="keys-h">
      <ListHeader level={2} id="keys-h" title="API keys" sub="Shown once, then only its name" empty={!keys.length} create={newKey} />
      {keys.length ? (
        <div className="idn-scroll">
          <table className="idn-t" data-testid="my-keys">
            <thead><tr><th>Key</th><th>Scopes</th><th>Last used</th><th>Expires</th><th><span className="idn-sr">Actions</span></th></tr></thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id} data-status={k.status}>
                  <td><span data-copy="data" title={`Key ${k.id}`}>{k.name}</span>{k.projects?.length || k.networks ? <small className="idn-block idn-dim" data-copy="data">{[k.projects?.length ? k.projects.join(", ") : null, k.networks].filter(Boolean).join(" · ")}</small> : null}</td>
                  <td><span className="idn-scopes">{k.scopes.slice(0, 4).map((p) => <Perm key={p} p={p} sm />)}{k.scopes.length > 4 ? <span className="idn-chip">+{k.scopes.length - 4}</span> : null}</span></td>
                  <td className="idn-mut" data-copy="data">{k.last_used_at ? <><Ago iso={k.last_used_at} />{k.last_used_client ? ` · ${k.last_used_client}` : ""}</> : "never"}</td>
                  <td>{keyStatus(k, now)}</td>
                  <td>{k.status === "active" ? (
                    <span className="idn-row idn-nowrap">
                      <CanButton permission="api_key:manage_own" className="idn-btn sm" onClick={() => setRotating(k)} tip="A new secret; the old one can overlap" data-testid={`rotate-${k.name}`}>
                        <ArrowsClockwise size={13} aria-hidden /> Rotate
                      </CanButton>
                      <button type="button" className="idn-btn sm ghost" aria-label={`Revoke ${k.name}`} title="Revoke" onClick={() => setRevoking(k)}><Prohibit size={14} aria-hidden /></button>
                    </span>
                  ) : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState icon={<Key size={20} />} title="No personal keys yet" testId="keys-empty"
          action={newKey}>
          A key lets the BOSS CLI or an MCP client act as you, with only the scopes you pick.
        </EmptyState>
      )}
      <ErrorLine error={error} />

      {creating ? <NewKeySheet onClose={() => setCreating(false)} onIssued={(i) => { setCreating(false); setIssued(i); reload().catch(() => undefined); }} /> : null}
      {issued ? <ShownOnce issued={issued} account="personal" onClose={() => setIssued(null)} /> : null}
      {rotating ? <RotateSheet k={rotating} onClose={() => setRotating(null)} onIssued={(i) => { setRotating(null); setIssued(i); reload().catch(() => undefined); }} /> : null}
      {revoking ? (
        <Sheet title={`Revoke “${revoking.name}”?`} icon={<Prohibit size={18} className="idn-bad" aria-hidden />} onClose={() => setRevoking(null)} testId="revoke-key">
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
  const [projects, setProjects] = useState("");
  const [networks, setNetworks] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scopes = preset === "custom" ? custom : presetScopes(preset, held);
  const shown = held.filter((p) => !filter || `${p} ${PERMISSION_META[p].title}`.toLowerCase().includes(filter.toLowerCase()));

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      onIssued(await guarded("Creating a key that acts as you.", () =>
        idPost<IssuedSecret>("/v1/identity/me/api-keys", {
          name: name.trim(), scopes, expires_days: days,
          projects: projects.trim() ? projects.split(/[\s,]+/).filter(Boolean) : null,
          networks: networks.trim() || null,
        })));
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
                    title={p} onClick={() => setCustom((xs) => (xs.includes(p) ? xs.filter((x) => x !== p) : [...xs, p]))}>
                    <span className="idn-sw" aria-hidden /> {permissionName(p)}
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
          <label htmlFor="key-projects" title="Project ids, comma-separated">Projects</label>
          <input id="key-projects" className="idn-input" value={projects} onChange={(e) => setProjects(e.target.value)} placeholder="all projects" data-testid="key-projects" />
        </div>
        <div className="idn-field">
          <label htmlFor="key-nets" title="IP ranges in CIDR form, e.g. 10.0.0.0/8">Allowed networks</label>
          <input id="key-nets" className="idn-input" value={networks} onChange={(e) => setNetworks(e.target.value)} placeholder="any · or CIDRs" data-testid="key-networks" />
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

const GRACE: { id: "0" | "1h" | "24h" | "7d"; label: string }[] = [
  { id: "0", label: "None" }, { id: "1h", label: "1 h" }, { id: "24h", label: "24 h" }, { id: "7d", label: "7 d" },
];

/** Rotate a personal key: a new secret with the same scopes and limits; the old one may overlap. */
function RotateSheet({ k, onClose, onIssued }: { k: ApiKey; onClose: () => void; onIssued: (i: IssuedSecret) => void }) {
  const guarded = useGuardedCall();
  const [grace, setGrace] = useState<(typeof GRACE)[number]["id"]>("1h");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title={`Rotate “${k.name}”`} icon={<ArrowsClockwise size={18} aria-hidden />} onClose={onClose} testId="rotate-key">
      <p className="idn-lede2" data-copy="prose">You get a new secret with the same scopes. Update wherever the old one is used before the overlap ends.</p>
      <div className="idn-field">
        <span className="idn-label" id="rot-l">Old key keeps working for</span>
        <div className="idn-segs" role="radiogroup" aria-labelledby="rot-l">
          {GRACE.map((g) => (
            <button key={g.id} type="button" role="radio" aria-checked={grace === g.id} aria-pressed={grace === g.id}
              title={g.id === "0" ? "The old key stops now" : "Both keys work until the overlap ends"} onClick={() => setGrace(g.id)}>{g.label}</button>
          ))}
        </div>
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={busy} data-testid="rotate-confirm" onClick={async () => {
          setBusy(true); setError(null);
          try {
            onIssued(await guarded("Rotating makes a new secret that acts as you.", () =>
              idPost<IssuedSecret>(`/v1/identity/me/api-keys/${encodeURIComponent(k.id)}/rotate`, { grace })));
          } catch (e) {
            if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(said(e));
            setBusy(false);
          }
        }}><ArrowsClockwise size={14} aria-hidden /> Rotate key</button>
      </div>
    </Sheet>
  );
}
