"use client";

/**
 * ACCEPT AN INVITATION (IDENTITY.md §4.8 3, §9.7): who invited you, to what, with which access;
 * then SSO (binding that verified identity to this invitation only) or a name and password.
 * Second factor next. A used or expired invitation says so, and nothing else.
 */
import React, { useState } from "react";
import { Key, UserPlus } from "@phosphor-icons/react";

import { idPost } from "../../../../lib/identity/api";
import type { AuthOptions } from "../../../../lib/identity/types";
import type { InvitationPreview } from "../../../../lib/identity/types-auth";
import { AuthStage, Live, Logo } from "../../auth-parts";
import { sayAuth } from "../../auth-flow";

export function InviteAccept({ token, invitation, options }: { token: string; invitation: InvitationPreview | null; options: AuthOptions }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const days = invitation.expires_at ? Math.max(0, Math.ceil((Date.parse(invitation.expires_at) - Date.now()) / 86_400_000)) : null;
  const ok = name.trim().length > 0 && password.length >= 12;

  const accept = async () => {
    setBusy(true);
    setError(null);
    try {
      await idPost(`/v1/auth/invitations/${encodeURIComponent(token)}/accept`, { name: name.trim(), password });
      setPassword("");
      window.location.assign("/enrol?next=%2F");
    } catch (e) {
      setError(sayAuth(e));
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
              <a key={p.id} className="idn-pv full" href={`/v1/auth/sso/${encodeURIComponent(p.id)}/start?invitation=${encodeURIComponent(token)}`}>
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
          <input id="inv-pw" className="idn-i" type="password" autoComplete="new-password" minLength={12} required value={password}
            onChange={(e) => setPassword(e.target.value)} aria-describedby="inv-pw-hint" />
          <span id="inv-pw-hint" className="idn-dim idn-hint">12 characters or more</span>
        </div>
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        <button type="submit" className="idn-big p" disabled={!ok || busy} data-testid="invite-accept">{busy ? "Joining…" : "Join and continue"}</button>
      </form>
      <p className="idn-fine" data-copy="prose">Next you&apos;ll add a passkey or an authenticator app.</p>
      <Live text={busy ? "Joining" : ""} />
    </AuthStage>
  );
}
