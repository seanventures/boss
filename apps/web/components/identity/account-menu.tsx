"use client";

/**
 * MY ACCOUNT, UNDER THE AVATAR (IDENTITY.md §9): who you are signed in as, your security, the
 * agents acting for you, your keys, and Sign out. Plus the console-wide identity banners the API
 * owns (network bridge, legacy password countdown, owner recovered, MFA enrolment required).
 */
import { primaryRoleLabel } from "../../lib/identity/role-label";
import React from "react";
import { Key, Robot, ShieldCheck, ShieldWarning, SignOut, Devices, UserCircle, X } from "@phosphor-icons/react";

import { enc, IdentityError, idPost } from "../../lib/identity/api";
import type { MeBanner } from "../../lib/identity/contract";
import { useIdentity } from "./identity-provider";
import { Face } from "./kit";
import { avatarTone } from "../../lib/identity/avatar";



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
        <Face name={me.display_name} tone={avatarTone(me.principal_id, me.avatar_seed)} size={26} />
        <span><b>{first}</b><span className="idn-me-role">, {primaryRoleLabel(me.access_roles)}</span></span>
        {mfa
          ? <ShieldCheck size={15} className="idn-ok" aria-label="MFA on" />
          : <ShieldWarning size={15} className="idn-warn" aria-label="No second factor" />}
      </summary>
      <div className="idn-me-menu" role="menu">
        <div className="idn-me-who">
          <b>{me.display_name}</b>
          <small>{me.email ?? (me.kind === "service_account" ? "Service account" : "")}</small>
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

/** Where a banner says to go next. */
function BannerLink({ b }: { b: MeBanner }) {
  if (b.kind === "network_bridge" && !b.until) return <a href="/claim">Claim this BOSS</a>;
  if (b.kind === "mfa_enrolment_required") return <a href="/enrol">Add a passkey</a>;
  if (b.kind === "recovery_codes_low") return <a href="/account">Make new codes</a>;
  if (b.kind === "api_key_expiring") return <a href="/account/keys">API keys</a>;
  if (b.kind === "new_device") return <a href="/account/sessions">Sessions</a>;
  if (b.kind === "no_password") return <a href="/account#acc-pw-h" data-testid="banner-set-password">Set a password</a>;
  if (b.kind === "owner_recovered") return <a href="/identity/audit">Open the audit log</a>;
  return null;
}

/** × for a dismissible banner, "Got it" for a security notice, nothing for a persistent one. */
function BannerClose({ b, onClose }: { b: MeBanner; onClose: (b: MeBanner) => void }) {
  if (b.policy === "persistent") return null;
  if (b.policy === "acknowledge") {
    return <button type="button" className="idn-banner-ack" title="Got it" data-testid="banner-close" onClick={() => onClose(b)}>Got it</button>;
  }
  return (
    <button type="button" className="idn-banner-x" title="Hide this" aria-label="Hide this" data-testid="banner-close" onClick={() => onClose(b)}>
      <X size={14} aria-hidden />
    </button>
  );
}

function Banner({ b, onClose }: { b: MeBanner; onClose: (b: MeBanner) => void }) {
  return (
    <div className="idn-banner" data-kind={b.kind} data-policy={b.policy} data-banner-id={b.id} role="status">
      <span className="idn-banner-text" data-copy="data">{b.message}</span>
      <BannerLink b={b} />
      <BannerClose b={b} onClose={onClose} />
    </div>
  );
}

/**
 * The console-wide identity banners (GET /v1/identity/me `banners`). Each closes by its policy
 * (engine/boss_engine/identity/banner_policy.py): hidden at once here, then closed server-side for
 * this person on every device. Two or more stack into one card ("2 notices") that opens on click,
 * so they never take half the screen.
 */
export function IdentityBanners() {
  const { me, stub, mirrored } = useIdentity();
  const [closed, setClosed] = React.useState<ReadonlySet<string>>(() => new Set());
  const [open, setOpen] = React.useState(false);
  const banners = (me?.banners ?? []).filter((b) => !closed.has(b.id));
  const close = React.useCallback(async (b: MeBanner) => {
    setClosed((was) => new Set(was).add(b.id));
    try {
      await idPost(`/v1/identity/me/banners/${enc(b.id)}/dismiss`);
    } catch (error) {
      // Gone already (404) stays gone; anything else, show it again.
      if (!(error instanceof IdentityError && error.status === 404)) {
        setClosed((was) => { const next = new Set(was); next.delete(b.id); return next; });
      }
    }
  }, []);
  if (!banners.length && !stub && mirrored) return null;
  const stacked = banners.length >= 2;
  return (
    <div className="idn-banners" data-testid="identity-banners">
      {stub ? <div className="idn-stub" data-testid="stub-banner">Stub data, identity API not connected</div> : null}
      {!mirrored ? <div className="idn-banner" data-kind="unavailable" data-copy="prose" title="Controls are not mirrored; BOSS checks every action">Access check offline</div> : null}
      {!stacked ? banners.map((b) => <Banner key={b.id} b={b} onClose={close} />) : (
        <div className="idn-banner-stack" data-testid="banner-stack" data-open={open ? "true" : "false"}>
          {(open ? banners : banners.slice(0, 1)).map((b) => <Banner key={b.id} b={b} onClose={close} />)}
          <button type="button" className="idn-banner-more" data-testid="banner-stack-toggle" aria-expanded={open}
            title={open ? "Show only the first" : "Show every notice"} onClick={() => setOpen((v) => !v)}>
            {open ? "Show less" : `${banners.length} notices`}
          </button>
        </div>
      )}
    </div>
  );
}
