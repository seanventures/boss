"use client";

/**
 * SIGN IN WITH BOSS for one app (APP_TEMPLATES_AND_SECURITY_LOOP.md §6.4): one press makes an app
 * built from the login-oauth template sign people in with BOSS. BOSS registers the app as a client
 * of its OIDC provider, registers the app's addresses (each running preview, where it is deployed)
 * as redirect URIs, and gives the app OIDC_BOSS_CLIENT_ID, _SECRET and _ISSUER as app secrets for
 * preview and deploy. Presence and addresses only: never a secret.
 *
 * It is the BOSS row of App › Connectors (components/v2/templates/setup-sheet.tsx): one surface for
 * an app's sign-in providers. It used to be a sheet of its own (⋯ › Sign-in), beside Setup.
 */
import { SpinnerGap } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useState } from "react";

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

/** The app's Sign in with BOSS state, and its two presses. */
export function useBossSignIn(projectId: string, appKey: string, onChange?: () => void) {
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

  const form = () => {
    const f = new FormData();
    f.set("project_id", projectId);
    f.set("app_key", appKey);
    return f;
  };
  async function press(which: "use" | "stop") {
    setBusy(which);
    const r = await callAction(which === "use" ? useSignInWithBossAction : stopSignInWithBossAction, undefined, "oidc_client:manage")(form());
    setBusy(null);
    if (r.ok) {
      showToast("success", r.message ?? (which === "use" ? "Sign in with BOSS is on" : "Sign in with BOSS is off"));
      setError("");
      void load();
      onChange?.();
    } else setError(r.error ?? "");
  }
  return { status, loaded, error, busy, use: () => press("use"), stop: () => press("stop") };
}

/** What the BOSS row says and does once BOSS has a public address. */
export function BossSignInDetails({ state }: { state: ReturnType<typeof useBossSignIn> }) {
  const { status: s, loaded, error, busy } = state;
  const envs = (["preview", "deploy"] as const).filter((e) => (s?.secrets[e] ?? []).length === 3);
  return (
    <div className="tpl-boss" data-testid="fx-signin-panel">
      {!loaded ? <span className="tpl-muted"><SpinnerGap size={12} className="ag-spin" aria-hidden /> Loading</span> : null}
      {loaded && s && !s.template_applied ? (
        <p className="tpl-muted" data-testid="fx-signin-needs-template">
          Apply the login-oauth template to this app first: its BOSS button is what this turns on.
        </p>
      ) : null}
      {loaded && s?.template_applied ? (
        <>
          <p className="fx-signin-state" data-testid="fx-signin-state">
            {s.connected ? "Sign in with BOSS is on" : s.client ? "Registered, not wired" : "Sign in with BOSS is off"}
            {s.connected ? <span className="tpl-muted" title="OIDC_BOSS_CLIENT_ID, _SECRET and _ISSUER, in the vault">
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
          <span className="tpl-prov-acts">
            <GatedButton permission="oidc_client:manage" type="button" className="cap-btn primary"
                         disabled={busy !== null} data-testid="fx-signin-use" aria-busy={busy === "use" || undefined}
                         title={s.connected ? "Register the app's current preview and deploy addresses again"
                           : `${SIGN_IN_TIP} BOSS registers the app as its client and stores its settings. Asks you to confirm it is you.`}
                         onClick={() => void state.use()}>
              {busy === "use" ? <SpinnerGap size={12} className="ag-spin" aria-hidden /> : null}
              {s.connected ? "Refresh addresses" : "Use Sign in with BOSS"}
            </GatedButton>
            {s.client ? (
              <GatedButton permission="oidc_client:manage" type="button" className="cap-btn" disabled={busy !== null}
                           data-testid="fx-signin-stop" title="Removes the client: everyone signed in through it is signed out"
                           onClick={() => void state.stop()}>Stop using</GatedButton>
            ) : null}
          </span>
          <p className="tpl-faint">
            A preview gets these settings when it starts; a new preview&apos;s address is registered as it starts.
          </p>
        </>
      ) : null}
      {error ? <span className="tpl-err" role="alert" data-user-text>{error}</span> : null}
    </div>
  );
}
