"use client";

/**
 * ENROL A SECOND FACTOR (IDENTITY.md §4.4, §4.5, §9.7): after a claim, an invitation, or when an
 * MFA-required role signs in without one. A passkey first; an authenticator app when the person
 * prefers or the browser has none. Then recovery codes, shown once.
 */
import React, { useEffect, useState } from "react";
import { CaretRight, DeviceMobile, Fingerprint } from "@phosphor-icons/react";

import { idPost } from "../../../lib/identity/api";
import { codesOf, type RecoveryCodes } from "../../../lib/identity/types-auth";
import { webauthnAvailable } from "../../../lib/identity/webauthn";
import { Face } from "../../../components/identity/kit";
import { AuthStage, Live, Logo, safeNextClient } from "../auth-parts";
import { primeCsrf, registerPasskey, sayAuth } from "../auth-flow";
import { RecoveryCodesView } from "../recovery-codes";
import { TotpStep } from "./totp-step";

type Stage = "choose" | "totp" | "codes";

export function Enrol({ host, next, who }: { host: string; next: string; who: { name: string; email: string; hue: number } | null }) {
  const [stage, setStage] = useState<Stage>("choose");
  const [passkeys, setPasskeys] = useState(true);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const target = safeNextClient(next);

  useEffect(() => {
    setPasskeys(webauthnAvailable());
    primeCsrf().catch(() => undefined);
  }, []);

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
        <RecoveryCodesView codes={codes} host={host} onContinue={() => { setCodes(null); window.location.assign(target); }} />
      </AuthStage>
    );
  }

  if (stage === "totp") {
    return (
      <AuthStage label="Authenticator app">
        <Logo host={host} />
        <h1>Add an authenticator</h1>
        <p className="idn-s" data-copy="prose">Scan the code with your authenticator app, then enter the 6 digits it shows. The key is shown only now.</p>
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
      <h1>Add a second factor</h1>
      <p className="idn-s" data-copy="prose">A passkey signs you in with your fingerprint, face or screen lock. Nobody can phish it.</p>
      {passkeys ? (
        <button type="button" className="idn-big p" onClick={withPasskey} disabled={busy} data-testid="enrol-passkey">
          <Fingerprint size={18} weight="bold" aria-hidden /> {busy ? "Waiting for your device…" : "Create a passkey"}
        </button>
      ) : (
        <div className="idn-note" data-copy="prose">This browser has no passkeys. Use an authenticator app.</div>
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
