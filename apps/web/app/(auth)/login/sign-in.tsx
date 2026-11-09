"use client";

/**
 * SIGN IN (IDENTITY.md §9.7, D33): a familiar layout in BOSS style.
 *
 *   the BOSS mark and "Welcome back" → the provider squares (Google, GitHub, Microsoft, Apple and
 *   any configured OIDC/SAML provider) → "Sign in with a passkey" → "or use email" → Password |
 *   Magic link | Email code → email, password (show/hide, "Forgot?") → "Sign in →"
 *   → "New here? Create account" → the host, and "Use the shared password" while it still works.
 *
 * What `/v1/auth/options` says isn't set up stays discoverable but disabled, and says exactly
 * what turns it on (lib/identity/sign-in-offers.ts): no fake
 * buttons. Then the second factor (code, passkey or recovery code) when the account has one. No
 * phone or SMS anywhere. The API decides every step; the page only carries the person through
 * it, and keeps every message uniform (an unknown address gets exactly the words a known one does).
 *
 * PASSWORD FIRST, PASSKEY ON REQUEST (BOSSAPP-812): the page opens on the Password tab with the
 * email field focused and NEVER starts a WebAuthn ceremony by itself (a browser without your
 * passkey popped "Insert your security key" and there was nothing else to do). "Sign in with a
 * passkey" is a complete sign-in on its own, started only by its button; if it fails or is
 * cancelled, one calm line points back to email and password. Email + password then asks for the
 * second factor the account has: the authenticator code, or its passkey (on a click), the user
 * can switch between them, and a recovery code always works.
 */
import { afterEnrolStep, ENROLMENT_GRANT_WORDS } from "../../../lib/identity/enrolment";
import React, { useEffect, useRef, useState } from "react";
import { ArrowRight, CaretRight, EnvelopeSimple, Eye, EyeSlash, Fingerprint, Hash, Key, LockSimple, ShieldCheck, Sparkle, UserPlus } from "@phosphor-icons/react";

import { IdentityError, idPost } from "../../../lib/identity/api";
import type { AuthOptions, SignInStep } from "../../../lib/identity/types";
import { emailSignInOffered } from "../../../lib/identity/email-sign-in";
import { conditionalMediationAvailable, getAssertion, PasskeyFailure, unwrapOptions, webauthnAvailable, type AssertionOptions, type Wrapped } from "../../../lib/identity/webauthn";
import { sayAuth } from "../auth-flow";
import { passkeyPlace } from "../../../lib/identity/passkey-support";
import { methodWhyNot, passkeyWhyNot, providerOffers, type Method } from "../../../lib/identity/sign-in-offers";
import { CodeInput, Face, Sheet } from "../../../components/identity/kit";
import { AuthStage, HostLine, Live, Logo, MethodTabs, ProviderRow, safeNextClient, Tip } from "../auth-parts";
import { CopyCommand } from "../copy-command";
import { HOST_COMMANDS } from "../../../lib/identity/host-commands";
import { localWith } from "../../../lib/local-time";

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
  rate_limited: "Too many tries, wait a minute",
  busy: "Busy, try again in a moment",
  account_disabled: "This account is disabled. Ask an admin.",
  identity_unavailable: "BOSS can't be reached",
  sso_required: "Your organisation signs in with single sign-on only.",
  second_factor_required: "Your role needs a second factor. Sign in with your password.",
  mfa_challenge_invalid: "That sign-in expired. Start again.",
  passkey_failed: "That passkey didn't work",
  passkey_unavailable: "Passkeys aren't set up on this BOSS yet",
  insecure_origin: "Open BOSS over its HTTPS address",
  claim_first: "This BOSS has no Owner yet. Claim it first.",
  shared_password_retired: "The shared password no longer works. Ask an Owner to invite you.",
  // The server named the refusal. "Reload and retry" hides that (BOSSAPP-671).
  csrf_failed: "Sign-in was refused (csrf_failed). This page's address didn't match BOSS. Open it at its own address and try again.",
};

/** "6 Nov" for the shared password's last day (local time). */
function shortDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  return localWith(iso, { day: "numeric", month: "short" }) || null;
}

function say(e: unknown): string {
  if (e instanceof IdentityError) {
    // A sentence the server wrote beats the short map, including csrf_failed's "Reload and retry".
    if (e.detail && /\s/.test(e.detail)) return e.detail;
    if (e.code === "invalid_code" && e.attemptsLeft !== null) return `That code didn't work: ${e.attemptsLeft} left`;
    return WORDS[e.code] ?? sayAuth(e);
  }
  return sayAuth(e);
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
  // The shared console password signs in with no email (Operator, until its end date).
  const [shared, setShared] = useState(false);
  const sharedUntil = shortDate(options.shared_password?.until);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(notice);
  const [resendIn, setResendIn] = useState(0);
  const [showPassword, setShowPassword] = useState(false);
  // Why a disabled option is off, in words, under the group that was pressed (a phone has no hover).
  const [why, setWhy] = useState<{ at: "idp" | "passkey" | "tabs"; text: string } | null>(null);
  const [sheet, setSheet] = useState<null | "invite-only" | "forgot">(null);
  // The page's own address and this browser's passkey support are known only once it runs here.
  const [where, setWhere] = useState<{ hostname: string; protocol: string; secure: boolean } | null>(null);
  const [webauthn, setWebauthn] = useState(false);
  const target = safeNextClient(next);
  const emailRef = useRef<HTMLInputElement | null>(null);
  const passwordRef = useRef<HTMLInputElement | null>(null);
  /** The calm line after a passkey sign-in that did not happen (cancelled, or no passkey here). */
  const [noPasskeyHere, setNoPasskeyHere] = useState(false);

  // The API offers passkeys only once a public origin is bound (S1 `passkey`, `passkey_reason`),
  // and never on an address a browser can't hold a passkey for (an IP, plain http: passkey-support).
  useEffect(() => {
    setWhere(passkeyPlace());
    setWebauthn(webauthnAvailable());
  }, []);
  const passkeyWhy = passkeyWhyNot(options, where, webauthn);
  const hasPasskeys = where !== null && passkeyWhy === null;
  // PASSKEY AUTOFILL (conditional mediation): where the browser supports it, the passkeys saved
  // here appear in the email field's autofill. No modal, no "Insert your security key": the
  // browser answers only when the person picks one. Aborted before any other passkey request.
  const autofill = useRef<AbortController | null>(null);
  const stopAutofill = () => { autofill.current?.abort(); autofill.current = null; };
  useEffect(() => {
    if (!hasPasskeys || initialStep) return;
    let live = true;
    (async () => {
      if (!(await conditionalMediationAvailable()) || !live) return;
      const ctrl = new AbortController();
      autofill.current = ctrl;
      try {
        const opts = await idPost<Wrapped<AssertionOptions>>("/v1/auth/webauthn/options", { purpose: "login" });
        if (!live || ctrl.signal.aborted || unwrapOptions(opts).stub) return;
        const credential = await getAssertion(opts, { conditional: true, signal: ctrl.signal });
        if (!live || ctrl.signal.aborted) return;
        setBusy(true);
        after(await idPost<SignInStep>("/v1/auth/webauthn/verify", { purpose: "login", credential }));
      } catch {
        // Aborted, dismissed, or not offered: the page stays as it is, quietly.
      } finally {
        if (live) setBusy(false);
        if (autofill.current === ctrl) autofill.current = null;
      }
    })();
    return () => { live = false; stopAutofill(); };
  }, [hasPasskeys]); // eslint-disable-line react-hooks/exhaustive-deps
  const sharedOffered = !!options.shared_password && (!options.shared_password.until || Date.parse(options.shared_password.until) > Date.now());
  useEffect(() => {
    if (resendIn <= 0) return;
    const t = window.setTimeout(() => setResendIn((n) => n - 1), 1000);
    return () => window.clearTimeout(t);
  }, [resendIn]);

  const after = (step: SignInStep) => {
    setCode("");
    if (step.next === "done") {
      setStage({ kind: "done" });
      // §4.7: a recovery-code sign-in that left fewer than 3 codes goes straight to making new ones.
      window.location.assign(step.must_regenerate_recovery_codes ? "/account" : target);
      return;
    }
    if (step.next === "enrol") {
      // H1: a sign-in that may not enrol says so here instead of opening a refused enrol screen.
      if (step.enrolment_grant === false) { setStage({ kind: "start" }); setError(ENROLMENT_GRANT_WORDS); return; }
      window.location.assign(afterEnrolStep(step, target)); return;
    }
    // The second factor screen. It never starts a passkey ceremony by itself: the passkey is a
    // button there, pressed by the person (BOSSAPP-812).
    setStage({ kind: "mfa", step });
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

  const passkey = (purpose: "login" | "mfa", step?: SignInStep) => {
    stopAutofill();
    if (purpose === "login") return passkeyLogin();
    return run(async () => {
      setStage({ kind: "passkey", purpose, step });
      const opts = await idPost<Wrapped<AssertionOptions>>("/v1/auth/webauthn/options", { purpose });
      const credential = await getAssertion(opts);
      after(await idPost<SignInStep>("/v1/auth/webauthn/verify", { purpose, credential }));
    });
  };

  /** "Sign in with a passkey": a whole sign-in on its own, started only by its button. A device
   *  that has no passkey here (or a cancel) comes back to email and password, calmly. */
  const passkeyLogin = async () => {
    setBusy(true);
    setError(null);
    setNoPasskeyHere(false);
    setStage({ kind: "passkey", purpose: "login" });
    try {
      const opts = await idPost<Wrapped<AssertionOptions>>("/v1/auth/webauthn/options", { purpose: "login" });
      const credential = await getAssertion(opts);
      after(await idPost<SignInStep>("/v1/auth/webauthn/verify", { purpose: "login", credential }));
    } catch (e) {
      setStage({ kind: "start" });
      setMethod("password");
      if (e instanceof PasskeyFailure) setNoPasskeyHere(true);
      else setError(say(e));
      window.setTimeout(() => (email ? passwordRef.current : emailRef.current)?.focus(), 0);
    } finally { setBusy(false); }
  };

  const tabs = [
    { id: "password" as const, label: "Password", icon: <Key size={14} weight="bold" aria-hidden /> },
    { id: "magic" as const, label: "Magic link", icon: <Sparkle size={14} weight="bold" aria-hidden />, why: methodWhyNot("magic", options) },
    { id: "code" as const, label: "Email code", icon: <Hash size={14} weight="bold" aria-hidden />, why: methodWhyNot("code", options) },
  ];
  const whyLine = (at: "idp" | "passkey" | "tabs") => why?.at === at
    ? <p className="idn-why" data-copy="prose" data-testid={`why-${at}`}>{why.text}</p> : null;

  // ---- second factor ---------------------------------------------------------------------------
  if (stage.kind === "mfa" || stage.kind === "recovery") {
    const who = stage.step.who;
    const recovery = stage.kind === "recovery";
    const methods = stage.step.methods ?? [];
    // The authenticator code is the default second step; a passkey-only account gets its passkey
    // as a button (never auto-started). Either way the person can switch.
    const codeOffered = methods.includes("totp") || !methods.includes("passkey");
    const passkeyOffered = methods.includes("passkey") && hasPasskeys;
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
        <h1>{recovery ? "Use a recovery code" : codeOffered ? "Enter your code" : "Use your passkey"}</h1>
        <p className="idn-s" data-copy="prose">
          {recovery ? "One of the ten codes you saved. Each works once."
            : codeOffered ? <>Open your authenticator app and enter the 6-digit code for <b>BOSS, {options.org_name}</b>.</>
            : <>Your password was right. Confirm it&apos;s you with the passkey for <b>{options.host}</b>.</>}
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
        ) : !codeOffered ? (
          passkeyOffered ? (
            <button type="button" className="idn-big p" disabled={busy} data-testid="mfa-passkey"
              onClick={() => passkey("mfa", stage.step)}><Fingerprint size={18} weight="bold" aria-hidden /> Use your passkey</button>
          ) : <p className="idn-s" data-copy="prose">{passkeyWhy ?? "Passkeys aren't available in this browser."} Use a recovery code.</p>
        ) : (
          <>
            <CodeInput value={code} onChange={setCode} label="Authenticator code" invalid={!!error}
              onComplete={(v) => run(async () => after(await idPost<SignInStep>("/v1/auth/mfa/totp", { code: v })))} />
            <button type="button" className="idn-big p" disabled={busy || code.length < 6}
              onClick={() => run(async () => after(await idPost<SignInStep>("/v1/auth/mfa/totp", { code })))}>{busy ? "Checking…" : "Verify"}</button>
          </>
        )}
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        <p className="idn-fine" data-copy="prose" data-testid="try-another-way">Try another way</p>
        <div className="idn-alt" role="group" aria-label="Try another way">
          {codeOffered && passkeyOffered ? (
            <button type="button" onClick={() => passkey("mfa", stage.step)}><Fingerprint size={16} aria-hidden /> Use a passkey instead <CaretRight size={13} className="idn-chev" aria-hidden /></button>
          ) : null}
          {recovery ? (
            <button type="button" onClick={() => { setStage({ kind: "mfa", step: stage.step }); setCode(""); setError(null); }}><ShieldCheck size={16} aria-hidden /> {codeOffered ? "Use your authenticator" : "Use your passkey"} <CaretRight size={13} className="idn-chev" aria-hidden /></button>
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
        <div className="idn-wait" role="status">{error ? <span className="idn-err">{error}</span> : <><span className="idn-blink" aria-hidden /> Waiting for your device…</>}</div>
        <div className="idn-alt">
          <button type="button" onClick={() => passkey(stage.purpose, stage.step)}><Key size={16} aria-hidden /> {error ? "Try again" : "Use a security key"} <CaretRight size={13} className="idn-chev" aria-hidden /></button>
          <button type="button" onClick={() => {
            setError(null); stopAutofill(); setStage(stage.step ? { kind: "mfa", step: stage.step } : { kind: "start" });
            if (!stage.step) { setMethod("password"); window.setTimeout(() => (email ? passwordRef.current : emailRef.current)?.focus(), 0); }
          }}>
            <EnvelopeSimple size={16} aria-hidden /> {stage.step ? ((stage.step.methods ?? []).includes("totp") ? "Use a code instead" : "Try another way") : "Use your password instead"} <CaretRight size={13} className="idn-chev" aria-hidden />
          </button>
        </div>
        <p className="idn-fine" data-copy="prose">Your passkey never leaves your device. BOSS stores only its public key.</p>
      </AuthStage>
    );
  }

  // ---- the first screen -----------------------------------------------------------------------
  const passkeyButton = (tip?: string) => (
    <button type="button" className="idn-big pk" aria-disabled={passkeyWhy ? true : undefined} aria-describedby={tip}
      disabled={busy} data-testid="passkey-sign-in"
      onClick={() => (passkeyWhy ? setWhy({ at: "passkey", text: passkeyWhy }) : passkey("login"))}>
      <Fingerprint size={20} weight="bold" aria-hidden /> Sign in with a passkey
    </button>
  );
  const offers = providerOffers(options.providers, target);
  const hasExternalMethod = offers.some((offer) => !!offer.href) || hasPasskeys;
  const externalMethods = <>
    <ProviderRow offers={offers} onWhy={(text) => setWhy({ at: "idp", text })} />
    {whyLine("idp")}
    {passkeyWhy ? <Tip text={passkeyWhy} className="block">{(tip) => passkeyButton(tip)}</Tip> : passkeyButton()}
    {whyLine("passkey")}
  </>;
  return (
    <AuthStage label="Sign in">
      <Logo />
      <h1>Welcome back</h1>
      <p className="idn-s" data-copy="prose">Sign in to {options.org_name}.</p>
      {options.claim ? (
        <div className="idn-claim" data-testid="claim-how">
          <a className="idn-note info" href="/claim" data-testid="claim-door">
            <ShieldCheck size={16} aria-hidden /> <span><b>This BOSS has no Owner yet.</b> Claim it <ArrowRight size={12} aria-hidden /></span>
          </a>
        </div>
      ) : null}
      {hasExternalMethod ? <>
        {externalMethods}
        <div className="idn-or">or use email</div>
      </> : <details className="idn-other-signin" data-testid="other-signin-options">
        <summary>Other sign-in options</summary>
        {externalMethods}
      </details>}
      <MethodTabs tabs={tabs} value={method} label="Sign-in method" onWhy={(text) => setWhy({ at: "tabs", text })}
        onChange={(m) => { setMethod(m); setError(null); setWhy(null); setStage({ kind: "start" }); }} />
      {whyLine("tabs")}

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
        <form id={`panel-${method}`} role="tabpanel" aria-labelledby={`tab-${method}`}
          onSubmit={(e) => {
            e.preventDefault();
            if (method === "password") run(async () => after(await idPost<SignInStep>("/v1/auth/login", { email: shared ? "" : email, password })));
            if (method === "magic") run(async () => { await idPost("/v1/auth/magic-link", { email }); setStage({ kind: "magic-sent", email }); setResendIn(42); });
            if (method === "code") run(async () => { await idPost("/v1/auth/email-code", { email }); setStage({ kind: "code-entry", email }); setResendIn(60); });
          }}>
          {shared && method === "password" ? null : (
            <div className="idn-f">
              <label htmlFor="email">Email</label>
              <div className="idn-ig">
                <EnvelopeSimple size={18} className="idn-ig-ico" aria-hidden />
                <input id="email" ref={emailRef} className="idn-i" type="email" autoComplete="username webauthn" required value={email} placeholder="you@example.com"
                  autoFocus onChange={(e) => setEmail(e.target.value)} aria-invalid={!!error || undefined} aria-describedby={error ? "signin-error" : undefined} />
              </div>
            </div>
          )}
          {method === "password" ? (
            <div className="idn-f">
              <span className="idn-flabel">
                <label htmlFor="password">{shared ? "Shared password" : "Password"}</label>
                {shared ? null : emailSignInOffered(options)
                  ? <a href="/reset" data-testid="forgot">Forgot?</a>
                  : <button type="button" data-testid="forgot" onClick={() => setSheet("forgot")}>Forgot?</button>}
              </span>
              <div className="idn-ig">
                <LockSimple size={18} className="idn-ig-ico" aria-hidden />
                <input id="password" ref={passwordRef} className="idn-i" type={showPassword ? "text" : "password"} autoComplete="current-password" required value={password}
                  placeholder="••••••••••" onChange={(e) => setPassword(e.target.value)} aria-invalid={!!error || undefined} />
                <button type="button" className="idn-eye" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword}
                  onClick={() => setShowPassword(!showPassword)}>
                  {showPassword ? <EyeSlash size={18} aria-hidden /> : <Eye size={18} aria-hidden />}
                </button>
              </div>
            </div>
          ) : null}
          {shared && method === "password" ? (
            <p className="idn-fine" data-copy="prose" data-testid="shared-password-note">
              Signs you in as Operator{sharedUntil ? ` until ${sharedUntil}` : ""}. Then ask an Owner to invite you.
            </p>
          ) : null}
          {noPasskeyHere && method === "password" ? (
            <p className="idn-s" role="status" data-copy="prose" data-testid="no-passkey-here-line">No passkey here? Use your password instead.</p>
          ) : null}
          {error ? <div className="idn-err" id="signin-error" role="alert">{error}</div> : null}
          <button type="submit" className="idn-big p go" disabled={busy} data-testid="email-submit">
            {busy ? "Signing in…" : method === "password" ? "Sign in" : method === "magic" ? "Email me a link" : "Email me a code"}
            {busy ? null : <ArrowRight size={18} weight="bold" aria-hidden />}
          </button>
        </form>
      )}

      <p className="idn-new">
        New here?{" "}
        {options.claim
          ? <a href="/claim" data-testid="create-account">Create account</a>
          : options.signup
            ? <a href="/signup" data-testid="create-account">Create account</a>
            : <button type="button" data-testid="create-account" onClick={() => setSheet("invite-only")}>Create account</button>}
      </p>
      <HostLine host={options.host}>
        {sharedOffered && method === "password" && stage.kind === "start" ? (
          <button type="button" className="idn-link" data-testid="shared-password-toggle"
            onClick={() => { setShared(!shared); setError(null); setPassword(""); }}>
            {shared ? "Sign in with your email" : "Use the shared password"}
          </button>
        ) : null}
      </HostLine>

      {sheet === "invite-only" ? (
        <Sheet title="This BOSS is invite-only" icon={<UserPlus size={20} aria-hidden />} onClose={() => setSheet(null)} testId="invite-only-sheet">
          <p className="idn-lede2" data-copy="prose">Ask an admin to invite you. Their invitation gives you a link that sets up your account.</p>
          <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn primary" onClick={() => setSheet(null)}>Got it</button></div>
        </Sheet>
      ) : null}
      {sheet === "forgot" ? (
        <Sheet title="Reset your password" icon={<LockSimple size={20} aria-hidden />} onClose={() => setSheet(null)} testId="forgot-sheet">
          <p className="idn-lede2" data-copy="prose">This BOSS sends no email. Ask an admin to send you a reset link.</p>
          <p className="idn-lede2" data-copy="prose">A locked-out sole Owner runs this on the host:</p>
          <CopyCommand command={HOST_COMMANDS.recoverOwner} />
          <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn primary" onClick={() => setSheet(null)}>Got it</button></div>
        </Sheet>
      ) : null}
      <Live text={busy ? "Working" : stage.kind === "done" ? "Signed in" : ""} />
    </AuthStage>
  );
}
