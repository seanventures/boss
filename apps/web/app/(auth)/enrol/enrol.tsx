"use client";

/**
 * ENROL A SECOND FACTOR (IDENTITY.md §4.4, §4.5, §9.7): after a claim, an invitation, or when an
 * MFA-required role signs in without one. A passkey first; an authenticator app when the person
 * prefers or the browser has none. Then recovery codes, shown once.
 *
 * NEVER PASSKEY-ONLY (BOSSAPP-812): someone who reached here with no password (an emailed
 * sign-up) chooses one right after the factor, the API lets a first password be set only on a
 * fresh second factor, which enrolling just gave this session. A passkey lives in one browser;
 * the password is the way in from any other.
 */
import React, { useEffect, useState } from "react";
import { CaretRight, DeviceMobile, Eye, EyeSlash, Fingerprint, LockSimple, SignIn } from "@phosphor-icons/react";

import { IdentityError, idPost } from "../../../lib/identity/api";
import { ENROLMENT_NEEDS_SIGN_IN, ENROLMENT_NEEDS_SIGN_IN_WORDS, freshSignInHref } from "../../../lib/identity/enrolment";
import { codesOf, type RecoveryCodes } from "../../../lib/identity/types-auth";
import { webauthnAvailable } from "../../../lib/identity/webauthn";
import { passkeyBlockWords, passkeyPlace } from "../../../lib/identity/passkey-support";
import { Face } from "../../../components/identity/kit";
import { AuthStage, Live, Logo, safeNextClient } from "../auth-parts";
import { primeCsrf, registerPasskey, sayAuth } from "../auth-flow";
import { RecoveryCodesView } from "../recovery-codes";
import { enrolContinueLabel } from "../../../lib/identity/claim-resume";
import { TotpStep } from "./totp-step";

type Stage = "choose" | "totp" | "codes" | "password" | "done";

const ENROL_STORAGE_KEY = "boss.enrol.state";

function readStoredEnrol(): { stage: "codes" | "password"; codes?: string[] } | null {
  try {
    const raw = window.sessionStorage.getItem(ENROL_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { stage?: unknown; codes?: unknown };
    if (parsed.stage === "codes" && Array.isArray(parsed.codes) && parsed.codes.every((c) => typeof c === "string")) {
      return { stage: "codes", codes: parsed.codes };
    }
    if (parsed.stage === "password") return { stage: "password" };
  } catch { /* non-browser or malformed */ }
  return null;
}

function writeStoredEnrol(state: { stage: "codes" | "password"; codes?: string[] } | null): void {
  try {
    if (!state) window.sessionStorage.removeItem(ENROL_STORAGE_KEY);
    else window.sessionStorage.setItem(ENROL_STORAGE_KEY, JSON.stringify(state));
  } catch { /* non-browser */ }
}

/** Was enrolment refused because only a fresh sign-in holds the grant (a promotion or a reset after
 *  this session began)? Then the way on is to sign in again, never an admin's reset. */
function needsFreshSignIn(e: unknown): boolean {
  return e instanceof IdentityError && e.code === ENROLMENT_NEEDS_SIGN_IN;
}

/** SIGN IN AGAIN, COMING BACK HERE: this session ends, the sign-in page opens, and a sign-in that
 *  must enrol goes straight on to /enrol (afterEnrolStep) and then to `next`. */
function SignInAgain({ next }: { next: string }) {
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    try { await idPost("/v1/auth/logout"); } catch { /* signed out either way */ }
    window.location.assign(freshSignInHref(next));
  };
  return (
    <>
      <div className="idn-note" role="status" data-copy="prose" data-testid="enrol-sign-in-again-why">{ENROLMENT_NEEDS_SIGN_IN_WORDS}</div>
      <button type="button" className="idn-big p" onClick={() => void go()} disabled={busy} data-testid="enrol-sign-in-again">
        <SignIn size={18} weight="bold" aria-hidden /> {busy ? "Signing out…" : "Sign in again"}
      </button>
    </>
  );
}

export function Enrol({
  host,
  next,
  who,
  passkeyReason = null,
  publicOrigin = null,
  passwordMin = 12,
  alreadyEnrolled = false,
  initialNeedsPassword = false,
}: {
  host: string; next: string; who: { name: string; email: string; hue: number } | null;
  /** The security policy's password minimum (GET /v1/auth/options). */
  passwordMin?: number;
  /** /v1/auth/options passkey_reason: passkey_needs_domain on an IP address (passkey-support). */
  passkeyReason?: string | null;
  /** The bound public origin: a passkey works only there (passkeyOtherLoopbackWords). */
  publicOrigin?: string | null;
  /** True when the signed-in user already has a TOTP factor or passkey on reload. */
  alreadyEnrolled?: boolean;
  /** True when /me reported no password yet. */
  initialNeedsPassword?: boolean;
}) {
  const [noPasskey, setNoPasskey] = useState<string | null>(null);
  const [stage, setStage] = useState<Stage>(() =>
    alreadyEnrolled ? (initialNeedsPassword ? "password" : "done") : "choose",
  );
  const [passkeys, setPasskeys] = useState(true);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Enrolment needs a fresh sign-in (403 enrolment_needs_sign_in): offer it, not an admin. */
  const [signInAgain, setSignInAgain] = useState(false);
  const target = safeNextClient(next);
  /** A refusal the screen answers itself: true when it was the fresh-sign-in one. */
  const refused = (e: unknown): boolean => {
    if (!needsFreshSignIn(e)) return false;
    setSignInAgain(true);
    return true;
  };
  /** /me said this person has no password yet: one is chosen after the factor. */
  const [needsPassword, setNeedsPassword] = useState(initialNeedsPassword);
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  useEffect(() => {
    const blocked = passkeyBlockWords(passkeyReason, passkeyPlace(), publicOrigin);
    setNoPasskey(blocked);
    setPasskeys(webauthnAvailable() && !blocked);
    const saved = alreadyEnrolled ? readStoredEnrol() : null;
    if (saved?.stage === "codes" && saved.codes?.length) {
      setCodes(saved.codes);
      setStage("codes");
    } else if (saved?.stage === "password") {
      setStage("password");
    } else if (alreadyEnrolled) {
      setStage(initialNeedsPassword ? "password" : "done");
    } else if (blocked) {
      // No passkey possible here: the authenticator app IS the choice, not a link under a dead button.
      setStage("totp");
    }
    primeCsrf().then((me) => {
      const noPw = me?.sign_in?.password === false;
      setNeedsPassword(noPw);
      if (alreadyEnrolled && !saved) {
        setStage(noPw ? "password" : "done");
      }
    }).catch(() => undefined);
  }, [passkeyReason, publicOrigin, alreadyEnrolled, initialNeedsPassword]);

  /** The codes issued with the first factor; only when there were none, ask for a set. */
  const toCodes = async (issued: string[] = []) => {
    const nextCodes = issued.length ? issued : codesOf(await idPost<RecoveryCodes>("/v1/identity/me/recovery-codes", {}));
    setCodes(nextCodes);
    setStage("codes");
    writeStoredEnrol({ stage: "codes", codes: nextCodes });
  };

  const withPasskey = async () => {
    setBusy(true);
    setError(null);
    try { await toCodes(await registerPasskey()); } catch (e) { if (!refused(e)) setError(sayAuth(e)); } finally { setBusy(false); }
  };

  if (stage === "done") {
    return (
      <AuthStage label="Account protected">
        <Logo host={host} />
        {who ? (
          <div className="idn-who-s">
            <Face name={who.name} tone={who.hue} size={34} />
            <span><b>{who.name}</b><small>{who.email}</small></span>
          </div>
        ) : null}
        <h1>Your account is protected</h1>
        <p className="idn-s" data-copy="prose">Your second factor is already set up.</p>
        <button type="button" className="idn-big p" onClick={() => window.location.assign(target)} data-testid="enrol-already-continue">
          {enrolContinueLabel(next)}
        </button>
      </AuthStage>
    );
  }

  if (stage === "codes" && codes) {
    return (
      <AuthStage label="Recovery codes">
        <Logo host={host} />
        <h1>Save your recovery codes</h1>
        <p className="idn-s" data-copy="prose">If you lose your second factor, one of these gets you back in. Each works once.</p>
        <RecoveryCodesView codes={codes} host={host} continueLabel={enrolContinueLabel(next)} onContinue={() => {
          setCodes(null);
          if (needsPassword) {
            setError(null);
            setStage("password");
            writeStoredEnrol({ stage: "password" });
          } else {
            writeStoredEnrol(null);
            window.location.assign(target);
          }
        }} />
      </AuthStage>
    );
  }

  if (stage === "password") {
    const setIt = async () => {
      setBusy(true);
      setError(null);
      try {
        await primeCsrf();
        await idPost("/v1/identity/me/password", { new: password });
        setPassword("");
        writeStoredEnrol(null);
        window.location.assign(target);
      } catch (e) { setError(sayAuth(e)); setBusy(false); }
    };
    return (
      <AuthStage label="Choose a password">
        <Logo host={host} />
        <h1>Choose a password</h1>
        <p className="idn-s" data-copy="prose">Your email and this password sign you in from any browser. Your second factor still protects it.</p>
        <form onSubmit={(e) => { e.preventDefault(); if (password.length >= passwordMin) setIt(); }} noValidate>
          <div className="idn-f">
            <label htmlFor="enrol-pw">Password</label>
            <div className="idn-ig">
              <LockSimple size={18} className="idn-ig-ico" aria-hidden />
              <input id="enrol-pw" className="idn-i" type={showPassword ? "text" : "password"} autoComplete="new-password" minLength={passwordMin} required
                value={password} onChange={(e) => setPassword(e.target.value)} aria-describedby="enrol-pw-hint" />
              <button type="button" className="idn-eye" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword}
                onClick={() => setShowPassword(!showPassword)}>
                {showPassword ? <EyeSlash size={18} aria-hidden /> : <Eye size={18} aria-hidden />}
              </button>
            </div>
            <span id="enrol-pw-hint" className="idn-dim idn-hint">{passwordMin} characters or more</span>
          </div>
          {error ? <div className="idn-err" role="alert">{error}</div> : null}
          <button type="submit" className="idn-big p" disabled={busy || password.length < passwordMin} data-testid="enrol-password">{busy ? "Saving…" : "Set password and continue"}</button>
        </form>
        <div className="idn-alt">
          <button type="button" onClick={() => { writeStoredEnrol(null); window.location.assign(target); }} data-testid="enrol-password-later">Later, in My account <CaretRight size={13} className="idn-chev" aria-hidden /></button>
        </div>
        <Live text={busy ? "Saving" : ""} />
      </AuthStage>
    );
  }

  if (stage === "totp") {
    return (
      <AuthStage label="Authenticator app">
        <Logo host={host} />
        <h1>Add an authenticator</h1>
        <p className="idn-s" data-copy="prose">Scan the code with your authenticator app, then enter the 6 digits it shows. The key is shown only now.</p>
        {noPasskey ? <div className="idn-note" data-copy="prose" data-testid="no-passkey-here">{noPasskey}</div> : null}
        {signInAgain ? <SignInAgain next={target} /> : (
          <TotpStep onDone={(issued) => { toCodes(issued).catch((e) => setError(sayAuth(e))); }} onRefused={refused} />
        )}
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        <div className="idn-alt">
          {passkeys ? <button type="button" onClick={() => { setStage("choose"); setError(null); }}><Fingerprint size={16} aria-hidden /> Use a passkey instead <CaretRight size={13} className="idn-chev" aria-hidden /></button> : null}
        </div>
      </AuthStage>
    );
  }

  return (
    <AuthStage label="Second factor">
      <Logo host={host} />
      {who ? (
        <div className="idn-who-s">
          <Face name={who.name} tone={who.hue} size={34} />
          <span><b>{who.name}</b><small>{who.email}</small></span>
        </div>
      ) : null}
      <h1>Protect your account</h1>
      <p className="idn-s" data-copy="prose">Add a second factor. A passkey on this device is recommended: it is also your one-tap sign-in here.</p>
      {signInAgain ? <SignInAgain next={target} /> : passkeys ? (
        <button type="button" className="idn-big p" onClick={withPasskey} disabled={busy} data-testid="enrol-passkey">
          <Fingerprint size={18} weight="bold" aria-hidden /> {busy ? "Waiting for your device…" : "Create a passkey"}
        </button>
      ) : (
        <div className="idn-note" data-copy="prose" data-testid="no-passkey-here">{noPasskey ?? "This browser has no passkeys. Use an authenticator app."}</div>
      )}
      {error ? <div className="idn-err" role="alert">{error}</div> : null}
      <div className="idn-alt">
        <button type="button" onClick={() => { setStage("totp"); setError(null); }} data-testid="enrol-totp">
          <DeviceMobile size={16} aria-hidden /> Use an authenticator app <CaretRight size={13} className="idn-chev" aria-hidden />
        </button>
      </div>
      <p className="idn-fine" data-copy="prose">Owners, Admins and Auditors must use one. No phone or SMS codes, ever.</p>
      <Live text={busy ? "Waiting for your device" : ""} />
    </AuthStage>
  );
}
