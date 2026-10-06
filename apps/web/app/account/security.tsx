"use client";

/**
 * MY ACCOUNT › SECURITY (IDENTITY.md §9, §4.2–§4.7): your passkeys (add, rename, remove), the
 * authenticator app (on or off), recovery codes (how many left; regenerate, shown once), your
 * password, and the SSO identities linked to you. Every change here needs a fresh second factor:
 * the API answers step_up_required and the step-up sheet opens (useGuardedCall).
 */
import React, { useEffect, useState } from "react";
import { DeviceMobile, Fingerprint, Key, LinkSimple, LockSimple, PencilSimple, Plus, ShieldCheck, Trash } from "@phosphor-icons/react";

import { IdentityError, idDelete, idGet, idPatch, idPost } from "../../lib/identity/api";
import type { MeResponse } from "../../lib/identity/contract";
import type { Factor } from "../../lib/identity/types";
import type { LinkedIdentity, RecoveryCodes } from "../../lib/identity/types-auth";
import { webauthnAvailable } from "../../lib/identity/webauthn";
import { useGuardedCall, useIdentity } from "../../components/identity/identity-provider";
import { Ago, ErrorLine, Pill, Sheet } from "../../components/identity/kit";
import { showToast } from "../client-forms";
import { registerPasskey } from "../(auth)/auth-flow";
import { RecoveryCodesView } from "../(auth)/recovery-codes";
import { TotpStep } from "../(auth)/enrol/totp-step";

function said(e: unknown): string {
  return e instanceof IdentityError ? e.message : "That didn't work";
}

const FACTOR_ICON: Record<Factor["kind"], React.ReactNode> = {
  passkey: <Fingerprint size={16} aria-hidden />, totp: <DeviceMobile size={16} aria-hidden />,
  recovery: <LockSimple size={16} aria-hidden />, sso: <LinkSimple size={16} aria-hidden />,
};

export function AccountSecurity({ me, factors: initialFactors, identities: initialIdentities, host }: {
  me: MeResponse | null; factors: Factor[]; identities: LinkedIdentity[]; host: string;
}) {
  const { refresh } = useIdentity();
  const guarded = useGuardedCall();
  const [factors, setFactors] = useState(initialFactors);
  const [identities, setIdentities] = useState(initialIdentities);
  const [sheet, setSheet] = useState<null | "totp" | "codes" | { rename: Factor } | { remove: Factor } | "totp-off">(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [rename, setRename] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pw, setPw] = useState({ current: "", next: "" });
  const [pwError, setPwError] = useState<string | null>(null);
  // Known only in the browser: read after mount, so the server's first paint and hydration agree.
  const [canPasskey, setCanPasskey] = useState(false);
  useEffect(() => { setCanPasskey(webauthnAvailable()); }, []);

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
                <span><b data-copy="data">{f.name}</b><small>Passkey · used <Ago iso={f.last_used_at} /></small></span>
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
              <span><b>Authenticator app</b><small>{totp ? <>TOTP · used <Ago iso={totp.last_used_at} /></> : "Not set up"}</small></span>
              {totp ? (
                <button type="button" className="idn-btn sm" aria-disabled={lastFactor(totp) || undefined}
                  title={lastFactor(totp) ? "Your access role needs a second factor" : "Stop using the app"}
                  onClick={() => { if (!lastFactor(totp)) setSheet("totp-off"); }}>Turn off</button>
              ) : <button type="button" className="idn-btn sm" onClick={() => setSheet("totp")} data-testid="add-totp">Set up</button>}
            </div>
            <div className="idn-fac">
              <span className="idn-d" data-kind="recovery">{FACTOR_ICON.recovery}</span>
              <span><b>Recovery codes</b><small>{left !== null ? `${left} of 10 left` : recovery?.detail ?? "Not made yet"}</small></span>
              <button type="button" className="idn-btn sm" data-testid="regen-codes"
                onClick={() => act("New recovery codes replace the old ones.", async () => {
                  const r = await idPost<RecoveryCodes>("/v1/identity/me/recovery-codes", {});
                  setCodes(r.codes);
                }, "New codes made", () => setSheet("codes"))}>{left !== null && left < 3 ? "Make new codes" : "Regenerate"}</button>
            </div>
          </div>
          <ErrorLine error={error} />
          {canPasskey ? (
            <div className="idn-mf">
              <button type="button" className="idn-btn primary" disabled={busy} data-testid="add-passkey"
                onClick={() => act("Adding a passkey changes how you sign in.", () => registerPasskey(), "Passkey added")}>
                <Plus size={14} weight="bold" aria-hidden /> Add a passkey
              </button>
            </div>
          ) : null}
        </section>

        <section className="idn-card" aria-labelledby="acc-pw-h">
          <div className="idn-card-h">
            <h2 id="acc-pw-h">Password</h2>
            <span className="idn-sub">{me?.sign_in.password ? "Set" : "Not set"}</span>
          </div>
          <form className="idn-form" onSubmit={(e) => {
            e.preventDefault();
            setPwError(null);
            guarded("Changing your password signs you out elsewhere.", () => idPost("/v1/identity/me/password", { current: pw.current, new: pw.next }))
              .then(() => { setPw({ current: "", next: "" }); showToast("success", "Password changed"); })
              .catch((err) => setPwError(err instanceof IdentityError && err.code === "password_too_short" ? "Use at least 12 characters" : said(err)));
          }}>
            {me?.sign_in.password ? (
              <div className="idn-field">
                <label htmlFor="pw-current">Current password</label>
                <input id="pw-current" className="idn-input" type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} />
              </div>
            ) : null}
            <div className="idn-field">
              <label htmlFor="pw-new">New password</label>
              <input id="pw-new" className="idn-input" type="password" autoComplete="new-password" minLength={12} value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} aria-describedby="pw-new-hint" />
              <span id="pw-new-hint" className="idn-dim idn-hint">12 characters or more</span>
            </div>
            <ErrorLine error={pwError} />
            <div className="idn-full idn-row">
              <span className="idn-dim" data-copy="prose">Every other session ends when it changes.</span>
              <span className="idn-spacer" />
              <button type="submit" className="idn-btn" disabled={pw.next.length < 12}>Change password</button>
            </div>
          </form>
        </section>
      </div>

      <div className="idn-colr">
        <section className="idn-card" aria-labelledby="acc-sso-h">
          <div className="idn-card-h"><h2 id="acc-sso-h">Linked sign-in</h2><span className="idn-sub">SSO identities</span></div>
          {identities.length ? identities.map((i) => (
            <div key={i.id} className="idn-fac">
              <span className="idn-d" data-kind="sso"><Key size={16} aria-hidden /></span>
              <span><b>{i.provider}</b><small data-copy="data">{i.label} · linked <Ago iso={i.linked_at} /></small></span>
              <button type="button" className="idn-btn sm ghost" onClick={() => guarded("Unlinking changes how you sign in.", () => idDelete(`/v1/identity/me/identities/${encodeURIComponent(i.id)}`))
                .then(() => { setIdentities((xs) => xs.filter((x) => x.id !== i.id)); showToast("success", "Unlinked"); })
                .catch((e) => setError(said(e)))}>Unlink</button>
            </div>
          )) : <div className="idn-dim">None linked</div>}
        </section>
        <section className="idn-card" aria-labelledby="acc-you-h">
          <div className="idn-card-h"><h2 id="acc-you-h">Signed in as</h2></div>
          <dl className="idn-kv">
            <dt>Name</dt><dd data-copy="data">{me?.display_name}</dd>
            <dt>Email</dt><dd data-copy="data">{me?.email ?? "—"}</dd>
            <dt>Org</dt><dd data-copy="data">{me?.org.name}</dd>
            <dt>Session ends</dt><dd><Ago iso={me?.session_expires_at ?? null} /></dd>
            <dt>Host</dt><dd data-copy="data">{host}</dd>
          </dl>
        </section>
      </div>

      {sheet === "totp" ? (
        <Sheet title="Set up an authenticator" icon={<DeviceMobile size={18} aria-hidden />} onClose={() => setSheet(null)} testId="totp-sheet">
          <p className="idn-lede2" data-copy="prose">Scan the code with your app, then enter the 6 digits. The key is shown only now.</p>
          <TotpStep onDone={() => { setSheet(null); reload().catch(() => undefined); showToast("success", "Authenticator on"); }} />
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
