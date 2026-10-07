"use client";

/**
 * IDENTITY › SIGN-IN & SSO (IDENTITY.md §9.8): the providers people sign in with (OIDC, SAML and
 * the social presets) with a test sign-in, domains and JIT; the security policy; the network
 * bridge with "Require sign-in"; and the legacy paths still in use, with their countdown.
 *
 * S5's policy rules: network trust is never offered back ON (only "Require sign-in", which the
 * API makes one-way); email (SMTP) is a status, not a switch; "Agents need your repo access" is
 * automatic until pinned, and can be reset to automatic. A save sends only what changed.
 */
import React, { useState } from "react";
import {
  AppleLogo, CheckCircle, EnvelopeSimple, GithubLogo, GoogleLogo, Key, Plugs, ShieldCheck, Warning, WindowsLogo, WifiHigh,
  Globe,
} from "@phosphor-icons/react";

import { CanButton, useCan, useGuardedCall } from "../../../components/identity/identity-provider";
import { Pill, daysUntil, shortDate, useNow } from "../../../components/identity/kit";
import { GITHUB_ACCESS_AUTOMATIC, REQUIRE_SIGN_IN, policyChanges, providerView } from "../../../lib/identity/adapt-admin";
import { IdentityError, idPatch, idPost, idPut } from "../../../lib/identity/api";
import type { AuthOptions, Posture, Provider, SecurityPolicy } from "../../../lib/identity/types";
import { PUBLIC_URL_HOWTO } from "../../../lib/identity/public-url";
import { showToast } from "../../client-forms";

const PRESET_ICON: Partial<Record<Provider["preset"], React.ReactNode>> = {
  google: <GoogleLogo size={18} weight="bold" aria-hidden />, github: <GithubLogo size={18} weight="fill" aria-hidden />,
  microsoft: <WindowsLogo size={18} weight="fill" aria-hidden />, apple: <AppleLogo size={18} weight="fill" aria-hidden />,
};
const ROLES = ["owner", "admin", "operator", "viewer", "auditor"] as const;
const PROTOCOL_LABEL: Record<Provider["protocol"], string> = { oidc: "OIDC", saml: "SAML", oauth2: "OAuth" };

/** The API's own sentence for a policy refusal (409 policy_refused, 422 invalid_policy). */
function refusalText(e: unknown, fallback: string): string {
  if (!(e instanceof IdentityError)) return fallback;
  return e.detail && (e.code === "policy_refused" || e.code === "invalid_policy") ? e.detail : e.message;
}

function Toggle({ on, label, onChange, disabled, reason, testId }: {
  on: boolean; label: string; onChange: (v: boolean) => void; disabled?: boolean; reason?: string; testId?: string;
}) {
  return (
    <button type="button" className="idn-tg" aria-pressed={on} aria-disabled={disabled || undefined} data-locked={disabled || undefined}
      title={disabled ? reason : undefined} data-testid={testId}
      onClick={() => { if (!disabled) onChange(!on); }}>
      <span className="idn-sw" aria-hidden />{label}
      {disabled && reason ? <span className="idn-sr">{reason}</span> : null}
    </button>
  );
}

function Choice<T extends string | number>({ label, value, options, onChange, disabled, reason }: {
  label: string; value: T; options: { v: T; label: string }[]; onChange: (v: T) => void; disabled?: boolean; reason?: string;
}) {
  return (
    <div className="idn-field">
      <span className="idn-label">{label}</span>
      <div className="idn-segs" role="radiogroup" aria-label={label}>
        {options.map((o) => (
          <button key={String(o.v)} type="button" role="radio" aria-checked={value === o.v} aria-pressed={value === o.v}
            aria-disabled={disabled || undefined} title={disabled ? reason : undefined}
            onClick={() => { if (!disabled) onChange(o.v); }}>{o.label}</button>
        ))}
      </div>
    </div>
  );
}

function ProviderCard({ initial }: { initial: Provider }) {
  const [p, setP] = useState(() => providerView(initial));
  const [testing, setTesting] = useState(false);
  const can = useCan("identity_provider:manage");
  const guarded = useGuardedCall();
  const patch = async (change: Partial<Provider>) => {
    const before = p;
    setP({ ...p, ...change });
    try {
      setP(providerView(await guarded(`Changing ${p.name} changes how people sign in.`, () => idPatch<Provider>(`/v1/identity/providers/${encodeURIComponent(p.id)}`, change))));
      showToast("success", "Provider saved");
    } catch (e) {
      setP(before);
      showToast("error", refusalText(e, "Not saved"));
    }
  };
  return (
    <article className="idn-card idn-prov" data-testid={`provider-${p.id}`} aria-labelledby={`prov-${p.id}`}>
      <div className="idn-card-h">
        <span className="idn-ico">{PRESET_ICON[p.preset] ?? <Key size={16} aria-hidden />}</span>
        <h3 id={`prov-${p.id}`}>{p.name}</h3>
        <span className="idn-tag">{PROTOCOL_LABEL[p.protocol]}</span>
        <Pill tone={p.status === "active" ? "ok" : "mute"}>{p.status}</Pill>
        <span className="idn-spacer" />
        <CanButton permission="identity_provider:manage" className="idn-btn sm" disabled={testing} tip="Runs discovery, JWKS and the redirect check"
          onClick={async () => {
            setTesting(true);
            try {
              const r = await idPost<{ ok: boolean; error?: string }>(`/v1/identity/providers/${encodeURIComponent(p.id)}/test`, {});
              showToast(r.ok ? "success" : "error", r.ok ? "Test passed" : r.error ? `Test failed · ${r.error}` : "Test failed");
            } catch (e) {
              showToast("error", e instanceof IdentityError ? e.message : "Test failed");
            } finally { setTesting(false); }
          }}>
          <Plugs size={14} aria-hidden /> {testing ? "Testing…" : "Test sign-in"}
        </CanButton>
      </div>
      <dl className="idn-kv">
        {p.issuer ? <><dt>Issuer</dt><dd><code className="idn-mono" data-copy="data">{p.issuer}</code></dd></> : null}
        <dt>Keys</dt>
        <dd>{p.protocol === "oauth2" ? <span className="idn-dim">no id_token (OAuth)</span>
          : p.protocol === "saml" ? <span className="idn-dim">{p.saml_certificate_count} signing certificate{p.saml_certificate_count === 1 ? "" : "s"}</span>
          : <span className="idn-dim">checked by Test sign-in</span>}</dd>
        <dt>Domains</dt><dd>{p.domains.length ? p.domains.map((d) => <span key={d} className="idn-chip">{d}</span>) : <span className="idn-dim">any</span>}</dd>
        {p.groups_claim ? <><dt>Groups</dt><dd><code className="idn-mono">{p.groups_claim}</code> claim</dd></> : null}
      </dl>
      <div className="idn-acts2 idn-prov-opts">
        <Toggle on={p.jit_mode === "domain"} label="Create people on first sign-in" disabled={!can.allowed} reason={can.reason}
          onChange={(v) => patch({ jit_mode: v ? "domain" : "off" })} />
        <Toggle on={p.trust_idp_mfa} label="Trust its MFA" disabled={!can.allowed} reason={can.reason} onChange={(v) => patch({ trust_idp_mfa: v })} />
        <Toggle on={p.enforce_sso} label="Require it for domains" disabled={!can.allowed || !p.domains.length}
          reason={!can.allowed ? can.reason : "Add a domain first"} onChange={(v) => patch({ enforce_sso: v })} />
      </div>
    </article>
  );
}

export function SignInSettings({ providers, policy: initialPolicy, legacy, publicUrl }: {
  providers: Provider[]; policy: SecurityPolicy | null; legacy: Posture["legacy"] | null;
  /** GET /v1/auth/options public_origin / public_origin_source (H2); the posture's "origin" fix links here. */
  publicUrl?: Pick<AuthOptions, "public_origin" | "public_origin_source">;
}) {
  const now = useNow();
  const [saved, setSaved] = useState(initialPolicy);
  const [policy, setPolicy] = useState(initialPolicy);
  const [saving, setSaving] = useState(false);
  const manage = useCan("security_policy:manage");
  const guarded = useGuardedCall();
  const locked = !manage.allowed;
  const dirty = Boolean(saved && policy && Object.keys(policyChanges(saved, policy)).length);
  const set = (change: Partial<SecurityPolicy>) => setPolicy((p) => (p ? { ...p, ...change } : p));

  /** PUT exactly `body` (only what changed, or one deliberate action), then take the API's view. */
  const send = async (body: Record<string, unknown>, why = "Policy changes apply to everyone.") => {
    if (!Object.keys(body).length) return;
    setSaving(true);
    try {
      const out = await guarded(why, () => idPut<SecurityPolicy>("/v1/identity/policy", body));
      setSaved(out);
      // Unsaved edits to other fields survive a one-click action (Require sign-in, Automatic).
      setPolicy((p) => {
        if (!p || !saved) return out;
        const pending = Object.entries(policyChanges(saved, p)).filter(([k]) => !(k in body));
        return { ...out, ...Object.fromEntries(pending) } as SecurityPolicy;
      });
      showToast("success", "Policy saved");
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) showToast("error", refusalText(e, "Not saved"));
    } finally { setSaving(false); }
  };
  const save = () => { if (saved && policy) void send(policyChanges(saved, policy) as Record<string, unknown>); };

  return (
    <>
      <section aria-labelledby="idn-prov-h">
        <div className="idn-sec-h">
          <h1 id="idn-prov-h" className="idn-h1">Sign-in & SSO</h1>
          <span className="idn-sub" title="Each shows as a button on the sign-in page">{providers.length} providers · on sign-in</span>
        </div>
        <div className="idn-prov-grid">
          {providers.map((p) => <ProviderCard key={p.id} initial={p} />)}
          {!providers.length ? <div className="idn-card idn-dim" data-copy="prose">No providers yet: people sign in with a passkey or password.</div> : null}
        </div>
      </section>

      <section className="idn-card" id="policy" aria-labelledby="idn-policy-h">
        <div className="idn-card-h">
          <h2 id="idn-policy-h">Security policy</h2>
          {locked ? <span className="idn-sub" title={manage.reason}>Read only · {manage.reason}</span> : null}
          <span className="idn-spacer" />
          {dirty ? <button type="button" className="idn-btn sm ghost" onClick={() => setPolicy(saved)}>Discard</button> : null}
          <CanButton permission="security_policy:manage" className="idn-btn sm primary" disabled={!dirty || saving} onClick={save} data-testid="save-policy">
            <ShieldCheck size={14} aria-hidden /> {saving ? "Saving…" : "Save policy"}
          </CanButton>
        </div>
        {policy ? (
          <div className="idn-form">
            <div className="idn-field idn-full">
              <span className="idn-label">MFA required for</span>
              <div className="idn-acts2">
                {ROLES.map((r) => (
                  <Toggle key={r} on={policy.mfa_required_roles.includes(r)} label={r} disabled={locked || r === "owner" || r === "admin"}
                    reason={locked ? manage.reason : "Always required"}
                    onChange={(v) => set({ mfa_required_roles: v ? [...policy.mfa_required_roles, r] : policy.mfa_required_roles.filter((x) => x !== r) })} />
                ))}
              </div>
            </div>
            <Choice label="Idle sign-out" value={policy.session_idle_minutes} disabled={locked} reason={manage.reason} onChange={(v) => set({ session_idle_minutes: v })}
              options={[{ v: 15, label: "15 m" }, { v: 60, label: "1 h" }, { v: 240, label: "4 h" }, { v: 720, label: "12 h" }, { v: 1440, label: "24 h" }]} />
            <Choice label="Session at most" value={policy.session_absolute_hours} disabled={locked} reason={manage.reason} onChange={(v) => set({ session_absolute_hours: v })}
              options={[{ v: 24, label: "1 d" }, { v: 72, label: "3 d" }, { v: 168, label: "7 d" }, { v: 720, label: "30 d" }]} />
            <Choice label="Password at least" value={policy.password_min_length} disabled={locked} reason={manage.reason} onChange={(v) => set({ password_min_length: v })}
              options={[{ v: 12, label: "12" }, { v: 14, label: "14" }, { v: 16, label: "16" }, { v: 20, label: "20" }]} />
            <Choice label="Keys expire within" value={policy.key_max_days} disabled={locked} reason={manage.reason} onChange={(v) => set({ key_max_days: v })}
              options={[{ v: 30, label: "30 d" }, { v: 90, label: "90 d" }, { v: 180, label: "180 d" }, { v: 365, label: "365 d" }]} />
            <Choice label="Grants last at most" value={policy.delegation_max_days} disabled={locked} reason={manage.reason} onChange={(v) => set({ delegation_max_days: v })}
              options={[{ v: 7, label: "7 d" }, { v: 30, label: "30 d" }, { v: 90, label: "90 d" }]} />
            <Choice label="Agent chain depth" value={policy.delegation_max_depth} disabled={locked} reason={manage.reason} onChange={(v) => set({ delegation_max_depth: v })}
              options={[1, 2, 3, 4, 5].map((n) => ({ v: n, label: String(n) }))} />
            <div className="idn-field idn-full">
              <span className="idn-label">Rules</span>
              <div className="idn-acts2">
                <Toggle on={policy.breach_check} label="Check breached passwords" disabled={locked} reason={manage.reason} onChange={(v) => set({ breach_check: v })} />
                <Toggle on={policy.delegation_until_revoked} label="Allow grants until revoked" disabled={locked} reason={manage.reason} onChange={(v) => set({ delegation_until_revoked: v })} />
                <Toggle on={policy.key_no_expiry_for_service_accounts} label="Service keys without expiry" disabled={locked} reason={manage.reason} onChange={(v) => set({ key_no_expiry_for_service_accounts: v })} />
                <Toggle on={policy.github_require_user_repo_access} label="Agents need your repo access" testId="toggle-github-access"
                  disabled={locked} reason={manage.reason} onChange={(v) => set({ github_require_user_repo_access: v })} />
                {policy.github_require_user_repo_access_auto && policy.github_require_user_repo_access === saved?.github_require_user_repo_access ? (
                  <span className="idn-chip" data-testid="github-access-auto" title="On once more than one person uses this BOSS">automatic</span>
                ) : saved && !saved.github_require_user_repo_access_auto ? (
                  <CanButton permission="security_policy:manage" className="idn-btn sm ghost" data-testid="github-access-reset" disabled={saving}
                    tip="Follow the install again: on once more than one person"
                    onClick={() => void send({ ...GITHUB_ACCESS_AUTOMATIC }, "Agents' repo access rule changes for everyone.")}>Automatic</CanButton>
                ) : null}
              </div>
            </div>
            <div className="idn-field idn-full">
              <span className="idn-label">Email</span>
              <span className={policy.smtp ? "idn-ok" : "idn-dim"} data-testid="smtp-status" title="Set in the install's settings, not here">
                <EnvelopeSimple size={13} aria-hidden /> {policy.smtp ? "Email (SMTP) configured" : "No email (SMTP) configured"}
              </span>
            </div>
            <div className="idn-field idn-full">
              <span className="idn-label">Self sign-up</span>
              <div className="idn-row">
                <Toggle on={policy.signup.enabled} label="People may create accounts" testId="toggle-signup"
                  disabled={locked || !policy.smtp} reason={!policy.smtp ? "Needs email (SMTP) first" : manage.reason}
                  onChange={(v) => set({ signup: { ...policy.signup, enabled: v } })} />
                <input className="idn-input idn-domains" aria-label="Allowed sign-up domains" placeholder="acme.dev, acme.io" disabled={locked || !policy.signup.enabled}
                  value={policy.signup.domains.join(", ")}
                  onChange={(e) => set({ signup: { ...policy.signup, domains: e.target.value.split(/[\s,]+/).filter(Boolean) } })} />
              </div>
            </div>
          </div>
        ) : <div className="idn-row idn-dim">Policy needs <code className="idn-perm">security_policy:read</code></div>}
      </section>

      <section className="idn-card" id="public-url" aria-labelledby="idn-url-h">
        <div className="idn-card-h">
          <h2 id="idn-url-h"><Globe size={16} aria-hidden /> Public URL</h2>
          {publicUrl?.public_origin ? <Pill tone="ok">bound</Pill> : publicUrl?.public_origin === null ? <Pill tone="bad">not set</Pill> : null}
        </div>
        <p className="idn-fine-l" data-copy="prose" data-testid="public-url-line">
          {publicUrl?.public_origin
            ? <>Sign-in, passkeys and emailed links use <code data-testid="public-url-value">{publicUrl.public_origin}</code>{publicUrl.public_origin_source === "env" ? " (BOSS_PUBLIC_URL)." : publicUrl.public_origin_source === "claim" ? " (bound at claim)." : "."}</>
            : publicUrl?.public_origin === null
            ? <>No public URL is bound, so emailed links are off. {PUBLIC_URL_HOWTO}</>
            : "This BOSS does not report its public URL."}
        </p>
      </section>

      <section className="idn-card" id="network" aria-labelledby="idn-net-h">
        <div className="idn-card-h">
          <h2 id="idn-net-h"><WifiHigh size={16} aria-hidden /> Network bridge</h2>
          {policy?.network_mode.active ? <Pill tone="bad">on{policy.network_mode.ends_at ? ` · ends ${shortDate(policy.network_mode.ends_at)}` : ""}</Pill> : <Pill tone="ok">off</Pill>}
          <span className="idn-spacer" />
          {policy?.network_mode.active ? (
            <CanButton permission="security_policy:manage" className="idn-btn sm danger" data-testid="require-sign-in" tip="Ends the network bridge now; everyone signs in"
              onClick={() => void send({ ...REQUIRE_SIGN_IN }, "Everyone on this network must sign in after this.")}>Require sign-in</CanButton>
          ) : null}
        </div>
        <p className="idn-fine-l" data-copy="prose">
          {policy?.network_mode.active
            ? "Anyone who can reach this BOSS acts as an Operator until the bridge ends."
            : "Everyone signs in as themselves. There is no network trust."}
        </p>
      </section>

      <section className="idn-card" id="legacy" aria-labelledby="idn-legacy-h">
        <div className="idn-card-h"><h2 id="idn-legacy-h">Legacy paths</h2><span className="idn-sub">retire each one</span></div>
        {legacy === null ? <div className="idn-row idn-dim">Needs <code className="idn-perm">user:read</code></div>
          : legacy.length ? legacy.map((l) => {
            const days = daysUntil(l.until, now);
            return (
              <div key={`${l.kind}-${l.until ?? "none"}`} className="idn-sa" data-testid={`legacy-${l.kind}`}>
                <span className="idn-ico warn"><Warning size={15} aria-hidden /></span>
                <span><b>{l.label}</b><small>{l.until ? `stops ${shortDate(l.until)}` : "until rotated in BOSS"}</small></span>
                <span className="idn-r">{days !== null ? <Pill tone={days <= 14 ? "warn" : "mute"}>{days} days left</Pill> : <Pill tone="bad">no end date</Pill>}</span>
              </div>
            );
          }) : <span className="idn-ok"><CheckCircle size={14} aria-hidden /> None in use</span>}
      </section>
    </>
  );
}
