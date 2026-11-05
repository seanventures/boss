"use client";

/**
 * ENROL A SECOND FACTOR (IDENTITY.md §4.4, §4.5, §9.7): after a claim, an invitation, or when an
 * MFA-required role signs in without one. A passkey first; an authenticator app when the person
 * prefers or the browser has none. Then recovery codes, shown once.
 *
 * NEVER PASSKEY-ONLY (BOSSAPP-812): someone who reached here with no password (an emailed
 * sign-up) chooses one right after the factor — the API lets a first password be set only on a
 * fresh second factor, which enrolling just gave this session. A passkey lives in one browser;
 * the password is the way in from any other.
 */
import React, { useEffect, useState } from "react";
import { CaretRight, DeviceMobile, Eye, EyeSlash, Fingerprint, LockSimple } from "@phosphor-icons/react";

import { idPost } from "../../../lib/identity/api";
import { codesOf, type RecoveryCodes } from "../../../lib/identity/types-auth";
import { webauthnAvailable } from "../../../lib/identity/webauthn";
import { passkeyBlockWords } from "../../../lib/identity/passkey-support";
import { Face } from "../../../components/identity/kit";
import { AuthStage, Live, Logo, safeNextClient } from "../auth-parts";
import { primeCsrf, registerPasskey, sayAuth } from "../auth-flow";
import { RecoveryCodesView } from "../recovery-codes";
import { TotpStep } from "./totp-step";

type Stage = "choose" | "totp" | "codes" | "password";

export function Enrol({ host, next, who, passkeyReason = null, passwordMin = 12 }: {
  host: string; next: string; who: { name: string; email: string; hue: number } | null;
  /** The security policy's password minimum (GET /v1/auth/options). */
  passwordMin?: number;
  /** /v1/auth/options passkey_reason: passkey_needs_domain on an IP address (passkey-support). */
  passkeyReason?: string | null;
}) {
  const [noPasskey, setNoPasskey] = useState<string | null>(null);
  const [stage, setStage] = useState<Stage>("choose");
  const [passkeys, setPasskeys] = useState(true);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const target = safeNextClient(next);
  /** /me said this person has no password yet: one is chosen after the factor. */
  const [needsPassword, setNeedsPassword] = useState(false);
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  useEffect(() => {
    const blocked = passkeyBlockWords(passkeyReason, window.location);
    setNoPasskey(blocked);
    setPasskeys(webauthnAvailable() && !blocked);
    // No passkey possible here: the authenticator app IS the choice, not a link under a dead button.
    if (blocked) setStage("totp");
    primeCsrf().then((me) => setNeedsPassword(me?.sign_in?.password === false)).catch(() => undefined);
  }, [passkeyReason]);

  /** The codes issued with the first factor; only when there were none, ask for a set. */
  const toCodes = async (issued: string[] = []) => {
    setCodes(issued.length ? issued : codesOf(await idPost<RecoveryCodes>("/v1/identity/me/recovery-codes", {})));
    setStage("codes");
  };

  const withPasskey = async () => {
    setBusy(true);
    setError(null);
    try { await toCodes(await registerPasskey()); } catch (e) { setError(sayAuth(e)); } finally { setBusy(false); }
  };

  if (stage === "codes" && codes) {
    return (
      <AuthStage label="Recovery codes">
        <Logo host={host} />
        <h1>Save your recovery codes</h1>
        <p className="idn-s" data-copy="prose">If you lose your second factor, one of these gets you back in. Each works once.</p>
        <RecoveryCodesView codes={codes} host={host} onContinue={() => {
          setCodes(null);
          if (needsPassword) { setError(null); setStage("password"); } else window.location.assign(target);
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
          <button type="button" onClick={() => window.location.assign(target)} data-testid="enrol-password-later">Later, in My account <CaretRight size={13} className="idn-chev" aria-hidden /></button>
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
        <TotpStep onDone={(issued) => { toCodes(issued).catch((e) => setError(sayAuth(e))); }} />
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
      {passkeys ? (
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
