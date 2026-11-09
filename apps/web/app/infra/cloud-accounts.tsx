"use client";

/**
 * Infra › Cloud › Accounts (spec §4.8; parity P-S-06..P-S-15, P-S-23): the accounts table, the
 * account drawer (IMPACT, CREDENTIALS, POLICY, ROTATE, REVOKE, AUDIT, today's AccountDrawer
 * restyled) and the `+ Account` sheet (the provider catalog with each adapter's categories and
 * skills, `Paste key`, `Use secret ref`). A credential is write-only: a reference is never drawn,
 * a pasted key is cleared the moment it is sent, and `Revoke…` asks for the account's name.
 */
import { GatedButton } from "../../components/gated";
import React, { useMemo, useState } from "react";

import { explainRawSecret } from "../../lib/hardware";
import { showToast } from "../client-forms";
import type { CloudViewProps } from "./cloud";
import {
  connectWithRefs, disableAccount, enableAccount, pasteKey, revokeAccount, rotateAccount, savePolicy, syncAccount, testAccount,
} from "./cloud-actions";
import { parseAccount, parseCatalog, type CatalogEntry, type CloudAccount } from "./cloud-rows";
import { afterWrite } from "./infra-actions";
import { ago, ErrorLine, localTime, useNow } from "./infra-util";
import { Drawer, DrawerSection, Empty, Sheet, StateWord, TypedConfirm, ZoneHead, confirmed } from "./kit";

const lines = (t: string) => t.split("\n").map((s) => s.trim()).filter(Boolean);

export function CloudAccounts(p: CloudViewProps) {
  const { body, url, go, canWrite } = p;
  const now = useNow(30_000);
  const rows = useMemo(() => body.rows.map(parseAccount), [body.rows]);
  const catalog = useMemo(() => parseCatalog(body.header.catalog), [body.header.catalog]);
  const [adding, setAdding] = useState(false);
  const open = url.account ? rows.find((r) => r.id === url.account) ?? null : null;
  const q = p.query.trim().toLowerCase();
  const shown = q ? rows.filter((r) => `${r.label} ${r.providerName}`.toLowerCase().includes(q)) : rows;

  return (
    <section className="cloud-sec" aria-labelledby="inf-cloud-accounts">
      <ZoneHead id="inf-cloud-accounts" label="ACCOUNTS" count={rows.length}>
        {canWrite ? (
          <GatedButton permission="cloud_account:manage" type="button" className="ag-btn" data-testid="inf-account-new" onClick={() => setAdding(true)}>+ Account</GatedButton>
        ) : null}
      </ZoneHead>
      {rows.length === 0 ? (
        <Empty testId="inf-cloud-empty">No cloud accounts</Empty>
      ) : (
        <div className="inf-table-wrap">
          <table className="inf-table" data-testid="inf-cloud-table">
            <thead><tr>
              <th scope="col">Account</th><th scope="col">Provider</th><th scope="col">State</th><th scope="col">Credential</th>
              <th scope="col">Last test</th><th scope="col">Authority</th><th scope="col">Resources</th><th scope="col">Spend 30d</th>
            </tr></thead>
            <tbody>
              {shown.map((a) => (
                <tr key={a.id} className="cloud-row" data-open={open?.id === a.id ? "1" : undefined} data-testid={`inf-account-row-${a.id}`}
                    onClick={() => go({ account: a.id })}>
                  <td><button type="button" className="cloud-open" onClick={(e) => { e.stopPropagation(); go({ account: a.id }); }}>
                    <span className="inf-trunc" data-copy="data" title={a.label}>{a.label}</span></button></td>
                  <td>{a.providerName}</td>
                  <td><StateWord w={a.state} /></td>
                  <td title="Write-only references; never shown">{a.slots.length} {a.slots.length === 1 ? "slot" : "slots"}, v{a.credentialVersion}</td>
                  <td title={a.lastTestAt ? localTime(a.lastTestAt) : "Never tested"}>
                    <StateWord w={a.lastTest} />{a.lastTestAt ? <span className="inf-faint"> {ago(a.lastTestAt, now)}</span> : null}
                  </td>
                  <td title={a.destructiveBlocked ? "Destructive actions blocked" : "Destructive actions allowed"}>{a.authority}</td>
                  <td className="inf-num">{a.resources}</td>
                  <td className="inf-num">{a.spend30d}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open ? <AccountDrawer {...p} account={open} catalog={catalog} onClose={() => go({ account: null })} /> : null}
      {adding ? <AddAccountSheet {...p} catalog={catalog} onClose={() => setAdding(false)} /> : null}
    </section>
  );
}

function Toggles({ values, set, disabled }: { values: Record<string, unknown>; set: (k: string, v: boolean) => void; disabled?: boolean }) {
  const items: [string, string, string][] = [
    ["read_only", "Read only", "BOSS reads; it never writes to this account"],
    ["billing_readable", "Billing readable", "BOSS may read billing for spend"],
    ["deploy_capable", "Deploy capable", "Deploys may target this account"],
    ["destructive_actions_disabled", "Destructive blocked", "Deletes and teardowns are refused"],
  ];
  return (
    <div className="cloud-toggles">
      {items.map(([k, label, tip]) => (
        <label key={k} className="inf-check" title={tip}>
          <input type="checkbox" checked={!!values[k]} disabled={disabled} onChange={(e) => set(k, e.target.checked)} data-testid={`inf-account-flag-${k}`} />
          {label}
        </label>
      ))}
    </div>
  );
}

/** Each slot's value must be a reference, not a pasted secret (lib/hardware explainRawSecret). */
function refProblem(refs: Record<string, string>, required: { key: string; required: boolean }[]): string | null {
  for (const s of required) {
    const v = (refs[s.key] ?? "").trim();
    if (!v) { if (s.required) return `${s.key}: add a secret reference`; continue; }
    const why = explainRawSecret(v);
    if (why) return why;
  }
  return null;
}

function AccountDrawer({ account: a, catalog, projectId, actor, canWrite, poll, onClose }: CloudViewProps & {
  account: CloudAccount; catalog: CatalogEntry[]; onClose: () => void;
}) {
  const now = useNow(30_000);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [policy, setPolicy] = useState({ account_label: a.label, owner_actor: a.owner ?? "", scopes: a.scopes.join("\n"),
    read_only: a.flags.readOnly, billing_readable: a.flags.billingReadable, deploy_capable: a.flags.deployCapable,
    destructive_actions_disabled: a.flags.destructiveActionsDisabled });
  const slots = a.rotationSlots.length ? a.rotationSlots : a.slots.map((k) => ({ key: k, required: true }));
  const [refs, setRefs] = useState<Record<string, string>>({});
  const [overlap, setOverlap] = useState("30");
  const [revoking, setRevoking] = useState(false);
  const [typed, setTyped] = useState("");
  const manage = canWrite && a.can.manage !== false;
  const def = catalog.find((c) => c.provider === a.provider);

  const run = async (key: string, fn: () => Promise<{ ok: boolean; error?: string }>, done: string) => {
    setBusy(key); setError(null);
    const r = await fn();
    setBusy(null);
    if (!r.ok) { setError(r.error ?? "Refused"); return false; }
    showToast("success", done);
    afterWrite(poll.refresh);
    return true;
  };

  return (
    <Drawer label={`${a.label} account`} testId="inf-account-drawer" onClose={onClose}
            head={<><strong className="inf-trunc" data-copy="data" title={a.label}>{a.label}</strong><StateWord w={a.state} /></>}>
      <span className="inf-muted">{a.providerName}{a.owner ? <>, <span data-copy="data">{a.owner}</span></> : null}</span>
      {a.lastError ? <ErrorLine error={a.lastError} testId="inf-account-last-error" /> : null}
      <ErrorLine error={error} testId="inf-account-error" />

      <DrawerSection label="IMPACT" testId="inf-account-impact">
        <dl className="inf-facts">
          <dt>Resources</dt><dd className="inf-num">{a.impact.resources}</dd>
          <dt>Spend 30d</dt><dd className="inf-num">{a.impact.spend}</dd>
          <dt>Open changes</dt><dd className="inf-num">{a.impact.openChanges}</dd>
          <dt>Receipts</dt><dd className="inf-num">{a.impact.receipts}</dd>
        </dl>
      </DrawerSection>

      <DrawerSection label="CREDENTIALS" testId="inf-account-credentials">
        {a.slots.length === 0 ? <Empty>No references set</Empty> : (
          <ul className="cloud-list">
            {a.slots.map((s) => (
              <li key={s} title="Write-only; never shown"><span data-copy="data">{s}</span><span className="inf-faint">•••••• set</span></li>
            ))}
          </ul>
        )}
        <span className="inf-faint" title="Each rotation bumps the version">Version {a.credentialVersion}</span>
      </DrawerSection>

      {manage ? (
        <DrawerSection label="POLICY" testId="inf-account-policy">
          <form className="inf-form" onSubmit={(e) => {
            e.preventDefault();
            void run("policy", () => savePolicy(projectId, actor, a.id, { ...policy, scopes: lines(policy.scopes) }), "Policy saved");
          }}>
            <div className="inf-form-row">
              <label className="inf-field"><span>Label</span>
                <input value={policy.account_label} onChange={(e) => setPolicy({ ...policy, account_label: e.target.value })} /></label>
              <label className="inf-field"><span>Owner</span>
                <input value={policy.owner_actor} placeholder={actor} onChange={(e) => setPolicy({ ...policy, owner_actor: e.target.value })} /></label>
            </div>
            <label className="inf-field" title="One scope per line"><span>Scopes</span>
              <textarea rows={2} value={policy.scopes} onChange={(e) => setPolicy({ ...policy, scopes: e.target.value })} /></label>
            <Toggles values={policy} set={(k, v) => setPolicy({ ...policy, [k]: v })} />
            <div className="inf-actions">
              <GatedButton permission="cloud_account:manage" type="submit" className="ag-btn" disabled={busy !== null} data-testid="inf-account-save">Save policy</GatedButton>
              <GatedButton permission="cloud_account:manage" type="button" className="ag-btn" disabled={busy !== null || a.can.test === false} data-testid="inf-account-test"
                      title="Calls the provider once with these credentials"
                      onClick={() => void run("test", () => testAccount(projectId, actor, a.id), "Tested")}>Test</GatedButton>
              <GatedButton permission="cloud_account:manage" type="button" className="ag-btn" disabled={busy !== null || a.can.sync === false} data-testid="inf-account-sync"
                      title="Reads inventory and costs from the provider"
                      onClick={() => void run("sync", () => syncAccount(projectId, actor, a.id), "Sync started")}>Sync</GatedButton>
              {a.can.enable ? (
                <GatedButton permission="cloud_account:manage" type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-account-enable"
                        onClick={() => void run("enable", () => enableAccount(projectId, actor, a.id), "Enabled")}>Enable</GatedButton>
              ) : (
                <GatedButton permission="cloud_account:manage" type="button" className="ag-btn" disabled={busy !== null || a.can.disable === false} data-testid="inf-account-disable"
                        title="Stops every adapter call for this account"
                        onClick={() => void run("disable", () => disableAccount(projectId, actor, a.id), "Disabled")}>Disable</GatedButton>
              )}
            </div>
          </form>
        </DrawerSection>
      ) : null}

      {manage && a.can.rotate !== false ? (
        <DrawerSection label="ROTATE" testId="inf-account-rotate">
          <form className="inf-form" onSubmit={(e) => {
            e.preventDefault();
            const why = refProblem(refs, slots);
            if (why) { setError(why); return; }
            void run("rotate", () => rotateAccount(projectId, actor, a.id,
              Object.fromEntries(Object.entries(refs).filter(([, v]) => v.trim()).map(([k, v]) => [k, v.trim()])), Number(overlap)), "Rotated")
              .then((ok) => { if (ok) setRefs({}); });
          }}>
            {slots.map((s) => (
              <label key={s.key} className="inf-field" title="A secret reference, not the secret"><span data-copy="data">{s.key}</span>
                <input type="password" autoComplete="new-password" value={refs[s.key] ?? ""} placeholder="Secret reference"
                       data-testid={`inf-account-rotate-${s.key}`} onChange={(e) => setRefs({ ...refs, [s.key]: e.target.value })} /></label>
            ))}
            <div className="inf-actions">
              <label className="inf-field" title="How long running work may keep the old credential"><span>Overlap</span>
                <select value={overlap} onChange={(e) => setOverlap(e.target.value)} data-testid="inf-account-overlap">
                  {["5", "15", "30", "60"].map((m) => <option key={m} value={m}>{m} min</option>)}
                </select></label>
              <GatedButton permission="cloud_account:manage" type="submit" className="ag-btn" disabled={busy !== null || def?.adapterStatus === "planned"}>Rotate</GatedButton>
            </div>
          </form>
        </DrawerSection>
      ) : null}

      {manage && a.can.revoke !== false ? (
        <DrawerSection label="REVOKE" testId="inf-account-revoke-sec">
          <div className="inf-actions">
            <GatedButton permission="cloud_account:manage" type="button" className="ag-btn" data-tone="reject" data-testid="inf-account-revoke"
                    title="Permanent. Its history stays as evidence." onClick={() => setRevoking(true)}>Revoke…</GatedButton>
          </div>
        </DrawerSection>
      ) : null}

      <DrawerSection label="AUDIT" testId="inf-account-audit-sec"
                     actions={<a className="inf-link" data-testid="inf-account-audit"
                                 href={`?area=ops&provider_account_id=${encodeURIComponent(a.id)}`}>Audit ↗</a>}>
        {a.events.length + a.receipts.length === 0 ? <Empty>Nothing recorded</Empty> : (
          <ul className="cloud-list">
            {a.events.map((e) => (
              <li key={e.id}><span data-copy="data">{e.kind}</span><span className="inf-faint" data-copy="data">{e.actor}, {ago(e.at, now)}</span></li>
            ))}
            {a.receipts.map((r) => (
              <li key={r.id}><span data-copy="data">{r.action}</span><span className="inf-faint">{r.status}, {ago(r.at, now)}</span></li>
            ))}
          </ul>
        )}
      </DrawerSection>

      {revoking ? (
        <Sheet title={`Revoke ${a.label}?`} testId="inf-account-revoke-sheet" onClose={() => { setRevoking(false); setTyped(""); }}>
          <dl className="inf-facts">
            <dt>Resources</dt><dd>{a.impact.resources}</dd>
            <dt>Spend 30d</dt><dd>{a.impact.spend}</dd>
            <dt>Undo</dt><dd>None</dd>
          </dl>
          <TypedConfirm name={a.label} value={typed} onChange={setTyped} testId="inf-account-revoke-name" />
          <div className="inf-actions">
            <GatedButton permission="cloud_account:manage" type="button" className="ag-btn" data-tone="reject" data-testid="inf-account-revoke-confirm"
                    disabled={!confirmed(a.label, typed) || busy !== null}
                    onClick={() => void run("revoke", () => revokeAccount(projectId, actor, a.id), "Revoked")
                      .then((ok) => { if (ok) { setRevoking(false); setTyped(""); } })}>Revoke</GatedButton>
            <button type="button" className="ag-link" onClick={() => { setRevoking(false); setTyped(""); }}>Cancel</button>
          </div>
        </Sheet>
      ) : null}
    </Drawer>
  );
}

function AddAccountSheet({ catalog, projectId, actor, poll, onClose }: CloudViewProps & { catalog: CatalogEntry[]; onClose: () => void }) {
  const [picked, setPicked] = useState(catalog.find((c) => c.adapterStatus === "available")?.provider ?? catalog[0]?.provider ?? "");
  const def = catalog.find((c) => c.provider === picked);
  const pasteable = catalog.filter((c) => c.pasteable);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Paste key
  const [pasteProvider, setPasteProvider] = useState(def?.pasteable ? picked : pasteable[0]?.provider ?? "");
  const pasteDef = pasteable.find((c) => c.provider === pasteProvider) ?? pasteable[0];
  const [pasteLabel, setPasteLabel] = useState("");
  const [secret, setSecret] = useState("");
  const [baseUrl, setBaseUrl] = useState("");

  // Use secret ref
  const [authKey, setAuthKey] = useState(def?.authMethods[0]?.key ?? "");
  const auth = def?.authMethods.find((m) => m.key === authKey) ?? def?.authMethods[0];
  const [refLabel, setRefLabel] = useState("");
  const [refs, setRefs] = useState<Record<string, string>>({});
  const [scopes, setScopes] = useState("");
  const [flags, setFlags] = useState<Record<string, boolean>>({ read_only: true, billing_readable: false, deploy_capable: false, destructive_actions_disabled: true });
  const available = def?.adapterStatus === "available" && !!auth?.available;

  const choose = (prov: string) => {
    setPicked(prov);
    const d = catalog.find((c) => c.provider === prov);
    setAuthKey(d?.authMethods[0]?.key ?? "");
    setRefs({});
    if (d?.pasteable) setPasteProvider(prov);
  };

  const paste = async () => {
    if (!pasteDef) return;
    if (!pasteLabel.trim()) { setError("Name the account"); return; }
    if (secret.trim().length < 8) { setError("That key is too short"); return; }
    setBusy("paste"); setError(null);
    const r = await pasteKey(projectId, actor, { provider: pasteDef.provider, account_label: pasteLabel, secret_value: secret,
      base_url: pasteDef.baseUrl && baseUrl.trim() ? baseUrl.trim() : undefined });
    setSecret("");   // sent: never kept in the browser
    setBusy(null);
    if (!r.ok) { setError(r.error.split(secret).join("[redacted]")); return; }
    showToast("success", "Connected");
    afterWrite(poll.refresh);
    onClose();
  };
  const bind = async () => {
    if (!def || !auth) return;
    if (!refLabel.trim()) { setError("Name the account"); return; }
    const why = refProblem(refs, auth.slots);
    if (why) { setError(why); return; }
    setBusy("ref"); setError(null);
    const r = await connectWithRefs(projectId, actor, {
      provider: def.provider, account_label: refLabel.trim(), auth_method: auth.key,
      credential_refs: Object.fromEntries(Object.entries(refs).filter(([, v]) => v.trim()).map(([k, v]) => [k, v.trim()])),
      scopes: lines(scopes), read_only: !!flags.read_only, billing_readable: !!flags.billing_readable,
      deploy_capable: !!flags.deploy_capable, destructive_actions_disabled: !!flags.destructive_actions_disabled,
    });
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Connected");
    afterWrite(poll.refresh);
    onClose();
  };

  return (
    <Sheet title="Cloud account" testId="inf-account-sheet" onClose={onClose}>
      <div className="cloud-catalog" role="radiogroup" aria-label="Providers" data-testid="inf-account-catalog">
        {catalog.length === 0 ? <Empty>No providers listed</Empty> : catalog.map((c) => (
          <button key={c.provider} type="button" role="radio" aria-checked={picked === c.provider} className="cloud-cat"
                  data-testid={`inf-catalog-${c.provider}`} onClick={() => choose(c.provider)}
                  title={c.skills.length ? `Skills: ${c.skills.join(", ")}` : undefined}>
            <span className="cloud-cat-name">{c.name}</span>
            <span className="cloud-cat-meta">
              <span className="cloud-chip" data-kind={c.adapterStatus} data-testid="inf-catalog-adapter"
                    title={c.adapterStatus === "available" ? "Adapter ready" : "Adapter planned; nothing connects yet"}>
                <span data-copy="data">{c.adapter || c.adapterStatus}</span>
              </span>
              <span className="inf-faint" data-copy="data">{c.categories.slice(0, 3).join(", ") || c.category}</span>
              <span className="inf-faint">{c.skillCount} skills</span>
            </span>
          </button>
        ))}
      </div>
      <ErrorLine error={error} testId="inf-account-sheet-error" />

      <form className="inf-form cloud-subform" data-testid="inf-account-paste" onSubmit={(e) => { e.preventDefault(); void paste(); }}>
        <h3 className="ag-zone-label" title="The value goes to the local vault; BOSS keeps a reference">Paste key</h3>
        {pasteable.length === 0 ? <Empty>No provider takes a pasted key</Empty> : (
          <>
            <div className="inf-form-row">
              <label className="inf-field"><span>Provider</span>
                <select value={pasteDef?.provider ?? ""} onChange={(e) => setPasteProvider(e.target.value)} data-testid="inf-paste-provider">
                  {pasteable.map((c) => <option key={c.provider} value={c.provider}>{c.name}</option>)}
                </select></label>
              <label className="inf-field"><span>Account label</span>
                <input value={pasteLabel} onChange={(e) => setPasteLabel(e.target.value)} placeholder={`${pasteDef?.name ?? "Provider"} prod`}
                       data-testid="inf-paste-label" /></label>
            </div>
            <label className="inf-field" title="Written to the local vault; never shown again"><span>API key</span>
              <input type="password" autoComplete="new-password" minLength={8} value={secret} onChange={(e) => setSecret(e.target.value)}
                     data-testid="inf-paste-secret" /></label>
            {pasteDef?.baseUrl ? (
              <label className="inf-field" title="Ollama, vLLM or any OpenAI-compatible endpoint"><span>Base URL</span>
                <input type="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://localhost:11434/v1" /></label>
            ) : null}
            <div className="inf-actions">
              <GatedButton permission="cloud_account:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-paste-submit">Paste key</GatedButton>
            </div>
          </>
        )}
      </form>

      <details className="cloud-subform" data-testid="inf-account-secret-ref">
        <summary className="ag-zone-label" title="Bind secret references you already hold">Use secret ref</summary>
        {!def ? <Empty>Pick a provider</Empty> : (
          <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void bind(); }}>
            {!available ? <span className="cloud-chip" data-kind="planned" title="BOSS won't accept credentials for a planned adapter">Adapter planned</span> : null}
            <div className="inf-form-row">
              <label className="inf-field"><span>Account label</span>
                <input value={refLabel} onChange={(e) => setRefLabel(e.target.value)} disabled={!available} data-testid="inf-ref-label" /></label>
              <label className="inf-field"><span>Sign-in</span>
                <select value={auth?.key ?? ""} onChange={(e) => { setAuthKey(e.target.value); setRefs({}); }} disabled={!available}>
                  {def.authMethods.map((m) => <option key={m.key} value={m.key} disabled={!m.available}>{m.key}</option>)}
                </select></label>
            </div>
            {(auth?.slots ?? []).map((s) => (
              <label key={s.key} className="inf-field" title="A secret reference, not the secret">
                <span data-copy="data">{s.key}{s.required ? "" : ", optional"}</span>
                <input type="password" autoComplete="new-password" value={refs[s.key] ?? ""} placeholder="Secret reference"
                       disabled={!available} data-testid={`inf-ref-slot-${s.key}`} onChange={(e) => setRefs({ ...refs, [s.key]: e.target.value })} />
              </label>
            ))}
            <label className="inf-field" title="One scope per line"><span>Scopes</span>
              <textarea rows={2} value={scopes} onChange={(e) => setScopes(e.target.value)} disabled={!available} /></label>
            <Toggles values={flags} set={(k, v) => setFlags({ ...flags, [k]: v })} disabled={!available} />
            <div className="inf-actions">
              <GatedButton permission="cloud_account:manage" type="submit" className="ag-btn" disabled={!available || busy !== null} data-testid="inf-ref-submit">Connect</GatedButton>
            </div>
          </form>
        )}
      </details>
    </Sheet>
  );
}
