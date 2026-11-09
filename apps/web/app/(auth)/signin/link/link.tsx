"use client";

/**
 * A MAGIC LINK LANDS HERE (IDENTITY.md §4.14): `/signin/link#t=<token>`, the token rides in the
 * fragment, so it never reaches an access log, and is removed from the address bar at once. The
 * API accepts it only in the browser that asked for it, once, within 10 minutes. Roles that
 * require MFA still finish with a second factor.
 */
import { afterEnrolStep } from "../../../../lib/identity/enrolment";
import React, { useEffect, useRef, useState } from "react";
import { EnvelopeSimple } from "@phosphor-icons/react";

import { idPost } from "../../../../lib/identity/api";
import type { SignInStep } from "../../../../lib/identity/types";
import { AuthStage, Live, Logo, fragmentToken } from "../../auth-parts";
import { MfaStep } from "../../mfa-step";

type State = { kind: "checking" } | { kind: "mfa"; step: SignInStep } | { kind: "failed" } | { kind: "done" };

export function MagicLinkLanding({ host, next }: { host: string; next: string }) {
  const [state, setState] = useState<State>({ kind: "checking" });
  const once = useRef(false);

  const finish = (step: SignInStep) => {
    if (step.next === "mfa") { setState({ kind: "mfa", step }); return; }
    setState({ kind: "done" });
    window.location.assign(step.next === "enrol" ? afterEnrolStep(step, next) : next);
  };

  useEffect(() => {
    if (once.current) return;
    once.current = true;
    const token = fragmentToken();
    try { window.history.replaceState(null, "", window.location.pathname); } catch { /* keep going */ }
    if (!token) { setState({ kind: "failed" }); return; }
    idPost<SignInStep>("/v1/auth/magic-link/verify", { token }).then(finish).catch(() => setState({ kind: "failed" }));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (state.kind === "failed") {
    return (
      <AuthStage label="Magic link">
        <Logo host={host} />
        <div className="idn-sent">
          <div className="idn-env" aria-hidden><EnvelopeSimple size={22} /></div>
          <h1>This link didn&apos;t work</h1>
          <p className="idn-s" data-copy="prose" data-testid="link-failed">It has expired, was used already, or was opened in another browser. Ask for a new one.</p>
        </div>
        <a className="idn-big p" href="/login">Back to sign in</a>
      </AuthStage>
    );
  }
  if (state.kind === "mfa") {
    return (
      <AuthStage label="Second factor">
        <Logo host={host} />
        <h1>One more step</h1>
        <p className="idn-s" data-copy="prose">Your access role needs a second factor, even with an email link.</p>
        <MfaStep step={state.step} onDone={finish} />
      </AuthStage>
    );
  }
  return (
    <AuthStage label="Magic link">
      <Logo host={host} />
      <h1 className="idn-center">Signing you in</h1>
      <div className="idn-wait" role="status"><span className="idn-blink" aria-hidden /> Checking your link…</div>
      <Live text={state.kind === "done" ? "Signed in" : ""} />
    </AuthStage>
  );
}
