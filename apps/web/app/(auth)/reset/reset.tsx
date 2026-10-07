"use client";

/**
 * FORGOT PASSWORD (IDENTITY.md §4.7): with SMTP, an emailed single-use link (30 minutes); the
 * answer is the same words whether or not the address has an account. Without SMTP, an admin
 * issues the link. Opening the link (`/reset#t=…`) sets a new password; every other session ends,
 * and the second factor is still required to sign in.
 */
import React, { useEffect, useRef, useState } from "react";
import { EnvelopeSimple, ShieldCheck } from "@phosphor-icons/react";

import { idPost } from "../../../lib/identity/api";
import { AuthStage, Live, Logo, linkToken } from "../auth-parts";
import { sayAuth } from "../auth-flow";
import { afterEnrolStep } from "../../../lib/identity/enrolment";

type ResetAnswer = { next?: "signin" | "enrol"; enrolment_grant?: boolean } | null;

export function Reset({ host, smtp, email: initialEmail, minPassword = 12 }: { host: string; smtp: boolean; email: string; minPassword?: number }) {
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState(initialEmail);
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [sent, setSent] = useState(false);
  const [changed, setChanged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Read ONCE: the fragment is cleared right after, so a second run of this effect (React's
  // development double-invoke) must not overwrite the token with "" — the link would read as
  // "Forgot your password?" (seen on the hotfix walk).
  const once = useRef(false);
  useEffect(() => {
    if (once.current) return;
    once.current = true;
    const t = linkToken("reset");
    setToken(t);
    if (t) { try { window.history.replaceState(null, "", window.location.pathname); } catch { /* fine */ } }
  }, []);

  const run = async (call: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try { await call(); } catch (e) { setError(sayAuth(e)); } finally { setBusy(false); }
  };

  if (changed) {
    return (
      <AuthStage label="Password changed">
        <Logo host={host} />
        <div className="idn-sent">
          <div className="idn-env" aria-hidden><ShieldCheck size={22} /></div>
          <h1>Password changed</h1>
          <p className="idn-s" data-copy="prose">You were signed out everywhere else. Sign in with the new password — your second factor is still needed.</p>
        </div>
        <a className="idn-big p" href="/login">Sign in</a>
      </AuthStage>
    );
  }

  if (token) {
    const mismatch = pw2.length > 0 && pw !== pw2;
    return (
      <AuthStage label="New password">
        <Logo host={host} />
        <h1>Choose a new password</h1>
        <p className="idn-s" data-copy="prose">{minPassword} characters or more. A long phrase is easier to remember than symbols.</p>
        <form onSubmit={(e) => { e.preventDefault(); if (pw.length >= minPassword && pw === pw2) run(async () => { const step = await idPost<ResetAnswer>("/v1/auth/password/reset", { token, new_password: pw }); setPw(""); setPw2("");
          // A person with no second factor and a first-factor grant (the host's recover-owner, an
          // admin's MFA reset) is signed in to enrol: straight on, never a dead end at "sign in".
          if (step?.next === "enrol") { window.location.assign(afterEnrolStep(step, "/identity")); return; }
          setChanged(true); }); }} noValidate>
          <div className="idn-f">
            <label htmlFor="new-pw">New password</label>
            <input id="new-pw" className="idn-i" type="password" autoComplete="new-password" minLength={minPassword} required value={pw} onChange={(e) => setPw(e.target.value)} />
          </div>
          <div className="idn-f">
            <label htmlFor="new-pw2">Type it again</label>
            <input id="new-pw2" className="idn-i" type="password" autoComplete="new-password" required value={pw2}
              onChange={(e) => setPw2(e.target.value)} aria-invalid={mismatch || undefined} aria-describedby={mismatch ? "pw-mismatch" : undefined} />
            {mismatch ? <span id="pw-mismatch" className="idn-err">They don&apos;t match</span> : null}
          </div>
          {error ? <div className="idn-err" role="alert">{error}</div> : null}
          <button type="submit" className="idn-big p" disabled={busy || pw.length < minPassword || pw !== pw2}>{busy ? "Saving…" : "Set password"}</button>
        </form>
        <Live text={busy ? "Saving" : ""} />
      </AuthStage>
    );
  }

  if (!smtp) {
    return (
      <AuthStage label="Forgot password">
        <Logo host={host} />
        <h1>Forgot your password?</h1>
        <p className="idn-s" data-copy="prose">This BOSS sends no email. An admin can give you a single-use reset link from People.</p>
        <a className="idn-big s" href="/login">Back to sign in</a>
      </AuthStage>
    );
  }

  return (
    <AuthStage label="Forgot password">
      <Logo host={host} />
      {sent ? (
        <div className="idn-sent" data-testid="reset-sent">
          <div className="idn-env" aria-hidden><EnvelopeSimple size={22} /></div>
          <h1>Check your email</h1>
          <p className="idn-s" data-copy="prose">If an account exists for that address, we sent a reset link. It works once, for 30 minutes.</p>
          <a className="idn-big s" href="/login">Back to sign in</a>
        </div>
      ) : (
        <>
          <h1>Forgot your password?</h1>
          <p className="idn-s" data-copy="prose">We&apos;ll email a link to set a new one. Your second factor stays as it is.</p>
          <form onSubmit={(e) => { e.preventDefault(); run(async () => { await idPost("/v1/auth/password/forgot", { email }); setSent(true); }); }}>
            <div className="idn-f">
              <label htmlFor="reset-email">Email</label>
              <input id="reset-email" className="idn-i" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            {error ? <div className="idn-err" role="alert">{error}</div> : null}
            <button type="submit" className="idn-big p" disabled={busy || !email.includes("@")}>{busy ? "Sending…" : "Email me a link"}</button>
          </form>
          <div className="idn-foot"><a href="/login">Back to sign in</a></div>
        </>
      )}
      <Live text={sent ? "Sent" : ""} />
    </AuthStage>
  );
}
