"use client";

/**
 * SIGN IN (IDENTITY.md §9.7, D33): a familiar layout in BOSS style.
 *
 *   the BOSS mark and "Welcome back" → the provider squares (Google, GitHub, Microsoft, Apple and
 *   any configured OIDC/SAML provider) → "Sign in with a passkey" → "or use email" → Password |
 *   Magic link | Email code → email, password (show/hide, "Forgot?") → "Sign in →"
 *   → "New here? Create account" → the host, and "Use the shared password" while it still works.
 *
 * The page always has that whole shape. What `/v1/auth/options` says isn't set up stays visible
 * but disabled, and says exactly what turns it on (lib/identity/sign-in-offers.ts): no fake
 * buttons. Then the second factor (code, passkey or recovery code) when the account has one. No
 * phone or SMS anywhere. The API decides every step; the page only carries the person through
 * it, and keeps every message uniform (an unknown address gets exactly the words a known one does).
 */
import { afterEnrolStep, ENROLMENT_GRANT_WORDS } from "../../../lib/identity/enrolment";
import React, { useEffect, useState } from "react";
import { ArrowRight, CaretRight, EnvelopeSimple, Eye, EyeSlash, Fingerprint, Hash, Key, LockSimple, ShieldCheck, Sparkle, UserPlus } from "@phosphor-icons/react";

import { IdentityError, idPost } from "../../../lib/identity/api";
import type { AuthOptions, SignInStep } from "../../../lib/identity/types";
import { emailSignInOffered } from "../../../lib/identity/email-sign-in";
import { getAssertion, webauthnAvailable, type AssertionOptions, type Wrapped } from "../../../lib/identity/webauthn";
import { sayAuth } from "../auth-flow";
import { methodWhyNot, passkeyWhyNot, providerOffers, type Method } from "../../../lib/identity/sign-in-offers";
import { CodeInput, Face, Sheet } from "../../../components/identity/kit";
import { AuthStage, HostLine, Live, Logo, MethodTabs, ProviderRow, safeNextClient, Tip } from "../auth-parts";
import { CopyCommand } from "../copy-command";
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
  claim_first: "This BOSS has no Owner yet. Claim it first.",
  shared_password_retired: "The shared password no longer works. Ask an Owner to invite you.",
};

/** "6 Nov" for the shared password's last day (local time). */
function shortDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  return localWith(iso, { day: "numeric", month: "short" }) || null;
}

function say(e: unknown): string {
  if (e instanceof IdentityError) {
    if (e.code === "invalid_code" && e.attemptsLeft !== null) return `That code didn't work · ${e.attemptsLeft} left`;
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
  const [where, setWhere] = useState<{ hostname: string; protocol: string } | null>(null);
  const [webauthn, setWebauthn] = useState(false);
  const target = safeNextClient(next);

  // The API offers passkeys only once a public origin is bound (S1 `passkey`, `passkey_reason`),
  // and never on an address a browser can't hold a passkey for (an IP, plain http: passkey-support).
  useEffect(() => {
    setWhere({ hostname: window.location.hostname, protocol: window.location.protocol });
    setWebauthn(webauthnAvailable());
  }, []);
  const passkeyWhy = passkeyWhyNot(options, where, webauthn);
  const hasPasskeys = where !== null && passkeyWhy === null;
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
        <div className="idn-wait" role="status">{error ? <span className="idn-err">{error}</span> : <><span className="idn-blink" aria-hidden /> Waiting for your device…</>}</div>
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
  const passkeyButton = (tip?: string) => (
    <button type="button" className="idn-big pk" aria-disabled={passkeyWhy ? true : undefined} aria-describedby={tip}
      disabled={busy} data-testid="passkey-sign-in"
      onClick={() => (passkeyWhy ? setWhy({ at: "passkey", text: passkeyWhy }) : passkey("login"))}>
      <Fingerprint size={20} weight="bold" aria-hidden /> Sign in with a passkey
    </button>
  );
  return (
    <AuthStage label="Sign in">
      <Logo />
      <h1>Welcome back</h1>
      <p className="idn-s" data-copy="prose">Sign in to {options.org_name} to pick up where your team left off.</p>
      {options.claim ? (
        <a className="idn-note info" href="/claim" data-testid="claim-door">
          <ShieldCheck size={16} aria-hidden /> <span><b>This BOSS has no Owner yet.</b> Claim it <ArrowRight size={12} aria-hidden /></span>
        </a>
      ) : null}
      <ProviderRow offers={providerOffers(options.providers, target)} onWhy={(text) => setWhy({ at: "idp", text })} />
      {whyLine("idp")}
      {passkeyWhy ? <Tip text={passkeyWhy} className="block">{(tip) => passkeyButton(tip)}</Tip> : passkeyButton()}
      {whyLine("passkey")}
      <div className="idn-or">or use email</div>
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
                <input id="email" className="idn-i" type="email" autoComplete="username webauthn" required value={email} placeholder="you@example.com"
                  onChange={(e) => setEmail(e.target.value)} aria-invalid={!!error || undefined} aria-describedby={error ? "signin-error" : undefined} />
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
                <input id="password" className="idn-i" type={showPassword ? "text" : "password"} autoComplete="current-password" required value={password}
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
          {error ? <div className="idn-err" id="signin-error" role="alert">{error}</div> : null}
          <button type="submit" className="idn-big p go" disabled={busy} data-testid="email-submit">
            {busy ? "Signing in…" : method === "password" ? "Sign in" : method === "magic" ? "Email me a link" : "Email me a code"}
            {busy ? null : <ArrowRight size={18} weight="bold" aria-hidden />}
          </button>
        </form>
      )}

      <p className="idn-new">
        New here?{" "}
        {options.signup
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
          <CopyCommand command="boss identity recover-owner --email you@example.com" />
          <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn primary" onClick={() => setSheet(null)}>Got it</button></div>
        </Sheet>
      ) : null}
      <Live text={busy ? "Working" : stage.kind === "done" ? "Signed in" : ""} />
    </AuthStage>
  );
}
