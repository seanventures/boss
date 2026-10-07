"use client";

/**
 * ACCEPT AN INVITATION (IDENTITY.md §4.8 3, §9.7): who invited you, to what, with which access;
 * then SSO (an IdP-verified email that matches the invitation accepts it) or a name and
 * password. Second factor next. A used or expired invitation says so, and nothing else.
 *
 * THE TOKEN NEVER RIDES A URL TO THE API. The link is `/invite#t=<token>`: the fragment never
 * reaches a server or an access log, it is wiped from the address bar at once, and the token
 * goes to the API only in a POST body (/v1/auth/invitations/preview and /accept).
 */
import { passwordMin, type AuthOptionsWithPolicy } from "../../../lib/identity/types-account";
import React, { useEffect, useRef, useState } from "react";
import { Key, UserPlus } from "@phosphor-icons/react";

import { IdentityError, idPost } from "../../../lib/identity/api";
import type { AuthOptions, SignInStep } from "../../../lib/identity/types";
import type { InvitationPreview } from "../../../lib/identity/types-auth";
import { AuthStage, Live, Logo, fragmentToken } from "../auth-parts";
import { sayAuth } from "../auth-flow";

/** Read the fragment once, preview the invitation, then hand over to InviteAccept. */
export function InviteLanding({ options }: { options: AuthOptions }) {
  const [state, setState] = useState<{ kind: "checking" } | { kind: "ready"; token: string; invitation: InvitationPreview | null }>({ kind: "checking" });
  const once = useRef(false);
  useEffect(() => {
    if (once.current) return;
    once.current = true;
    const token = fragmentToken();
    try { window.history.replaceState(null, "", window.location.pathname); } catch { /* keep going */ }
    if (!token) { setState({ kind: "ready", token: "", invitation: null }); return; }
    idPost<InvitationPreview>("/v1/auth/invitations/preview", { token })
      .then((invitation) => setState({ kind: "ready", token, invitation }))
      .catch(() => setState({ kind: "ready", token, invitation: null }));
  }, []);
  if (state.kind === "checking") {
    return (
      <AuthStage label="Invitation">
        <Logo host={options.host} />
        <h1>Opening your invitation…</h1>
        <Live text="Opening your invitation" />
      </AuthStage>
    );
  }
  return <InviteAccept token={state.token} invitation={state.invitation} options={options} />;
}

export function InviteAccept({ token, invitation, options }: { token: string; invitation: InvitationPreview | null; options: AuthOptions }) {
  const minPw = passwordMin(options as AuthOptionsWithPolicy);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Days left, from a clock read after mount: the server's "now" is not the browser's.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => { setNow(Date.now()); }, []);

  if (!invitation) {
    return (
      <AuthStage label="Invitation">
        <Logo host={options.host} />
        <h1>This invitation no longer works</h1>
        <p className="idn-s" data-copy="prose" data-testid="invite-expired">It was used already or it expired. Ask whoever invited you to send a new one.</p>
        <a className="idn-big s" href="/login">Go to sign in</a>
      </AuthStage>
    );
  }

  const days = invitation.expires_at && now !== null ? Math.max(0, Math.ceil((Date.parse(invitation.expires_at) - now) / 86_400_000)) : null;
  const ok = name.trim().length > 0 && password.length >= minPw;

  const accept = async () => {
    setBusy(true);
    setError(null);
    try {
      const step = await idPost<SignInStep>("/v1/auth/invitations/accept", { token, name: name.trim(), password });
      setPassword("");
      window.location.assign(step?.next === "done" ? "/" : "/enrol?next=%2F");
    } catch (e) {
      setError(e instanceof IdentityError && e.code === "invitation_invalid" ? "This invitation no longer works" : sayAuth(e));
      setBusy(false);
    }
  };

  return (
    <AuthStage label="Accept invitation">
      <Logo host={options.host} right={days !== null ? `expires in ${days} days` : undefined} />
      <h1>Join {invitation.org}</h1>
      <p className="idn-s" data-copy="prose"><b>{invitation.inviter}</b> invited <b>{invitation.email}</b> to BOSS.</p>
      <div className="idn-need" aria-label="Your access">
        {invitation.roles.map((r) => <span key={r} className="idn-chip"><UserPlus size={13} aria-hidden /> {r}</span>)}
      </div>
      {invitation.sso.length ? (
        <>
          <div className="idn-provs">
            {invitation.sso.map((p) => (
              <a key={p.id} className="idn-pv full" href={`/v1/auth/sso/${encodeURIComponent(p.id)}/start`}
                title={`Sign in at ${p.name} as ${invitation.email}; that verified email accepts this invitation`}>
                <Key size={15} aria-hidden /> Continue with {p.name}
              </a>
            ))}
          </div>
          <div className="idn-or">or set a password</div>
        </>
      ) : null}
      <form onSubmit={(e) => { e.preventDefault(); if (ok) accept(); }} noValidate>
        <div className="idn-f">
          <label htmlFor="inv-name">Your name</label>
          <input id="inv-name" className="idn-i" autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="idn-f">
          <label htmlFor="inv-pw">Choose a password</label>
          <input id="inv-pw" className="idn-i" type="password" autoComplete="new-password" minLength={minPw} required value={password}
            onChange={(e) => setPassword(e.target.value)} aria-describedby="inv-pw-hint" />
          <span id="inv-pw-hint" className="idn-dim idn-hint">{minPw} characters or more</span>
        </div>
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        <button type="submit" className="idn-big p" disabled={!ok || busy} data-testid="invite-accept">{busy ? "Joining…" : "Join and continue"}</button>
      </form>
      <p className="idn-fine" data-copy="prose">Next you&apos;ll add a passkey or an authenticator app.</p>
      <Live text={busy ? "Joining" : ""} />
    </AuthStage>
  );
}
