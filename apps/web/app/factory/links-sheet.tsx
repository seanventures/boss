"use client";

/**
 * App ▸ Links (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): one surface for what this app talks to.
 * Rows read `ALIAS → App, preview, test users, active`, with the address AS THE APP SEES IT , 
 * the env var it reads (`BOSS_LINK_BANK_URL`, copy), never the gateway's internal host, when it
 * was last used, and Revoke (asks first). No credential value is ever shown; "Reveal test users" (D9) needs
 * `app_secret:write` with step-up and is audited. "+ Link" adds one: another app of this project,
 * Preview or Deployed, with or without its test users.
 */
import { SpinnerGap } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useState } from "react";

import { callAction } from "../../lib/call-action";
import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import { ago, useNow } from "../agents/live-util";
import { createLinkAction, revealTestUsersAction, revokeLinkAction } from "./links-actions";
import { SideSheet } from "../../components/v2/templates/side-sheet";
import { aliasProblem } from "../../lib/link-alias";

export type AppLink = {
  id: string; alias: string; provider_app_key: string; provider_app: string; env: "preview" | "deploy";
  credentials: "none" | "test_users" | "client"; status: "active" | "revoked"; url?: string | null;
  env_prefix: string; test_users?: { count: number; generation: number } | null; last_used_at?: string | null;
  revoked_at?: string | null;
};
type Revealed = { username: string; password: string; role: string }[];

export const LINKS_TIP = "B gets A's address and test logins at build and run time.";

export function parseLinks(w: unknown): AppLink[] {
  const links = (w as { links?: unknown } | null)?.links;
  return Array.isArray(links) ? (links as AppLink[]).filter((l) => l && typeof l.id === "string") : [];
}

/**
 * What the row shows as the address: what the APP reads. A preview link reaches the provider
 * through BOSS's link gateway at an internal host (`http://egress:18100`) that means nothing to a
 * person and is not reachable from their browser (tester P2), so the row names the env var the
 * app reads it from, and the provider; a deployed link shows the provider's public URL.
 */
export function linkAddress(l: AppLink): { text: string; copy: string; tip: string } | null {
  if (l.status !== "active") return null;
  const envVar = `${l.env_prefix}URL`;
  if (l.env === "deploy") {
    return l.url ? { text: l.url, copy: l.url, tip: `${l.provider_app}'s deployed address, given to the app as ${envVar}` } : null;
  }
  return { text: envVar, copy: envVar, tip: `The app reads ${l.provider_app}'s preview address from ${envVar}; BOSS's link gateway forwards it, to that preview only` };
}

/** The row's words: `BANK → Banking, preview, test users, active`. */
export function linkWords(l: AppLink): string {
  return [`${l.alias} → ${l.provider_app}`, l.env === "deploy" ? "deployed" : "preview",
    l.credentials === "test_users" ? "test users" : null, l.status].filter(Boolean).join(", ");
}

export type LinksSheetProps = {
  projectId: string;
  appKey: string;
  app: string;
  /** The project's other apps: what this one may link to (same project only, D7). */
  others: { key: string; app: string }[];
  onClose: () => void;
};

export function LinksSheet({ projectId, appKey, app, others, onClose }: LinksSheetProps) {
  const now = useNow(30_000);
  const [links, setLinks] = useState<AppLink[] | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<Record<string, Revealed>>({});
  const [confirm, setConfirm] = useState<string | null>(null);
  const [alias, setAlias] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/v1/apps/${encodeURIComponent(appKey)}/links?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (!r.ok) { setError(`Links could not be read (HTTP ${r.status}).`); setLinks([]); return; }
      setLinks(parseLinks(await r.json()));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setLinks([]);
    }
  }, [appKey, projectId]);

  useEffect(() => { void load(); }, [load]);

  const form = (extra: Record<string, string>) => {
    const f = new FormData();
    f.set("project_id", projectId);
    f.set("app_key", appKey);
    for (const [k, v] of Object.entries(extra)) f.set(k, v);
    return f;
  };

  async function revoke(l: AppLink) {
    setBusy(l.id);
    const r = await callAction(revokeLinkAction, undefined, "app_link:manage")(form({ link_id: l.id }));
    setBusy(null);
    setConfirm(null);
    if (r.ok) { showToast("success", `${l.alias} revoked`); void load(); } else setError(r.error ?? "");
  }

  async function reveal(l: AppLink) {
    setBusy(`reveal-${l.id}`);
    const f = form({ env: l.env });
    f.set("app_key", l.provider_app_key);
    const r = await callAction(revealTestUsersAction, undefined, "app_secret:write")(f);
    setBusy(null);
    if (r.ok) setRevealed((m) => ({ ...m, [l.id]: ((r.data?.users ?? []) as Revealed) }));
    else setError(r.error ?? "");
  }

  const aliasWhy = alias ? aliasProblem(alias) : "";
  async function add(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (aliasWhy) { setError(aliasWhy); return; }
    setBusy("add");
    const f = new FormData(e.currentTarget);
    f.set("project_id", projectId);
    f.set("app_key", appKey);
    const r = await callAction(createLinkAction, undefined, "app_link:manage")(f);
    setBusy(null);
    if (r.ok) { showToast("success", r.message ?? "Linked"); setAdding(false); setError(""); setAlias(""); void load(); }
    else setError(r.error ?? "");
  }

  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text).then(() => showToast("success", "Copied"), () => undefined);
  };
  const active = (links ?? []).filter((l) => l.status === "active");
  const revokedCount = (links ?? []).length - active.length;

  return (
    <SideSheet title={<span title={LINKS_TIP}>Links</span>} labelledTitle={`Links of ${app}`} sub={app} onClose={onClose}
               testId="fx-links-sheet">
      <div className="fx-links">
        {links === null ? <span className="fx-muted"><SpinnerGap size={12} className="ag-spin" aria-hidden /> Loading</span> : null}
        {links !== null && !active.length && !adding ? (
          <p className="fx-muted fx-links-empty" data-testid="fx-links-empty">
            Talks to nothing yet. A link gives this app another app&apos;s address and test logins.
          </p>
        ) : null}
        <ul className="fx-links-list" aria-label="Links">
          {active.map((l) => {
            const addr = linkAddress(l);
            const users = revealed[l.id];
            return (
              <li key={l.id} className="fx-link" data-testid={`fx-link-${l.alias}`}>
                <div className="fx-link-main">
                  <span className="fx-link-words" data-user-text>{linkWords(l)}</span>
                  <span className="fx-link-meta fx-muted">
                    {addr ? (
                      <button type="button" className="ag-link fx-link-url" title={`${addr.tip}. Copy.`}
                              data-testid={`fx-link-copy-${l.alias}`} onClick={() => copy(addr.copy)}>{addr.text}</button>
                    ) : <span title="The provider has no deploy with a public address yet">no address yet</span>}
                    <span title="Last request through this link">{l.last_used_at ? `, used ${ago(l.last_used_at, now)}` : ", never used"}</span>
                    {l.test_users ? <span title="Generated, test-only, rotated on each new preview">{`, ${l.test_users.count} test users`}</span> : null}
                  </span>
                  {confirm === l.id ? (
                    <span className="fx-link-confirm" role="alertdialog" aria-label={`Revoke ${l.alias}?`} data-testid={`fx-link-confirm-${l.alias}`}>
                      <span>Revoke {l.alias}? {app} loses {l.provider_app}&apos;s address within seconds.</span>
                      <GatedButton permission="app_link:manage" type="button" className="ag-btn" disabled={busy !== null}
                                   aria-busy={busy === l.id || undefined} data-testid={`fx-link-revoke-yes-${l.alias}`}
                                   onClick={() => void revoke(l)}>Revoke</GatedButton>
                      <button type="button" className="ag-link" onClick={() => setConfirm(null)}>Keep</button>
                    </span>
                  ) : null}
                </div>
                <span className="fx-link-acts">
                  {l.credentials === "test_users" && !users ? (
                    <GatedButton permission="app_secret:write" type="button" className="ag-link" disabled={busy !== null}
                                 title="Show the test logins (asks you to confirm it is you; recorded)"
                                 data-testid={`fx-link-reveal-${l.alias}`} onClick={() => void reveal(l)}>Reveal test users</GatedButton>
                  ) : null}
                  {confirm !== l.id ? (
                    <GatedButton permission="app_link:manage" type="button" className="ag-btn" disabled={busy !== null}
                                 title="Cut this app off from the other: asks first"
                                 data-testid={`fx-link-revoke-${l.alias}`} onClick={() => setConfirm(l.id)}>Revoke</GatedButton>
                  ) : null}
                </span>
                  {users ? (
                    <div className="fx-link-users-wrap" data-testid={`fx-link-users-${l.alias}`}>
                      <table className="fx-link-users">
                        <thead><tr><th scope="col">Login</th><th scope="col">Password</th><th scope="col">Role</th></tr></thead>
                        <tbody>{users.map((u) => (
                          <tr key={u.username}><td data-user-text>{u.username}</td><td><code>{u.password}</code></td><td>{u.role}</td></tr>
                        ))}</tbody>
                      </table>
                      <button type="button" className="ag-link" data-testid={`fx-link-hide-${l.alias}`}
                              onClick={() => setRevealed(({ [l.id]: _gone, ...rest }) => rest)}>Hide</button>
                    </div>
                  ) : null}
              </li>
            );
          })}
        </ul>
        {revokedCount ? <span className="fx-muted fx-links-revoked">{revokedCount} revoked</span> : null}
        {adding ? (
          <form className="fx-form fx-link-add" onSubmit={(e) => void add(e)} data-testid="fx-link-add-form">
            <label className="fx-field"><span>Talks to</span>
              <select name="provider_app_key" required data-testid="fx-link-provider" defaultValue="">
                <option value="" disabled>Choose an app of this project</option>
                {others.map((o) => <option key={o.key} value={o.key}>{o.app}</option>)}
              </select>
            </label>
            <label className="fx-field"><span title="Its env vars are BOSS_LINK_<ALIAS>_URL, _USERS and _TOKEN">Alias</span>
              <input name="alias" required placeholder="BANK" maxLength={31} value={alias} aria-invalid={!!aliasWhy || undefined}
                     data-testid="fx-link-alias" onChange={(e) => { setAlias(e.currentTarget.value.toUpperCase()); setError(""); }} />
              {aliasWhy ? <span className="fx-muted" role="status" data-testid="fx-link-alias-why">{aliasWhy}</span> : null}
            </label>
            <span className="fx-link-opts">
              <label className="fx-field fx-link-env"><span>Reaches its</span>
                <select name="env" defaultValue="preview" data-testid="fx-link-env">
                  <option value="preview">Preview</option>
                  <option value="deploy">Deployed</option>
                </select>
              </label>
              <label className="fx-waive-gate" title="Generated test-only logins, rotated on each new preview">
                <input type="checkbox" name="test_users" defaultChecked data-testid="fx-link-test-users" /> Test users
              </label>
            </span>
            <span className="fx-sheet-actions">
              <GatedButton permission="app_link:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy !== null || !!aliasWhy}
                           title={aliasWhy || LINKS_TIP} data-testid="fx-link-submit">
                {busy === "add" ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}Link
              </GatedButton>
              <button type="button" className="ag-link" onClick={() => { setAdding(false); setError(""); }}>Cancel</button>
            </span>
          </form>
        ) : (
          <span className="fx-sheet-actions">
            <GatedButton permission="app_link:manage" type="button" className="ag-btn" disabled={!others.length}
                         title={others.length ? LINKS_TIP : "This project has no other app to link to"}
                         data-testid="fx-link-add" onClick={() => setAdding(true)}>+ Link</GatedButton>
          </span>
        )}
        {error ? <span className="ag-att-error" role="alert" data-testid="fx-link-error" data-user-text>{error}</span> : null}
      </div>
    </SideSheet>
  );
}
