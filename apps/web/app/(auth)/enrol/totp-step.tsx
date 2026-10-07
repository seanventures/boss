"use client";

/**
 * AUTHENTICATOR ENROLMENT (IDENTITY.md §4.4): the QR code and the text secret, shown ONCE, then a
 * code from the app to confirm. The QR is drawn by the API (`qr_svg`, a seam for S1) so the
 * console ships no QR encoder; without it the secret is shown large enough to type.
 */
import React, { useEffect, useRef, useState } from "react";

import { IdentityError, idPost } from "../../../lib/identity/api";
import { ENROLMENT_EXPIRED, ENROLMENT_EXPIRED_WORDS } from "../../../lib/identity/enrolment";
import { codesOf, type RecoveryCodes, type TotpEnrolment } from "../../../lib/identity/types-auth";
import { CodeInput } from "../../../components/identity/kit";
import { sayAuth } from "../auth-flow";
import { svgDataUri } from "../../../lib/svg-data-uri";


/** `onDone(codes)`: the recovery codes the API issued with this FIRST factor, or [] (S1). */
export function TotpStep({ onDone, guard }: {
  onDone: (codes: string[]) => void;
  /** My account passes useGuardedCall: starting an authenticator needs a fresh second factor. */
  guard?: <T>(reason: string, call: () => Promise<T>) => Promise<T>;
}) {
  const run = <T,>(call: () => Promise<T>) => (guard ? guard("Adding an authenticator changes how you sign in.", call) : call());
  const [enrol, setEnrol] = useState<TotpEnrolment | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [qrBroken, setQrBroken] = useState(false);

  const liveRef = useRef(true);
  /** Start (or restart) an enrolment: a new key and QR, bound to this browser's session. */
  const start = () => run(() => idPost<TotpEnrolment>("/v1/identity/me/mfa/totp", {}))
    .then((r) => { if (liveRef.current) { setEnrol(r); setQrBroken(false); } });

  useEffect(() => {
    liveRef.current = true;
    start().catch((e) => { if (liveRef.current) setError(sayAuth(e)); });
    return () => { liveRef.current = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const confirm = async (value: string) => {
    setBusy(true);
    setError(null);
    try {
      const confirmed = await run(() => idPost<RecoveryCodes | null>("/v1/identity/me/mfa/totp/confirm", { code: value }));
      setEnrol(null); // the secret leaves memory the moment it is confirmed
      onDone(codesOf(confirmed));
    } catch (e) {
      setCode("");
      if (e instanceof IdentityError && e.code === ENROLMENT_EXPIRED) {
        // No key to check the code against (it expired, or another tab began a new one): not a
        // wrong code. Show a NEW key at once and say so, never "That code didn't work" forever.
        setEnrol(null);
        try { await start(); setError(ENROLMENT_EXPIRED_WORDS); } catch (again) { setError(sayAuth(again)); }
      } else {
        setError(sayAuth(e));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="totp-step">
      {enrol ? (
        <div className="idn-totp">
          {/* No QR from the API (or one that would not load): no image box at all, only the key. */}
          {enrol.qr_svg && !qrBroken
            ? /* eslint-disable-next-line @next/next/no-img-element */
              <img className="idn-qr" src={svgDataUri(enrol.qr_svg)} alt="QR code for your authenticator app" width={168} height={168}
                data-testid="totp-qr" onError={() => setQrBroken(true)} />
            : null}
          <div className="idn-f">
            <span className="idn-flabel">Or type this key</span>
            <code className="idn-secret-key" data-copy="data" aria-label="Authenticator key">{enrol.secret}</code>
          </div>
        </div>
      ) : !error ? <div className="idn-wait" role="status"><span className="idn-blink" aria-hidden /> Preparing…</div> : null}
      <CodeInput value={code} onChange={setCode} onComplete={confirm} label="Code from your app" invalid={!!error} autoFocus={false} />
      <button type="button" className="idn-big p" disabled={busy || code.length < 6 || !enrol} onClick={() => confirm(code)}>
        {busy ? "Checking…" : "Confirm code"}
      </button>
      {error ? <div className="idn-err" role="alert">{error}</div> : null}
    </div>
  );
}
