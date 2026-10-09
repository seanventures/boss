"use client";

/**
 * App ▸ Sign-in (APP_TEMPLATES_AND_SECURITY_LOOP.md §6.4): one press makes an app built from the
 * login-oauth template sign people in with BOSS. BOSS registers the app as a client of its OIDC
 * provider, registers the app's addresses (each running preview, where it is deployed) as redirect
 * URIs, and gives the app OIDC_BOSS_CLIENT_ID, _SECRET and _ISSUER as app secrets for preview and
 * deploy. The sheet shows presence and addresses only: never a secret.
 */
import { SpinnerGap } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useId, useState } from "react";

import { callAction } from "../../lib/call-action";
import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import { stopSignInWithBossAction, useSignInWithBossAction } from "./sign-in-actions";

export const SIGN_IN_TIP = "People sign in to this app with their BOSS account (login-oauth's BOSS button).";

export type SignInStatus = {
  app_key: string;
  template_applied: boolean;
  connected: boolean;
  secrets: { preview?: string[]; deploy?: string[] };
  client: null | { client_id: string; status: string; pool_id: string; redirect_uris: string[]; allowed_scopes: string[] };
  issuer?: string | null;
};

export function parseSignIn(w: unknown): SignInStatus | null {
  const s = w as Partial<SignInStatus> | null;
  if (!s || typeof s !== "object" || typeof s.connected !== "boolean") return null;
  return {
    app_key: String(s.app_key ?? ""), template_applied: Boolean(s.template_applied), connected: s.connected,
    secrets: (s.secrets && typeof s.secrets === "object") ? s.secrets : {},
    client: s.client && typeof s.client === "object" && typeof s.client.client_id === "string" ? s.client : null,
    issuer: typeof s.issuer === "string" ? s.issuer : null,
  };
}

/** Where a redirect URI points, in words: "preview prv_x" or the deployed origin. */
export function uriWords(uri: string): string {
  const preview = /\/v1\/previews\/([^/]+)\/web\//.exec(uri);
  if (preview) return `preview ${preview[1]}`;
  try { return new URL(uri).host === "localhost:3000" ? "local (until a preview starts)" : new URL(uri).origin; }
  catch { return uri; }
}

export type SignInSheetProps = { projectId: string; appKey: string; app: string; onClose: () => void };

export function SignInSheet({ projectId, appKey, app, onClose }: SignInSheetProps) {
  const titleId = useId();
  const [status, setStatus] = useState<SignInStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"use" | "stop" | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/v1/apps/${encodeURIComponent(appKey)}/sign-in-with-boss?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (!r.ok) setError(`Sign-in could not be read (HTTP ${r.status}).`);
      else setStatus(parseSignIn(await r.json()));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    setLoaded(true);
  }, [appKey, projectId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const form = () => {
    const f = new FormData();
    f.set("project_id", projectId);
    f.set("app_key", appKey);
    return f;
  };

  async function use() {
    setBusy("use");
    const r = await callAction(useSignInWithBossAction, undefined, "oidc_client:manage")(form());
    setBusy(null);
    if (r.ok) { showToast("success", r.message ?? "Sign in with BOSS is on"); setError(""); void load(); }
    else setError(r.error ?? "");
  }

  async function stop() {
    setBusy("stop");
    const r = await callAction(stopSignInWithBossAction, undefined, "oidc_client:manage")(form());
    setBusy(null);
    if (r.ok) { showToast("success", r.message ?? "Sign in with BOSS is off"); setError(""); void load(); }
    else setError(r.error ?? "");
  }

  const s = status;
  const envs = (["preview", "deploy"] as const).filter((e) => (s?.secrets[e] ?? []).length === 3);
  return (
    <div className="fx-sheet-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="fx-sheet fx-signin" role="dialog" aria-modal="true" aria-labelledby={titleId}
           data-testid="fx-signin-sheet">
        <header className="fx-sheet-head">
          <h2 id={titleId} title={SIGN_IN_TIP}>Sign-in</h2>
          <span className="fx-sheet-sub ag-trunc" data-user-text title={app}>{app}</span>
          <button type="button" className="ag-act-close" aria-label="Close" onClick={onClose}>×</button>
        </header>
        {!loaded ? <span className="fx-muted"><SpinnerGap size={12} className="ag-spin" aria-hidden /> Loading</span> : null}
        {loaded && s && !s.template_applied ? (
          <p className="fx-muted" data-testid="fx-signin-needs-template">
            Apply the login-oauth template to this app first: its BOSS button is what this turns on.
          </p>
        ) : null}
        {loaded && s?.template_applied ? (
          <>
            <p className="fx-signin-state" data-testid="fx-signin-state">
              {s.connected ? "Sign in with BOSS is on" : s.client ? "Registered, not wired" : "Sign in with BOSS is off"}
              {s.connected ? <span className="fx-muted" title="OIDC_BOSS_CLIENT_ID, _SECRET and _ISSUER, in the vault">
                {` · settings for ${envs.map((e) => (e === "deploy" ? "deploys" : "previews")).join(" and ")}`}</span> : null}
            </p>
            {s.client ? (
              <dl className="fx-signin-facts">
                <dt>Client</dt><dd><code data-testid="fx-signin-client">{s.client.client_id}</code></dd>
                {s.issuer ? (<><dt>Issuer</dt><dd><code>{s.issuer}</code></dd></>) : null}
                <dt title="Where BOSS may send people back after they sign in">Returns to</dt>
                <dd><ul className="fx-signin-uris" aria-label="Redirect URIs">
                  {s.client.redirect_uris.map((u) => (
                    <li key={u} title={u} data-user-text>{uriWords(u)}</li>
                  ))}
                </ul></dd>
              </dl>
            ) : null}
            <span className="fx-sheet-actions">
              <GatedButton permission="oidc_client:manage" type="button" className="ag-btn ag-btn-primary"
                           disabled={busy !== null} data-testid="fx-signin-use" aria-busy={busy === "use" || undefined}
                           title={s.connected ? "Register the app's current preview and deploy addresses again"
                             : `${SIGN_IN_TIP} Asks you to confirm it is you.`}
                           onClick={() => void use()}>
                {busy === "use" ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}
                {s.connected ? "Refresh addresses" : "Use Sign in with BOSS"}
              </GatedButton>
              {s.client ? (
                <GatedButton permission="oidc_client:manage" type="button" className="ag-link" disabled={busy !== null}
                             data-testid="fx-signin-stop" title="Removes the client: everyone signed in through it is signed out"
                             onClick={() => void stop()}>Stop using</GatedButton>
              ) : null}
            </span>
            <p className="fx-muted fx-signin-note">
              A preview gets these settings when it starts; a new preview&apos;s address is registered as it starts.
            </p>
          </>
        ) : null}
        {error ? <span className="ag-att-error" role="alert" data-user-text>{error}</span> : null}
      </div>
    </div>
  );
}
