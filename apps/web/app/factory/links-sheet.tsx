"use client";

/**
 * App ▸ Links (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): one surface for what this app talks to.
 * Rows read `ALIAS → App · preview · test users · active`, with the address B uses (copy), when it
 * was last used, and Revoke. No credential value is ever shown; "Reveal test users" (D9) needs
 * `app_secret:write` with step-up and is audited. "+ Link" adds one: another app of this project,
 * Preview or Deployed, with or without its test users.
 */
import { SpinnerGap } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useId, useRef, useState } from "react";

import { callAction } from "../../lib/call-action";
import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import { ago, useNow } from "../agents/live-util";
import { createLinkAction, revealTestUsersAction, revokeLinkAction } from "./links-actions";

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

/** The row's words: `BANK → Banking · preview · test users · active`. */
export function linkWords(l: AppLink): string {
  return [`${l.alias} → ${l.provider_app}`, l.env === "deploy" ? "deployed" : "preview",
    l.credentials === "test_users" ? "test users" : null, l.status].filter(Boolean).join(" · ");
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
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  const now = useNow(30_000);
  const [links, setLinks] = useState<AppLink[] | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<Record<string, Revealed>>({});

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
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

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

  async function add(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy("add");
    const f = new FormData(e.currentTarget);
    f.set("project_id", projectId);
    f.set("app_key", appKey);
    const r = await callAction(createLinkAction, undefined, "app_link:manage")(f);
    setBusy(null);
    if (r.ok) { showToast("success", r.message ?? "Linked"); setAdding(false); setError(""); void load(); }
    else setError(r.error ?? "");
  }

  const copy = (url: string) => {
    void navigator.clipboard?.writeText(url).then(() => showToast("success", "Address copied"), () => undefined);
  };
  const active = (links ?? []).filter((l) => l.status === "active");
  const revokedCount = (links ?? []).length - active.length;

  return (
    <div className="fx-sheet-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={box} className="fx-sheet fx-links" role="dialog" aria-modal="true" aria-labelledby={titleId}
           data-testid="fx-links-sheet">
        <header className="fx-sheet-head">
          <h2 id={titleId} title={LINKS_TIP}>Links</h2>
          <span className="fx-sheet-sub ag-trunc" data-user-text title={app}>{app}</span>
          <button type="button" className="ag-act-close" aria-label="Close" onClick={onClose}>×</button>
        </header>
        {links === null ? <span className="fx-muted"><SpinnerGap size={12} className="ag-spin" aria-hidden /> Loading</span> : null}
        {links !== null && !active.length && !adding ? (
          <p className="fx-muted fx-links-empty" data-testid="fx-links-empty">
            Talks to nothing yet. A link gives this app another app&apos;s address and test logins.
          </p>
        ) : null}
        <ul className="fx-links-list" aria-label="Links">
          {active.map((l) => (
            <li key={l.id} className="fx-link" data-testid={`fx-link-${l.alias}`}>
              <div className="fx-link-main">
                <span className="fx-link-words" data-user-text>{linkWords(l)}</span>
                <span className="fx-link-meta fx-muted">
                  {l.url ? (
                    <button type="button" className="ag-link fx-link-url" title={`${l.env_prefix}URL — copy`}
                            data-testid={`fx-link-copy-${l.alias}`} onClick={() => copy(l.url as string)}>{l.url}</button>
                  ) : <span title="The provider has no deploy with a public address yet">no address yet</span>}
                  <span title="Last request through this link">{l.last_used_at ? ` · used ${ago(l.last_used_at, now)}` : " · never used"}</span>
                  {l.test_users ? <span title="Generated, test-only, rotated on each new preview">{` · ${l.test_users.count} test users`}</span> : null}
                </span>
                {revealed[l.id] ? (
                  <table className="fx-link-users" data-testid={`fx-link-users-${l.alias}`}>
                    <tbody>{revealed[l.id].map((u) => (
                      <tr key={u.username}><td data-user-text>{u.username}</td><td><code>{u.password}</code></td><td>{u.role}</td></tr>
                    ))}</tbody>
                  </table>
                ) : null}
              </div>
              <span className="fx-link-acts">
                {l.credentials === "test_users" && !revealed[l.id] ? (
                  <GatedButton permission="app_secret:write" type="button" className="ag-link" disabled={busy !== null}
                               title="Show the test logins (asks you to confirm it is you; recorded)"
                               data-testid={`fx-link-reveal-${l.alias}`} onClick={() => void reveal(l)}>Reveal test users</GatedButton>
                ) : null}
                <GatedButton permission="app_link:manage" type="button" className="ag-btn" disabled={busy !== null}
                             title="Cut this app off from the other: within seconds" aria-busy={busy === l.id || undefined}
                             data-testid={`fx-link-revoke-${l.alias}`} onClick={() => void revoke(l)}>Revoke</GatedButton>
              </span>
            </li>
          ))}
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
              <input name="alias" required pattern="[A-Za-z][A-Za-z0-9_]{1,30}" placeholder="BANK"
                     data-testid="fx-link-alias" onChange={(e) => { e.currentTarget.value = e.currentTarget.value.toUpperCase(); }} />
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
              <GatedButton permission="app_link:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy !== null}
                           title={LINKS_TIP} data-testid="fx-link-submit">
                {busy === "add" ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}Link
              </GatedButton>
              <button type="button" className="ag-link" onClick={() => setAdding(false)}>Cancel</button>
            </span>
          </form>
        ) : (
          <span className="fx-sheet-actions">
            <GatedButton permission="app_link:manage" type="button" className="ag-btn" disabled={!others.length}
                         title={others.length ? LINKS_TIP : "This project has no other app to link to"}
                         data-testid="fx-link-add" onClick={() => setAdding(true)}>+ Link</GatedButton>
          </span>
        )}
        {error ? <span className="ag-att-error" role="alert" data-user-text>{error}</span> : null}
      </div>
    </div>
  );
}
