"use client";

/**
 * MY ACCOUNT › SECURITY (IDENTITY.md §9, §4.2–§4.7): your passkeys (add, rename, remove), the
 * authenticator app (on or off), recovery codes (how many left; regenerate, shown once), your
 * password, and the SSO identities linked to you. Every change here needs a fresh second factor:
 * the API answers step_up_required and the step-up sheet opens (useGuardedCall).
 */
import { avatarTone } from "../../lib/identity/avatar";
import React, { useEffect, useState } from "react";
import { DeviceMobile, Fingerprint, Key, LinkSimple, LockSimple, PencilSimple, Plus, ShieldCheck, Trash } from "@phosphor-icons/react";

import { IdentityError, idDelete, idGet, idPatch, idPost, refusalSentence } from "../../lib/identity/api";
import type { MeResponse } from "../../lib/identity/contract";
import type { AuthOptions, Factor } from "../../lib/identity/types";
import { probeSsoLink, ssoLinkStartPath } from "../../lib/identity/sso-link";
import { codesOf, type LinkedIdentity, type RecoveryCodes } from "../../lib/identity/types-auth";
import { webauthnAvailable } from "../../lib/identity/webauthn";
import { PASSKEY_NEEDS_DOMAIN_WORDS, PASSKEY_NEEDS_HTTPS_WORDS, passkeyBlockWords, passkeyFailureWords } from "../../lib/identity/passkey-support";
import { useGuardedCall, useIdentity } from "../../components/identity/identity-provider";
import { Ago, ErrorLine, Face, Pill, Sheet } from "../../components/identity/kit";
import type { MyProfile } from "../../lib/identity/types-account";
import { showToast } from "../client-forms";
import { registerPasskey } from "../(auth)/auth-flow";
import { RecoveryCodesView } from "../(auth)/recovery-codes";
import { TotpStep } from "../(auth)/enrol/totp-step";

function said(e: unknown): string {
  const passkey = passkeyFailureWords(e);
  if (passkey) return passkey;
  if (e instanceof IdentityError) return PASSKEY_CODES[e.code] ?? e.message;
  return refusalSentence(e);
}

/** The API's passkey refusals, in the same words the sign-in screens use. */
const PASSKEY_CODES: Record<string, string> = {
  passkey_needs_domain: PASSKEY_NEEDS_DOMAIN_WORDS,
  passkey_needs_https: PASSKEY_NEEDS_HTTPS_WORDS,
  passkey_limit_reached: "You already have 10 passkeys. Remove one first",
  passkey_verification_failed: "The device's answer didn't check out. Try again",
};

const FACTOR_ICON: Record<Factor["kind"], React.ReactNode> = {
  passkey: <Fingerprint size={16} aria-hidden />, totp: <DeviceMobile size={16} aria-hidden />,
  recovery: <LockSimple size={16} aria-hidden />, sso: <LinkSimple size={16} aria-hidden />,
};

/** The avatar tone a person chose ("0".."7"), else one derived from their id. */
export function toneOf(seed: string | null | undefined, id: string): number {
  return avatarTone(id, seed);
}

/** Your own name and avatar colour (PATCH /v1/identity/me). Nothing else about you changes here. */
function ProfileSheet({ profile, id, onClose, onSaved }: { profile: MyProfile; id: string; onClose: () => void; onSaved: (p: MyProfile) => void }) {
  const [name, setName] = useState(profile.display_name);
  const [tone, setTone] = useState(toneOf(profile.avatar_seed, id));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title="Your name and avatar" icon={<PencilSimple size={18} aria-hidden />} onClose={onClose} testId="profile-sheet">
      <form onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true); setError(null);
        try {
          const next = await idPatch<MyProfile>("/v1/identity/me", { display_name: name.trim(), avatar_seed: String(tone) });
          showToast("success", "Saved");
          onSaved(next);
        } catch (err) { setError(said(err)); setBusy(false); }
      }}>
        <div className="idn-field">
          <label htmlFor="me-name">Name</label>
          <input id="me-name" className="idn-input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} data-autofocus autoComplete="name" />
        </div>
        <div className="idn-field">
          <span className="idn-label" id="me-tone-l">Avatar colour</span>
          <div className="idn-row idn-tones" role="radiogroup" aria-labelledby="me-tone-l">
            {[0, 1, 2, 3, 4, 5, 6, 7].map((t) => (
              <button key={t} type="button" role="radio" aria-checked={tone === t} className="idn-tone" aria-label={`Colour ${t + 1}`}
                data-testid={`tone-${t}`} onClick={() => setTone(t)}>
                <Face name={name || "?"} tone={t} size={34} />
              </button>
            ))}
          </div>
        </div>
        <ErrorLine error={error} />
        <div className="idn-mf">
          <span className="idn-spacer" />
          <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="idn-btn primary" disabled={!name.trim() || busy} data-testid="profile-save">{busy ? "Saving…" : "Save"}</button>
        </div>
      </form>
    </Sheet>
  );
}

export function AccountSecurity({ me, factors: initialFactors, identities: initialIdentities, host, linked = false, providers = [], passwordMin = 12, profile: initialProfile = null, passkeyReason = null }: {
  /** /v1/auth/options passkey_reason (passkey-support): no "Add a passkey" where none can be made. */
  passkeyReason?: string | null;
  me: MeResponse | null; factors: Factor[]; identities: LinkedIdentity[]; host: string;
  /** The security policy's password minimum (GET /v1/auth/options). */
  passwordMin?: number;
  /** Your name and avatar colour (GET /v1/identity/me/profile); null when unread. */
  profile?: MyProfile | null;
  /** The org's sign-in providers (GET /v1/auth/options): the ones not linked yet offer "Link". */
  providers?: AuthOptions["providers"];
  /** Back from linking a single sign-on account (`/account?linked=1`). */
  linked?: boolean;
}) {
  const { refresh } = useIdentity();
  const [profile, setProfile] = useState<MyProfile | null>(initialProfile ?? (me ? { display_name: me.display_name, email: me.email, avatar_seed: null } : null));
  const [editingProfile, setEditingProfile] = useState(false);
  const guarded = useGuardedCall();
  const [factors, setFactors] = useState(initialFactors);
  const [identities, setIdentities] = useState(initialIdentities);
  const [sheet, setSheet] = useState<null | "totp" | "codes" | { rename: Factor } | { remove: Factor } | "totp-off">(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [rename, setRename] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pw, setPw] = useState({ current: "", next: "" });
  // "Set password" until there is one (a passkey-first account has none — BOSSAPP-812).
  const [pwSet, setPwSet] = useState<boolean | null>(null);
  const hasPassword = pwSet ?? !!me?.sign_in.password;
  const [pwError, setPwError] = useState<string | null>(null);
  // Known only in the browser: read after mount, so the server's first paint and hydration agree.
  const [canPasskey, setCanPasskey] = useState(false);
  const [noPasskey, setNoPasskey] = useState<string | null>(null);
  useEffect(() => {
    const blocked = passkeyBlockWords(passkeyReason, window.location);
    setNoPasskey(blocked);
    setCanPasskey(webauthnAvailable() && !blocked);
  }, [passkeyReason]);
  useEffect(() => {
    if (!linked) return;
    showToast("success", "Single sign-on linked");
    try { window.history.replaceState(null, "", window.location.pathname); } catch { /* keep going */ }
  }, [linked]);

  const reload = async () => {
    const r = await idGet<{ factors: Factor[] }>("/v1/identity/me/factors");
    setFactors(r.factors);
    refresh();
  };
  const act = async (reason: string, call: () => Promise<unknown>, done: string, after?: () => void) => {
    setBusy(true);
    setError(null);
    try {
      await guarded(reason, call);
      await reload();
      showToast("success", done);
      after?.();
    } catch (e) {
      setError(said(e));
    } finally {
      setBusy(false);
    }
  };

  // LINK: probe the start (fresh session, not SAML), step up if asked, then go to the provider.
  const [linkError, setLinkError] = useState<string | null>(null);
  const link = async (p: AuthOptions["providers"][number]) => {
    setLinkError(null);
    try {
      await guarded("Linking adds a way to sign in to your account.", () => probeSsoLink(p.id));
      window.location.assign(ssoLinkStartPath(p.id));
    } catch (e) {
      setLinkError(e instanceof IdentityError && e.code === "step_up_required"
        ? `${p.name} can't be linked from here — sign out and sign in with it instead.`
        : e instanceof IdentityError && e.code === "step_up_cancelled" ? null : said(e));
    }
  };
  const linkedNames = new Set(identities.map((i) => i.provider));
  const linkable = providers.filter((p) => !linkedNames.has(p.name) && !linkedNames.has(p.id));

  const passkeys = factors.filter((f) => f.kind === "passkey");
  const totp = factors.find((f) => f.kind === "totp");
  const recovery = factors.find((f) => f.kind === "recovery");
  const left = me?.sign_in.recovery_codes_remaining ?? null;
  const mfaRequired = me?.mfa_required ?? false;
  const lastFactor = (f: Factor) => mfaRequired && passkeys.length + (totp ? 1 : 0) <= 1 && (f.kind === "passkey" || f.kind === "totp");

  return (
    <div className="idn-cols">
      <div className="idn-colr">
        <section className="idn-card" aria-labelledby="acc-mfa-h">
          <div className="idn-card-h">
            <h2 id="acc-mfa-h">Sign-in and MFA</h2>
            <span className="idn-spacer" />
            {passkeys.length || totp ? <Pill tone="ok"><ShieldCheck size={13} aria-hidden /> MFA on</Pill> : <Pill tone="warn">No second factor</Pill>}
          </div>
          <div className="idn-feed" data-testid="my-factors">
            {passkeys.map((f) => (
              <div key={f.id} className="idn-fac">
                <span className="idn-d" data-kind={f.kind}>{FACTOR_ICON[f.kind]}</span>
                <span><b data-copy="data">{f.name}</b><small>Passkey · {f.last_used_at ? <>used <Ago iso={f.last_used_at} /></> : "not used yet"}</small></span>
                <span className="idn-row">
                  {f.note ? <span className="idn-dim">{f.note}</span> : null}
                  <button type="button" className="idn-btn sm ghost" aria-label={`Rename ${f.name}`} title="Rename" onClick={() => { setRename(f.name); setSheet({ rename: f }); }}><PencilSimple size={14} aria-hidden /></button>
                  <button type="button" className="idn-btn sm ghost" aria-label={`Remove ${f.name}`}
                    title={lastFactor(f) ? "Your access role needs a second factor" : "Remove"} aria-disabled={lastFactor(f) || undefined}
                    onClick={() => { if (!lastFactor(f)) setSheet({ remove: f }); }}><Trash size={14} aria-hidden /></button>
                </span>
              </div>
            ))}
            <div className="idn-fac">
              <span className="idn-d" data-kind="totp">{FACTOR_ICON.totp}</span>
              <span><b>Authenticator app</b><small>{totp ? <>6-digit codes · {totp.last_used_at ? <>used <Ago iso={totp.last_used_at} /></> : "not used yet"}</> : "Not set up"}</small></span>
              {totp ? (
                <button type="button" className="idn-btn sm" aria-disabled={lastFactor(totp) || undefined}
                  title={lastFactor(totp) ? "Your access role needs a second factor" : "Stop using the app"}
                  onClick={() => { if (!lastFactor(totp)) setSheet("totp-off"); }}>Turn off</button>
              ) : <button type="button" className="idn-btn sm" onClick={() => setSheet("totp")} data-testid="add-totp">Set up</button>}
            </div>
            <div className="idn-fac">
              <span className="idn-d" data-kind="recovery">{FACTOR_ICON.recovery}</span>
              <span><b>Recovery codes</b><small>{left ? `${left} of 10 left` : left === 0 ? "None to use · make a set" : recovery?.detail ?? "None to use · make a set"}</small></span>
              <button type="button" className="idn-btn sm" data-testid="regen-codes"
                onClick={() => act("New recovery codes replace the old ones.", async () => {
                  const r = await idPost<RecoveryCodes>("/v1/identity/me/recovery-codes", {});
                  setCodes(codesOf(r));
                }, "New codes made", () => setSheet("codes"))}>{left !== null && left < 3 ? "Make new codes" : "Regenerate"}</button>
            </div>
          </div>
          <ErrorLine error={error} />
          {canPasskey ? (
            <div className="idn-mf">
              <button type="button" className="idn-btn primary" disabled={busy} data-testid="add-passkey"
                onClick={() => {
                  let issued: string[] = [];
                  act("Adding a passkey changes how you sign in.", async () => { issued = await registerPasskey(); }, "Passkey added",
                    () => { if (issued.length) { setCodes(issued); setSheet("codes"); } });
                }}>
                <Plus size={14} weight="bold" aria-hidden /> Add a passkey
              </button>
            </div>
          ) : noPasskey ? <div className="idn-note" data-copy="prose" data-testid="no-passkey-here">{noPasskey}</div> : null}
        </section>

        <section className="idn-card" aria-labelledby="acc-pw-h">
          <div className="idn-card-h">
            <h2 id="acc-pw-h">Password</h2>
            <span className="idn-sub" data-testid="pw-state">{hasPassword ? "You have one" : "None yet · add one to sign in from any browser"}</span>
          </div>
          <form className="idn-form" onSubmit={(e) => {
            e.preventDefault();
            setPwError(null);
            guarded(hasPassword ? "Changing your password signs you out elsewhere." : "Setting a password changes how you sign in.", () => idPost("/v1/identity/me/password", hasPassword ? { current: pw.current, new: pw.next } : { new: pw.next }))
              .then(() => { setPw({ current: "", next: "" }); showToast("success", hasPassword ? "Password changed" : "Password set"); setPwSet(true); })
              .catch((err) => setPwError(err instanceof IdentityError && err.code === "password_too_short" ? `Use at least ${passwordMin} characters` : said(err)));
          }}>
            {hasPassword ? (
              <div className="idn-field">
                <label htmlFor="pw-current">Current password</label>
                <input id="pw-current" className="idn-input" type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} />
              </div>
            ) : null}
            <div className="idn-field">
              <label htmlFor="pw-new">{hasPassword ? "New password" : "Choose a password"}</label>
              <input id="pw-new" className="idn-input" type="password" autoComplete="new-password" minLength={passwordMin} value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} aria-describedby="pw-new-hint" />
              <span id="pw-new-hint" className="idn-dim idn-hint">{passwordMin} characters or more</span>
            </div>
            <ErrorLine error={pwError} />
            <div className="idn-full idn-row">
              <span className="idn-dim" data-copy="prose">{hasPassword ? "Every other session ends when it changes." : "Then email and password sign you in from any browser."}</span>
              <span className="idn-spacer" />
              <button type="submit" className="idn-btn" disabled={pw.next.length < passwordMin} data-testid="pw-submit">{hasPassword ? "Change password" : "Set password"}</button>
            </div>
          </form>
        </section>
      </div>

      <div className="idn-colr">
        <section className="idn-card" aria-labelledby="acc-sso-h">
          <div className="idn-card-h"><h2 id="acc-sso-h">Linked sign-in</h2><span className="idn-sub">SSO identities</span></div>
          {linked ? <p className="idn-dim" role="status" data-testid="sso-linked" data-copy="prose">Linked. You can now sign in with it.</p> : null}
          {identities.length ? identities.map((i) => (
            <div key={i.id} className="idn-fac">
              <span className="idn-d" data-kind="sso"><Key size={16} aria-hidden /></span>
              <span><b>{i.provider}</b><small data-copy="data">{i.label} · linked <Ago iso={i.linked_at} /></small></span>
              <button type="button" className="idn-btn sm ghost" onClick={() => guarded("Unlinking changes how you sign in.", () => idDelete(`/v1/identity/me/identities/${encodeURIComponent(i.id)}`))
                .then(() => { setIdentities((xs) => xs.filter((x) => x.id !== i.id)); showToast("success", "Unlinked"); })
                .catch((e) => setError(said(e)))}>Unlink</button>
            </div>
          )) : <div className="idn-dim">None linked</div>}
          {linkable.map((p) => (
            <div key={`link-${p.id}`} className="idn-fac" data-testid={`sso-link-${p.id}`}>
              <span className="idn-d" data-kind="sso"><LinkSimple size={16} aria-hidden /></span>
              <span><b>{p.name}</b><small>Not linked</small></span>
              <button type="button" className="idn-btn sm" onClick={() => void link(p)}
                title={`Sign in with ${p.name} once to link it`}>Link</button>
            </div>
          ))}
          <ErrorLine error={linkError} />
        </section>
        <section className="idn-card" aria-labelledby="acc-you-h">
          <div className="idn-card-h">
            <h2 id="acc-you-h">Signed in as</h2>
            <span className="idn-spacer" />
            {me && profile ? (
              <button type="button" className="idn-btn sm" onClick={() => setEditingProfile(true)} title="Your name and avatar colour" data-testid="edit-profile">
                <PencilSimple size={13} aria-hidden /> Edit
              </button>
            ) : null}
          </div>
          {me && profile ? (
            <div className="idn-row idn-me-row">
              <Face name={profile.display_name} tone={toneOf(profile.avatar_seed, me.principal_id)} size={44} />
              <span><b data-copy="data">{profile.display_name}</b><small className="idn-block idn-dim" data-copy="data">{me.email ?? ""}</small></span>
            </div>
          ) : null}
          <dl className="idn-kv">
            <dt>Email</dt><dd data-copy="data">{me?.email ?? "—"}</dd>
            <dt>Org</dt><dd data-copy="data">{me?.org.name}</dd>
            <dt>Session ends</dt><dd><Ago iso={me?.session_expires_at ?? null} /></dd>
            <dt>Host</dt><dd data-copy="data">{host}</dd>
          </dl>
        </section>
      </div>

      {editingProfile && profile && me ? (
        <ProfileSheet profile={profile} id={me.principal_id} onClose={() => setEditingProfile(false)}
          onSaved={(p) => { setProfile(p); setEditingProfile(false); refresh(); }} />
      ) : null}
      {sheet === "totp" ? (
        <Sheet title="Set up an authenticator" icon={<DeviceMobile size={18} aria-hidden />} onClose={() => setSheet(null)} testId="totp-sheet">
          <p className="idn-lede2" data-copy="prose">Scan the code with your app, then enter the 6 digits. The key is shown only now.</p>
          <TotpStep guard={guarded} onDone={(issued) => {
            reload().catch(() => undefined); showToast("success", "Authenticator on");
            if (issued.length) { setCodes(issued); setSheet("codes"); } else setSheet(null);
          }} />
        </Sheet>
      ) : null}
      {sheet === "codes" && codes ? (
        <Sheet title="Your new recovery codes" icon={<LockSimple size={18} aria-hidden />} onClose={() => undefined} locked testId="codes-sheet">
          <p className="idn-lede2" data-copy="prose">The old codes stopped working. Store these now — BOSS can't show them again.</p>
          <RecoveryCodesView codes={codes} host={host} continueLabel="Done" onContinue={() => { setCodes(null); setSheet(null); }} />
        </Sheet>
      ) : null}
      {sheet && typeof sheet === "object" && "rename" in sheet ? (
        <Sheet title="Rename passkey" icon={<PencilSimple size={18} aria-hidden />} onClose={() => setSheet(null)}>
          <form onSubmit={(e) => { e.preventDefault(); act("Renaming a passkey.", () => idPatch(`/v1/identity/me/passkeys/${encodeURIComponent(sheet.rename.id)}`, { name: rename.trim() }), "Renamed", () => setSheet(null)); }}>
            <div className="idn-field"><label htmlFor="pk-name">Name</label><input id="pk-name" className="idn-input" value={rename} maxLength={60} onChange={(e) => setRename(e.target.value)} /></div>
            <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn ghost" onClick={() => setSheet(null)}>Cancel</button><button type="submit" className="idn-btn primary" disabled={!rename.trim() || busy}>Save</button></div>
          </form>
        </Sheet>
      ) : null}
      {sheet && typeof sheet === "object" && "remove" in sheet ? (
        <Sheet title={`Remove ${sheet.remove.name}?`} icon={<Trash size={18} aria-hidden />} onClose={() => setSheet(null)}>
          <p className="idn-lede2" data-copy="prose">That device can no longer sign you in. Your other factors keep working.</p>
          <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn ghost" onClick={() => setSheet(null)}>Cancel</button>
            <button type="button" className="idn-btn danger" disabled={busy} onClick={() => act("Removing a passkey changes how you sign in.", () => idDelete(`/v1/identity/me/passkeys/${encodeURIComponent(sheet.remove.id)}`), "Passkey removed", () => setSheet(null))}>Remove</button></div>
        </Sheet>
      ) : null}
      {sheet === "totp-off" ? (
        <Sheet title="Turn off the authenticator?" icon={<DeviceMobile size={18} aria-hidden />} onClose={() => setSheet(null)}>
          <p className="idn-lede2" data-copy="prose">Codes from the app stop working. Your passkeys keep working.</p>
          <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn ghost" onClick={() => setSheet(null)}>Cancel</button>
            <button type="button" className="idn-btn danger" disabled={busy} onClick={() => act("Turning off a factor changes how you sign in.", () => idDelete("/v1/identity/me/mfa/totp"), "Authenticator off", () => setSheet(null))}>Turn off</button></div>
        </Sheet>
      ) : null}
    </div>
  );
}
