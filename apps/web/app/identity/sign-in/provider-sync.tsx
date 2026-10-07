"use client";

/**
 * A PROVIDER'S GROUPS AND DIRECTORY SYNC (IDENTITY.md §9.8, §4.9): which IdP groups land in which
 * BOSS group at sign-in, and the SCIM tokens the IdP uses to push people and groups. A SCIM token
 * is shown once; BOSS keeps only a hash. Reads GET /v1/identity/providers/{id} (mappings and
 * keys); writes PATCH …/{id} (group_mappings, groups_claim) and …/scim-keys.
 */
import React, { useEffect, useState } from "react";
import { ArrowsClockwise, CheckCircle, Copy, Check, Plus, Trash, UsersThree, Warning } from "@phosphor-icons/react";

import { CanButton, useGuardedCall } from "../../../components/identity/identity-provider";
import { Ago, ErrorLine, Pill, Sheet, shortDate } from "../../../components/identity/kit";
import { EmptyState, ErrorState, type ReadFailure } from "../../../components/identity/states";
import { IdentityError, idDelete, idGet, idPatch, idPost } from "../../../lib/identity/api";
import type { ApiKey } from "../../../lib/identity/types";
import { scimPrincipalId, type GroupMapping, type IssuedScimKey, type ProviderDetail, type ScimKey } from "../../../lib/identity/types-sso";
import { showToast } from "../../client-forms";
import { CopyRow } from "./provider-sheet";

type GroupRow = { id: string; name: string };
type Row = { external: string; group: string };

const failureOf = (e: unknown): ReadFailure => e instanceof IdentityError
  ? { status: e.status, code: e.code, permission: e.permission }
  : { status: 0, code: "identity_unavailable" };

/** The shown-once dialog for a SCIM token: it cannot be closed until the admin says it's stored. */
export function ScimShownOnce({ issued, provider, onClose }: { issued: IssuedScimKey; provider: string; onClose: () => void }) {
  const [stored, setStored] = useState(false);
  const [copied, setCopied] = useState(false);
  return (
    <Sheet title="Copy the sync token now" icon={<CheckCircle size={20} weight="fill" aria-hidden />} onClose={() => { if (stored) onClose(); }}
      locked={!stored} wide testId="scim-shown-once">
      <p className="idn-lede2" data-copy="prose">Paste it into {provider}'s provisioning settings as the bearer token. BOSS shows it only this once.</p>
      <div className="idn-secret" data-raw-ok>
        <code data-testid="scim-secret" data-copy="data" aria-label="The sync token">{issued.key}</code>
        <button type="button" className="idn-btn sm primary" data-autofocus onClick={async () => {
          try { await navigator.clipboard.writeText(issued.key); setCopied(true); showToast("success", "Copied"); }
          catch { showToast("error", "Copy blocked · select it"); }
        }}>
          {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="idn-note idn-mt" data-copy="prose">
        <Warning size={16} aria-hidden />
        <span>Close this and it is gone for good. Lost it? Make a new one and revoke this.{issued.expires_at ? ` Expires ${shortDate(issued.expires_at)}.` : ""}</span>
      </div>
      <div className="idn-mf">
        <label className="idn-check"><input type="checkbox" checked={stored} onChange={(e) => setStored(e.target.checked)} data-testid="scim-stored" /> I've stored it somewhere safe</label>
        <span className="idn-spacer" />
        <button type="button" className="idn-btn primary" disabled={!stored} onClick={onClose} data-testid="scim-done">Done</button>
      </div>
    </Sheet>
  );
}

function keyFromApiKey(k: ApiKey): ScimKey {
  return { id: k.id, name: k.name, status: k.status, created_at: k.created_at, expires_at: k.expires_at, last_used_at: k.last_used_at };
}

export function ProviderSync({ provider, origin, onClose }: { provider: ProviderDetail; origin: string | null | undefined; onClose: () => void }) {
  const guarded = useGuardedCall();
  const [detail, setDetail] = useState<ProviderDetail | null>(null);
  const [detailFailure, setDetailFailure] = useState<ReadFailure | null>(null);
  const [groups, setGroups] = useState<GroupRow[] | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [claim, setClaim] = useState(provider.groups_claim ?? "");
  const [keys, setKeys] = useState<ScimKey[] | null>(null);
  const [keysFailure, setKeysFailure] = useState<ReadFailure | null>(null);
  const [issued, setIssued] = useState<IssuedScimKey | null>(null);
  const [tokenName, setTokenName] = useState("SCIM");
  const [days, setDays] = useState(365);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null);
  const sso = provider.protocol !== "oauth2";

  const loadKeys = async (d: ProviderDetail | null) => {
    if (d?.scim_keys) { setKeys(d.scim_keys); return; }
    // An API without the provider detail: the org's key list, filtered to this provider's SCIM principal.
    try {
      const all = await idGet<{ keys: ApiKey[] }>("/v1/identity/api-keys");
      const owner = scimPrincipalId(provider.id);
      setKeys((all.keys ?? []).filter((k) => k.owner?.id === owner).map(keyFromApiKey));
    } catch (e) { setKeysFailure(failureOf(e)); }
  };

  useEffect(() => {
    let live = true;
    idGet<ProviderDetail>(`/v1/identity/providers/${encodeURIComponent(provider.id)}`)
      .then((d) => {
        if (!live) return;
        setDetail(d);
        setClaim(d.groups_claim ?? "");
        setRows((d.group_mappings ?? []).map((m: GroupMapping) => ({ external: m.external_group, group: m.group_id })));
        void loadKeys(d);
      })
      .catch((e) => { if (live) { setDetailFailure(failureOf(e)); void loadKeys(null); } });
    idGet<{ groups: GroupRow[] }>("/v1/identity/groups")
      .then((g) => { if (live) setGroups((g.groups ?? []).map((x) => ({ id: String(x.id), name: String(x.name) }))); })
      .catch(() => { if (live) setGroups([]); });
    return () => { live = false; };
  }, [provider.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Mappings are editable only once read: saving replaces them all, so an unknown set stays untouched.
  const mappingsKnown = Boolean(detail && Array.isArray(detail.group_mappings));
  const was = JSON.stringify([detail?.groups_claim ?? "", (detail?.group_mappings ?? []).map((m) => [m.external_group, m.group_id])]);
  const now = JSON.stringify([claim.trim(), rows.filter((r) => r.external.trim() && r.group).map((r) => [r.external.trim(), r.group])]);
  const dirty = mappingsKnown && was !== now;
  const incomplete = rows.some((r) => (r.external.trim() && !r.group) || (!r.external.trim() && r.group));

  const saveMappings = async () => {
    setBusy(true); setError(null);
    try {
      const group_mappings = Object.fromEntries(rows.filter((r) => r.external.trim() && r.group).map((r) => [r.external.trim(), r.group]));
      await guarded(`Group mappings decide who joins which group at ${provider.name}.`,
        () => idPatch(`/v1/identity/providers/${encodeURIComponent(provider.id)}`, { groups_claim: claim.trim(), group_mappings }));
      const d = await idGet<ProviderDetail>(`/v1/identity/providers/${encodeURIComponent(provider.id)}`);
      setDetail(d);
      setRows((d.group_mappings ?? []).map((m) => ({ external: m.external_group, group: m.group_id })));
      showToast("success", "Mappings saved");
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) {
        setError(e instanceof IdentityError && e.code === "invalid_provider" ? "A mapped group no longer exists" : e instanceof IdentityError ? e.message : "Not saved");
      }
    } finally { setBusy(false); }
  };

  const createToken = async () => {
    setBusy(true); setError(null);
    try {
      const out = await guarded("A sync token lets your IdP add and remove people.",
        () => idPost<IssuedScimKey>(`/v1/identity/providers/${encodeURIComponent(provider.id)}/scim-keys`, { name: tokenName.trim() || "SCIM", expires_in_days: days }));
      setIssued(out);
      setKeys((k) => [{ id: out.id, name: tokenName.trim() || "SCIM", status: "active", created_at: new Date().toISOString(), expires_at: out.expires_at, last_used_at: null }, ...(k ?? [])]);
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) {
        setError(e instanceof IdentityError && e.code === "too_long" ? "Longer than the policy allows" : e instanceof IdentityError ? e.message : "Not created");
      }
    } finally { setBusy(false); }
  };

  const revoke = async (id: string) => {
    setBusy(true); setError(null);
    try {
      await guarded("Revoking stops this token at once.", () => idDelete(`/v1/identity/providers/${encodeURIComponent(provider.id)}/scim-keys/${encodeURIComponent(id)}`));
      setKeys((k) => (k ?? []).map((x) => (x.id === id ? { ...x, status: "revoked" } : x)));
      setConfirmRevoke(null);
      showToast("success", "Token revoked");
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(e instanceof IdentityError ? e.message : "Not revoked");
    } finally { setBusy(false); }
  };

  if (issued) return <ScimShownOnce issued={issued} provider={provider.name} onClose={() => setIssued(null)} />;

  const liveKeys = (keys ?? []).filter((k) => k.status !== "revoked");
  return (
    <Sheet title={`${provider.name} · groups and sync`} icon={<ArrowsClockwise size={18} aria-hidden />} onClose={onClose} wide testId="provider-sync">
      {sso ? (
        <section aria-labelledby="sync-groups-h">
          <div className="idn-card-h"><h3 id="sync-groups-h"><UsersThree size={15} aria-hidden /> Groups at sign-in</h3></div>
          <p className="idn-dim idn-small" data-copy="prose">When someone signs in, each of their IdP groups listed here puts them in the BOSS group beside it.</p>
          {detailFailure && !mappingsKnown ? <ErrorState compact what="this provider's mappings" failure={detailFailure} /> : null}
          {mappingsKnown ? (
            <>
              <div className="idn-field">
                <label htmlFor="sync-claim" title="The token field that lists the person's groups">Groups claim</label>
                <input id="sync-claim" className="idn-input" value={claim} placeholder="groups" onChange={(e) => setClaim(e.target.value)} />
              </div>
              <div className="idn-sso-maps" data-testid="mapping-rows">
                {rows.map((r, i) => (
                  <div key={i} className="idn-sso-map">
                    <input className="idn-input" aria-label="IdP group" placeholder="IdP group, e.g. eng" value={r.external}
                      onChange={(e) => setRows((x) => x.map((y, j) => (j === i ? { ...y, external: e.target.value } : y)))} />
                    <span aria-hidden>→</span>
                    <select className="idn-input" aria-label="BOSS group" value={r.group}
                      onChange={(e) => setRows((x) => x.map((y, j) => (j === i ? { ...y, group: e.target.value } : y)))}>
                      <option value="">BOSS group…</option>
                      {(groups ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                    </select>
                    <button type="button" className="idn-btn sm ghost" aria-label="Remove this mapping" title="Remove"
                      onClick={() => setRows((x) => x.filter((_, j) => j !== i))}><Trash size={13} aria-hidden /></button>
                  </div>
                ))}
                {!rows.length ? <span className="idn-dim idn-small">No mappings: groups from {provider.name} are ignored.</span> : null}
              </div>
              {groups && !groups.length ? <p className="idn-dim idn-small" data-copy="prose">Make a group in Identity › Groups first.</p> : null}
              <div className="idn-row">
                <button type="button" className="idn-btn sm" onClick={() => setRows((x) => [...x, { external: "", group: "" }])} data-testid="mapping-add">
                  <Plus size={13} aria-hidden /> Add mapping
                </button>
                <span className="idn-spacer" />
                <CanButton permission="identity_provider:manage" className="idn-btn sm primary" disabled={!dirty || incomplete || busy} onClick={saveMappings}
                  data-testid="mapping-save" tip={incomplete ? "Each row needs both sides" : "Applies at each person's next sign-in"}>Save mappings</CanButton>
              </div>
            </>
          ) : null}
          <div className="idn-hr" />
        </section>
      ) : null}

      <section aria-labelledby="sync-scim-h">
        <div className="idn-card-h"><h3 id="sync-scim-h"><ArrowsClockwise size={15} aria-hidden /> Directory sync (SCIM)</h3></div>
        <p className="idn-dim idn-small" data-copy="prose">Your IdP adds, updates and removes people and groups in BOSS. Give it the base URL and a token.</p>
        {origin ? <CopyRow label="Base URL" value={`${origin.replace(/\/+$/, "")}/scim/v2`} tip="Your IdP's SCIM connector URL" />
          : <p className="idn-note" data-copy="prose">Set the public URL first: your IdP calls BOSS there.</p>}
        {keysFailure ? <ErrorState compact what="sync tokens" failure={keysFailure} /> : keys === null ? <span className="idn-dim idn-small">Loading tokens…</span> : liveKeys.length ? (
          <div className="idn-scroll">
            <table className="idn-t" data-testid="scim-keys">
              <thead><tr><th>Token</th><th>Last used</th><th>Expires</th><th /></tr></thead>
              <tbody>
                {liveKeys.map((k) => (
                  <tr key={k.id}>
                    <td><b data-copy="data">{k.name}</b> {k.status !== "active" ? <Pill tone="warn">{k.status === "grace" ? "ending" : k.status}</Pill> : null}</td>
                    <td className="idn-dim"><Ago iso={k.last_used_at} empty="never" /></td>
                    <td className="idn-dim">{k.expires_at ? shortDate(k.expires_at) : "never"}</td>
                    <td className="idn-right">
                      {confirmRevoke === k.id ? (
                        <span className="idn-row">
                          <button type="button" className="idn-btn sm ghost" onClick={() => setConfirmRevoke(null)}>Keep</button>
                          <CanButton permission="identity_provider:manage" className="idn-btn sm danger" disabled={busy} onClick={() => revoke(k.id)} data-testid={`scim-revoke-confirm-${k.id}`}>Revoke now</CanButton>
                        </span>
                      ) : (
                        <CanButton permission="identity_provider:manage" className="idn-btn sm" tip="Sync stops with this token" onClick={() => setConfirmRevoke(k.id)} data-testid={`scim-revoke-${k.id}`}>Revoke</CanButton>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState compact title="No sync token yet" testId="scim-empty">Make one below and paste it into your IdP.</EmptyState>
        )}
        <div className="idn-row idn-sso-newkey">
          <input className="idn-input" aria-label="Token name" value={tokenName} onChange={(e) => setTokenName(e.target.value)} />
          <select className="idn-input" aria-label="Expires in" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={90}>90 days</option>
            <option value={180}>180 days</option>
            <option value={365}>1 year</option>
          </select>
          <CanButton permission="identity_provider:manage" className="idn-btn sm primary" disabled={busy} onClick={createToken} data-testid="scim-create" tip="Shown once; BOSS keeps only a hash">
            <Plus size={13} aria-hidden /> New token
          </CanButton>
        </div>
      </section>
      <ErrorLine error={error} />
      <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn" onClick={onClose}>Close</button></div>
    </Sheet>
  );
}
