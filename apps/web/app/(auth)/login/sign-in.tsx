"use client";

/**
 * SIGN IN (mockup e, IDENTITY.md §9.7, D33): Kaappu's layout in BOSS style.
 *
 *   mark and welcome → the configured provider row (hidden when none) → "Sign in with a passkey"
 *   → "or use email" → Password | Magic link | Email code (the email tabs only with SMTP)
 *   → footer: "Ask an admin to invite you", or "New here? Create account" when self sign-up is on.
 *
 * Then the second factor (code, passkey or recovery code) when the account has one. No phone or
 * SMS anywhere. The API decides every step; the page only carries the person through it, and
 * keeps every message uniform (an unknown address gets exactly the words a known one does).
 */
import { afterEnrolStep, ENROLMENT_GRANT_WORDS } from "../../../lib/identity/enrolment";
import React, { useEffect, useState } from "react";
import { ArrowRight, CaretRight, EnvelopeSimple, Fingerprint, Key, LockSimple, ShieldCheck } from "@phosphor-icons/react";

import { IdentityError, idPost } from "../../../lib/identity/api";
import type { AuthOptions, SignInStep } from "../../../lib/identity/types";
import { getAssertion, PasskeyCancelled, webauthnAvailable, type AssertionOptions, type Wrapped } from "../../../lib/identity/webauthn";
import { CodeInput, Face } from "../../../components/identity/kit";
import { AuthStage, Live, Logo, MethodTabs, ProviderRow, safeNextClient } from "../auth-parts";

type Method = "password" | "magic" | "code";
type Stage =
  | { kind: "start" }
  | { kind: "magic-sent"; email: string }
  | { kind: "code-entry"; email: string }
  | { kind: "mfa"; step: SignInStep }
  | { kind: "recovery"; step: SignInStep }
  | { kind: "passkey"; purpose: "login" | "mfa"; step?: SignInStep }
  | { kind: "done" };

const WORDS: Record<string, string> = {
  invalid_credentials: "Wrong email or password",
  invalid_code: "That code didn't work",
  code_invalid: "That code didn't work",
  rate_limited: "Too many tries — wait a minute",
  busy: "Busy — try again in a moment",
  account_disabled: "This account is disabled. Ask an admin.",
  identity_unavailable: "BOSS can't be reached",
  sso_required: "Your organisation signs in with single sign-on only.",
  second_factor_required: "Your role needs a second factor. Sign in with your password.",
  mfa_challenge_invalid: "That sign-in expired. Start again.",
  passkey_failed: "That passkey didn't work",
  passkey_unavailable: "Passkeys aren't set up on this BOSS yet",
  insecure_origin: "Open BOSS over its HTTPS address",
};

function say(e: unknown): string {
  if (e instanceof PasskeyCancelled) return "Passkey cancelled";
  if (e instanceof IdentityError) {
    if (e.code === "invalid_code" && e.attemptsLeft !== null) return `That code didn't work · ${e.attemptsLeft} left`;
    return WORDS[e.code] ?? e.message;
  }
  return "That didn't work";
}

/**
 * Magic link and email code send an email whose link names BOSS's public URL. Without SMTP there is
 * no email, and without a public URL (H2: `public_origin` null) the API refuses them
 * `409 public_origin_unset` — so neither is offered. An API that does not report the URL is trusted.
 */
export function emailSignInOffered(options: Pick<AuthOptions, "smtp" | "public_origin">): boolean {
  return options.smtp && options.public_origin !== null;
}

/**
 * `initialStep`: single sign-on came back needing a second factor (`/signin?step=mfa`; the
 * pre-auth cookie holds the rest). `notice`: why single sign-on did not finish
 * (`/signin?error=<code>`), already in words.
 */
export function SignIn({ options, next, initialStep = null, notice = null }: {
  options: AuthOptions; next: string; initialStep?: SignInStep | null; notice?: string | null;
}) {
  const [method, setMethod] = useState<Method>("password");
  const [stage, setStage] = useState<Stage>(initialStep ? { kind: "mfa", step: initialStep } : { kind: "start" });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(notice);
  const [resendIn, setResendIn] = useState(0);
  const [hasPasskeys, setHasPasskeys] = useState(true);
  const target = safeNextClient(next);

  // The API offers passkeys only once a public origin is bound (S1 `passkey`); the stub omits it.
  useEffect(() => { setHasPasskeys(webauthnAvailable() && options.passkey !== false); }, [options.passkey]);
  useEffect(() => {
    if (resendIn <= 0) return;
    const t = window.setTimeout(() => setResendIn((n) => n - 1), 1000);
    return () => window.clearTimeout(t);
  }, [resendIn]);

  const after = (step: SignInStep) => {
    setCode("");
    if (step.next === "done") { setStage({ kind: "done" }); window.location.assign(target); return; }
    if (step.next === "enrol") {
      // H1: a sign-in that may not enrol says so here instead of opening a refused enrol screen.
      if (step.enrolment_grant === false) { setStage({ kind: "start" }); setError(ENROLMENT_GRANT_WORDS); return; }
      window.location.assign(afterEnrolStep(step, target)); return;
    }
    const methods = step.methods ?? [];
    setStage(methods.includes("totp") || !methods.includes("passkey") ? { kind: "mfa", step } : { kind: "passkey", purpose: "mfa", step });
  };

  const run = async (call: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try { await call(); } catch (e) {
      setError(say(e));
      // The pre-auth step expired: the second factor can no longer finish this sign-in.
      if (e instanceof IdentityError && e.code === "mfa_challenge_invalid") { setStage({ kind: "start" }); setCode(""); }
    } finally { setBusy(false); }
  };

  const passkey = (purpose: "login" | "mfa", step?: SignInStep) => run(async () => {
    setStage({ kind: "passkey", purpose, step });
    const opts = await idPost<Wrapped<AssertionOptions>>("/v1/auth/webauthn/options", { purpose });
    const credential = await getAssertion(opts);
    after(await idPost<SignInStep>("/v1/auth/webauthn/verify", { purpose, credential }));
  });

  const tabs: { id: Method; label: string }[] = emailSignInOffered(options)
    ? [{ id: "password", label: "Password" }, { id: "magic", label: "Magic link" }, { id: "code", label: "Email code" }]
    : [{ id: "password", label: "Password" }];

  // ---- second factor ---------------------------------------------------------------------------
  if (stage.kind === "mfa" || stage.kind === "recovery") {
    const who = stage.step.who;
    const recovery = stage.kind === "recovery";
    return (
      <AuthStage label="Second factor">
        <Logo />
        {who ? (
          <div className="idn-who-s">
            <Face name={who.display_name} tone={who.hue} size={34} />
            <span><b>{who.display_name}</b><small>{who.email}</small></span>
            <button type="button" onClick={() => { setStage({ kind: "start" }); setError(null); }}>Not you?</button>
          </div>
        ) : null}
        <h1>{recovery ? "Use a recovery code" : "Enter your code"}</h1>
        <p className="idn-s" data-copy="prose">
          {recovery ? "One of the ten codes you saved. Each works once." : <>Open your authenticator app and enter the 6-digit code for <b>BOSS · {options.org_name}</b>.</>}
        </p>
        {recovery ? (
          <form onSubmit={(e) => { e.preventDefault(); run(async () => after(await idPost<SignInStep>("/v1/auth/mfa/recovery", { code }))); }}>
            <div className="idn-f">
              <label htmlFor="rc">Recovery code</label>
              <input id="rc" className="idn-i" autoComplete="one-time-code" spellCheck={false} placeholder="XXXX-XXXX-XXXX" value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())} aria-invalid={!!error || undefined} autoFocus />
            </div>
            <button type="submit" className="idn-big p" disabled={busy || code.replace(/[\s-]/g, "").length < 12}>Verify</button>
          </form>
        ) : (
          <>
            <CodeInput value={code} onChange={setCode} label="Authenticator code" invalid={!!error}
              onComplete={(v) => run(async () => after(await idPost<SignInStep>("/v1/auth/mfa/totp", { code: v })))} />
            <button type="button" className="idn-big p" disabled={busy || code.length < 6}
              onClick={() => run(async () => after(await idPost<SignInStep>("/v1/auth/mfa/totp", { code })))}>{busy ? "Checking…" : "Verify"}</button>
          </>
        )}
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        <div className="idn-alt">
          {stage.step.methods.includes("passkey") && hasPasskeys ? (
            <button type="button" onClick={() => passkey("mfa", stage.step)}><Fingerprint size={16} aria-hidden /> Use a passkey instead <CaretRight size={13} className="idn-chev" aria-hidden /></button>
          ) : null}
          {recovery ? (
            <button type="button" onClick={() => { setStage({ kind: "mfa", step: stage.step }); setCode(""); setError(null); }}><ShieldCheck size={16} aria-hidden /> Use your authenticator <CaretRight size={13} className="idn-chev" aria-hidden /></button>
          ) : (
            <button type="button" onClick={() => { setStage({ kind: "recovery", step: stage.step }); setCode(""); setError(null); }}><LockSimple size={16} aria-hidden /> Use a recovery code <CaretRight size={13} className="idn-chev" aria-hidden /></button>
          )}
        </div>
        <p className="idn-fine" data-copy="prose">5 tries per sign-in. Lost every factor? An admin can reset it.</p>
        <Live text={busy ? "Checking" : ""} />
      </AuthStage>
    );
  }

  // ---- waiting for the passkey ----------------------------------------------------------------
  if (stage.kind === "passkey") {
    return (
      <AuthStage label="Passkey">
        <Logo />
        <h1 className="idn-center">Use your passkey</h1>
        <p className="idn-s idn-center" data-copy="prose">Your device is waiting. Confirm it's you to sign in to <b>{options.host}</b>.</p>
        <div className="idn-pk" aria-hidden>
          <span className="idn-wave w1" /><span className="idn-wave w2" /><span className="idn-wave w3" />
          <span className="idn-core"><Fingerprint size={40} weight="bold" /></span>
        </div>
        <div className="idn-state" role="status">{error ? <span className="idn-err">{error}</span> : <><span className="idn-blink" aria-hidden /> Waiting for your device…</>}</div>
        <div className="idn-alt">
          <button type="button" onClick={() => passkey(stage.purpose, stage.step)}><Key size={16} aria-hidden /> {error ? "Try again" : "Use a security key"} <CaretRight size={13} className="idn-chev" aria-hidden /></button>
          <button type="button" onClick={() => { setError(null); setStage(stage.step ? { kind: "mfa", step: stage.step } : { kind: "start" }); }}>
            <EnvelopeSimple size={16} aria-hidden /> {stage.step ? "Use a code instead" : "Use email instead"} <CaretRight size={13} className="idn-chev" aria-hidden />
          </button>
        </div>
        <p className="idn-fine" data-copy="prose">Your passkey never leaves your device. BOSS stores only its public key.</p>
      </AuthStage>
    );
  }

  // ---- the first screen -----------------------------------------------------------------------
  return (
    <AuthStage label="Sign in">
      <Logo host={options.host} />
      <h1>Welcome back</h1>
      <p className="idn-s" data-copy="prose">Sign in to the {options.org_name} to pick up where your team left off.</p>
      {options.claim ? (
        <a className="idn-note info" href="/claim" data-testid="claim-door">
          <ShieldCheck size={16} aria-hidden /> <span><b>This BOSS has no Owner yet.</b> Claim it <ArrowRight size={12} aria-hidden /></span>
        </a>
      ) : null}
      <ProviderRow providers={options.providers} next={target} />
      {hasPasskeys ? (
        <button type="button" className="idn-big p" onClick={() => passkey("login")} disabled={busy} data-testid="passkey-sign-in">
          <Fingerprint size={18} weight="bold" aria-hidden /> Sign in with a passkey
        </button>
      ) : null}
      <div className="idn-or">{hasPasskeys || options.providers.length ? "or use email" : "sign in with email"}</div>
      <MethodTabs tabs={tabs} value={method} onChange={(m) => { setMethod(m); setError(null); setStage({ kind: "start" }); }} label="Sign-in method" />

      {stage.kind === "magic-sent" ? (
        <div className="idn-sent" id="panel-magic" role="tabpanel" aria-labelledby="tab-magic">
          <div className="idn-env" aria-hidden><EnvelopeSimple size={22} /></div>
          <h2>Check your email</h2>
          <p className="idn-s" data-copy="prose">If an account exists for that address, we sent a link. It works once, for 10 minutes, in this browser.</p>
          <div className="idn-row">
            <button type="button" className="idn-btn sm" disabled={resendIn > 0 || busy}
              onClick={() => run(async () => { await idPost("/v1/auth/magic-link", { email: stage.email }); setResendIn(60); })}>
              {resendIn > 0 ? `Resend in 0:${String(resendIn).padStart(2, "0")}` : "Resend"}
            </button>
            <span className="idn-spacer" />
            <button type="button" className="idn-link" onClick={() => setStage({ kind: "start" })}>Use a different email</button>
          </div>
        </div>
      ) : stage.kind === "code-entry" ? (
        <div id="panel-code" role="tabpanel" aria-labelledby="tab-code">
          <p className="idn-s" data-copy="prose">If an account exists for that address, we sent a 6-digit code. It works once, for 10 minutes.</p>
          <CodeInput value={code} onChange={setCode} label="Email code" invalid={!!error}
            onComplete={(v) => run(async () => after(await idPost<SignInStep>("/v1/auth/email-code/verify", { code: v })))} />
          <button type="button" className="idn-big p" disabled={busy || code.length < 6}
            onClick={() => run(async () => after(await idPost<SignInStep>("/v1/auth/email-code/verify", { code })))}>{busy ? "Checking…" : "Verify"}</button>
          {error ? <div className="idn-err" role="alert">{error}</div> : null}
          <div className="idn-row idn-fine">
            <span>5 tries, then request a new code</span>
            <span className="idn-spacer" />
            <button type="button" className="idn-link" disabled={resendIn > 0}
              onClick={() => run(async () => { await idPost("/v1/auth/email-code", { email: stage.email }); setResendIn(60); setCode(""); })}>
              {resendIn > 0 ? `Resend in ${resendIn}s` : "Resend"}
            </button>
          </div>
        </div>
      ) : (
        <form id={`panel-${method}`} role={tabs.length > 1 ? "tabpanel" : undefined} aria-labelledby={tabs.length > 1 ? `tab-${method}` : undefined}
          onSubmit={(e) => {
            e.preventDefault();
            if (method === "password") run(async () => after(await idPost<SignInStep>("/v1/auth/login", { email, password })));
            if (method === "magic") run(async () => { await idPost("/v1/auth/magic-link", { email }); setStage({ kind: "magic-sent", email }); setResendIn(42); });
            if (method === "code") run(async () => { await idPost("/v1/auth/email-code", { email }); setStage({ kind: "code-entry", email }); setResendIn(60); });
          }}>
          <div className="idn-f">
            <label htmlFor="email">Email</label>
            <input id="email" className="idn-i" type="email" autoComplete="username webauthn" required value={email}
              onChange={(e) => setEmail(e.target.value)} aria-invalid={!!error || undefined} aria-describedby={error ? "signin-error" : undefined} />
          </div>
          {method === "password" ? (
            <div className="idn-f">
              <span className="idn-flabel">
                <label htmlFor="password">Password</label>
                {emailSignInOffered(options)
                  ? <a href="/reset">Forgot password?</a>
                  : <span className="idn-dim" title="An admin can send you a reset link">Ask an admin</span>}
              </span>
              <input id="password" className="idn-i" type="password" autoComplete="current-password" required value={password}
                onChange={(e) => setPassword(e.target.value)} aria-invalid={!!error || undefined} />
            </div>
          ) : null}
          {error ? <div className="idn-err" id="signin-error" role="alert">{error}</div> : null}
          <button type="submit" className={`idn-big ${hasPasskeys ? "s" : "p"}`} disabled={busy} data-testid="email-submit">
            {busy ? "Signing in…" : method === "password" ? "Sign in" : method === "magic" ? "Email me a link" : "Email me a code"}
          </button>
        </form>
      )}

      <div className="idn-foot">
        {options.signup
          ? <>New here? <a href="/signup">Create account</a></>
          : <>No account? <span className="idn-mut">Ask an admin to invite you</span></>}
      </div>
      <Live text={busy ? "Working" : stage.kind === "done" ? "Signed in" : ""} />
    </AuthStage>
  );
}
