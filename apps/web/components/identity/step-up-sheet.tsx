"use client";

/**
 * STEP-UP (IDENTITY.md §4.6): a sensitive action needs a second factor from the last 10 minutes.
 * Passkey first, then the authenticator code; a person without MFA (where policy allows it)
 * confirms with their password. On success the API rotates the session and the action retries.
 */
import React, { useState } from "react";
import { Key, Fingerprint } from "@phosphor-icons/react";

import type { MeResponse } from "../../lib/identity/contract";
import { IdentityError, idPost, refusalSentence } from "../../lib/identity/api";
import { passkeyFailureWords } from "../../lib/identity/passkey-support";
import { getAssertion, webauthnAvailable, type AssertionOptions, type Wrapped } from "../../lib/identity/webauthn";
import { CodeInput, ErrorLine, Sheet } from "./kit";

export function StepUpSheet({ reason, me, onDone }: { reason: string; me: MeResponse | null; onDone: (ok: boolean) => void }) {
  const passkey = (me?.sign_in.passkeys ?? 1) > 0 && webauthnAvailable();
  const totp = me ? me.sign_in.totp : true;
  const passwordOnly = me ? !me.sign_in.totp && me.sign_in.passkeys === 0 : false;
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const finish = async (call: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await call();
      onDone(true);
    } catch (e) {
      setError(passkeyFailureWords(e) ?? (e instanceof IdentityError ? (e.attemptsLeft !== null ? `Wrong code · ${e.attemptsLeft} left` : e.message)
        : refusalSentence(e)));
      setBusy(false);
    }
  };

  const withPasskey = () => finish(async () => {
    const options = await idPost<Wrapped<AssertionOptions>>("/v1/auth/webauthn/options", { purpose: "step_up" });
    const credential = await getAssertion(options);
    await idPost("/v1/auth/webauthn/verify", { purpose: "step_up", credential });
  });
  const withCode = (value: string) => finish(() => idPost("/v1/auth/step-up", { method: "totp", code: value }));
  const withPassword = () => finish(() => idPost("/v1/auth/step-up", { method: "password", password }));

  return (
    <Sheet title="Confirm it's you" icon={<Key size={18} className="idn-warn" aria-hidden />} onClose={() => onDone(false)} testId="step-up-sheet" layer="step-up">
      <p className="idn-lede2" data-copy="prose">{reason} Sensitive actions need a fresh check, at most every 10 minutes.</p>
      {passwordOnly ? (
        <form onSubmit={(e) => { e.preventDefault(); withPassword(); }}>
          <div className="idn-f">
            <label htmlFor="stepup-pw">Your password</label>
            <input id="stepup-pw" className="idn-i" type="password" autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} data-autofocus />
          </div>
          <button type="submit" className="idn-big p" disabled={busy || !password}>Confirm</button>
        </form>
      ) : (
        <>
          {passkey ? (
            <button type="button" className="idn-big p" onClick={withPasskey} disabled={busy} data-autofocus data-testid="step-up-passkey">
              <Fingerprint size={18} aria-hidden /> Confirm with passkey
            </button>
          ) : null}
          {passkey && totp ? <div className="idn-or">or</div> : null}
          {totp ? (
            <CodeInput value={code} onChange={setCode} onComplete={withCode} label="Authenticator code" size="sm" invalid={!!error} autoFocus={!passkey} />
          ) : null}
        </>
      )}
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn sm ghost" onClick={() => onDone(false)}>Cancel</button>
      </div>
    </Sheet>
  );
}
