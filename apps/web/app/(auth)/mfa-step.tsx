"use client";

/**
 * The second-factor step for flows that land outside the sign-in page (a magic link, a reset):
 * the authenticator code, or a passkey. Same words and rules as sign-in's own step.
 */
import React, { useState } from "react";
import { CaretRight, Fingerprint } from "@phosphor-icons/react";

import { idPost } from "../../lib/identity/api";
import type { SignInStep } from "../../lib/identity/types";
import { getAssertion, webauthnAvailable, type AssertionOptions, type Wrapped } from "../../lib/identity/webauthn";
import { CodeInput } from "../../components/identity/kit";
import { sayAuth } from "./auth-flow";

export function MfaStep({ step, onDone }: { step: SignInStep; onDone: (next: SignInStep) => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (call: () => Promise<SignInStep>) => {
    setBusy(true);
    setError(null);
    try { onDone(await call()); } catch (e) { setError(sayAuth(e)); setCode(""); } finally { setBusy(false); }
  };
  const totp = (v: string) => run(() => idPost<SignInStep>("/v1/auth/mfa/totp", { code: v }));
  const passkey = () => run(async () => {
    const opts = await idPost<Wrapped<AssertionOptions>>("/v1/auth/webauthn/options", { purpose: "mfa" });
    return idPost<SignInStep>("/v1/auth/webauthn/verify", { purpose: "mfa", credential: await getAssertion(opts) });
  });
  return (
    <div data-testid="mfa-step">
      {step.methods.includes("totp") ? (
        <>
          <CodeInput value={code} onChange={setCode} onComplete={totp} label="Authenticator code" invalid={!!error} />
          <button type="button" className="idn-big p" disabled={busy || code.length < 6} onClick={() => totp(code)}>{busy ? "Checking…" : "Verify"}</button>
        </>
      ) : null}
      {error ? <div className="idn-err" role="alert">{error}</div> : null}
      {step.methods.includes("passkey") && webauthnAvailable() ? (
        <div className="idn-alt">
          <button type="button" onClick={passkey} disabled={busy}><Fingerprint size={16} aria-hidden /> Use a passkey {step.methods.includes("totp") ? "instead" : ""} <CaretRight size={13} className="idn-chev" aria-hidden /></button>
        </div>
      ) : null}
    </div>
  );
}
