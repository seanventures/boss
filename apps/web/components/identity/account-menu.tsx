"use client";

/**
 * MY ACCOUNT, UNDER THE AVATAR (IDENTITY.md §9): who you are signed in as, your security, the
 * agents acting for you, your keys, and Sign out. Plus the console-wide identity banners the API
 * owns (network bridge, legacy password countdown, owner recovered, MFA enrolment required).
 */
import { primaryRoleLabel } from "../../lib/identity/role-label";
import React from "react";
import { Key, Robot, ShieldCheck, ShieldWarning, SignOut, Devices, UserCircle } from "@phosphor-icons/react";

import { idPost } from "../../lib/identity/api";
import { useIdentity } from "./identity-provider";
import { Face } from "./kit";


function hueOf(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h % 8;
}

export async function signOut(): Promise<void> {
  try { await idPost("/v1/auth/logout"); } catch { /* signed out either way */ }
  window.location.assign("/login");
}

export function AccountMenu() {
  const { me } = useIdentity();
  if (!me) return null;
  const mfa = me.sign_in.passkeys > 0 || me.sign_in.totp;
  const first = me.display_name.split(" ")[0];
  return (
    <details className="idn-me" data-testid="account-menu">
      <summary aria-label={`My account: ${me.display_name}`}>
        <Face name={me.display_name} tone={hueOf(me.principal_id)} size={26} />
        <span><b>{first}</b><span className="idn-me-role"> · {primaryRoleLabel(me.access_roles)}</span></span>
        {mfa
          ? <ShieldCheck size={15} className="idn-ok" aria-label="MFA on" />
          : <ShieldWarning size={15} className="idn-warn" aria-label="No second factor" />}
      </summary>
      <div className="idn-me-menu" role="menu">
        <div className="idn-me-who">
          <b>{me.display_name}</b>
          <small>{me.email ?? me.principal_id}</small>
        </div>
        <a role="menuitem" href="/account"><UserCircle size={16} aria-hidden /> Security</a>
        <a role="menuitem" href="/account/sessions"><Devices size={16} aria-hidden /> Sessions</a>
        <a role="menuitem" href="/account/agents"><Robot size={16} aria-hidden /> Agents for me</a>
        <a role="menuitem" href="/account/keys"><Key size={16} aria-hidden /> API keys</a>
        <button role="menuitem" type="button" onClick={signOut} data-testid="sign-out"><SignOut size={16} aria-hidden /> Sign out</button>
      </div>
    </details>
  );
}

export function IdentityBanners() {
  const { me, stub, mirrored } = useIdentity();
  const banners = me?.banners ?? [];
  if (!banners.length && !stub && mirrored) return null;
  return (
    <div className="idn-banners" data-testid="identity-banners">
      {stub ? <div className="idn-stub" data-testid="stub-banner">Stub data · identity API not connected</div> : null}
      {!mirrored ? <div className="idn-banner" data-kind="unavailable" data-copy="prose" title="Controls are not mirrored; BOSS checks every action">Access check offline</div> : null}
      {banners.map((b) => (
        <div key={b.kind} className="idn-banner" data-kind={b.kind} role="status">
          <span>{b.message}</span>
          {b.kind === "network_bridge" ? <a href="/claim">Claim this BOSS</a> : null}
          {b.kind === "mfa_enrolment_required" ? <a href="/enrol">Add a passkey</a> : null}
          {b.kind === "legacy_password" ? <a href="/identity/people">Invite people</a> : null}
        </div>
      ))}
    </div>
  );
}
