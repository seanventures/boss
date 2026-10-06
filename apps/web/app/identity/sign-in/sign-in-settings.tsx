"use client";

/**
 * IDENTITY › SIGN-IN & SSO (IDENTITY.md §9.8): the providers people sign in with (OIDC, SAML and
 * the social presets) with a test sign-in, JWKS status, domains, JIT, group mappings and SCIM
 * keys (ids only — no part of a key, ever); the security policy; the network bridge with
 * "Require sign-in"; and the legacy paths still in use, with their countdown.
 */
import React, { useState } from "react";
import {
  AppleLogo, CheckCircle, GithubLogo, GoogleLogo, Key, Plugs, ShieldCheck, Warning, WindowsLogo, WifiHigh,
} from "@phosphor-icons/react";

import { CanButton, useCan, useGuardedCall } from "../../../components/identity/identity-provider";
import { Ago, Pill, daysUntil, shortDate, useNow } from "../../../components/identity/kit";
import { IdentityError, idPatch, idPost, idPut } from "../../../lib/identity/api";
import type { Posture, Provider, SecurityPolicy } from "../../../lib/identity/types";
import { showToast } from "../../client-forms";

const PRESET_ICON: Partial<Record<Provider["preset"], React.ReactNode>> = {
  google: <GoogleLogo size={18} weight="bold" aria-hidden />, github: <GithubLogo size={18} weight="fill" aria-hidden />,
  microsoft: <WindowsLogo size={18} weight="fill" aria-hidden />, apple: <AppleLogo size={18} weight="fill" aria-hidden />,
};
const ROLES = ["owner", "admin", "operator", "viewer", "auditor"] as const;

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
  const [p, setP] = useState(initial);
  const [testing, setTesting] = useState(false);
  const can = useCan("identity_provider:manage");
  const guarded = useGuardedCall();
  const patch = async (change: Partial<Provider>) => {
    const before = p;
    setP({ ...p, ...change });
    try {
      setP(await guarded(`Changing ${p.name} changes how people sign in.`, () => idPatch<Provider>(`/v1/identity/providers/${encodeURIComponent(p.id)}`, change)));
      showToast("success", "Provider saved");
    } catch (e) {
      setP(before);
      showToast("error", e instanceof IdentityError ? e.message : "Not saved");
    }
  };
  return (
    <article className="idn-card idn-prov" data-testid={`provider-${p.id}`} aria-labelledby={`prov-${p.id}`}>
      <div className="idn-card-h">
        <span className="idn-ico">{PRESET_ICON[p.preset] ?? <Key size={16} aria-hidden />}</span>
        <h3 id={`prov-${p.id}`}>{p.name}</h3>
        <span className="idn-tag">{p.kind === "oauth2" ? "OAuth" : p.kind.toUpperCase()}</span>
        <Pill tone={p.status === "active" ? "ok" : p.status === "error" ? "bad" : "mute"}>{p.status}</Pill>
        <span className="idn-spacer" />
        <CanButton permission="identity_provider:manage" className="idn-btn sm" disabled={testing} tip="Runs discovery, JWKS and the redirect check"
          onClick={async () => {
            setTesting(true);
            try {
              const r = await idPost<{ ok: boolean }>(`/v1/identity/providers/${encodeURIComponent(p.id)}/test`, {});
              showToast(r.ok ? "success" : "error", r.ok ? "Test passed" : "Test failed");
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
        <dd>{p.kind === "oauth2" ? <span className="idn-dim">no id_token (OAuth)</span> : p.jwks.ok
          ? <span className="idn-ok"><CheckCircle size={13} aria-hidden /> JWKS ok · <Ago iso={p.jwks.checked_at} /></span>
          : <span className="idn-bad"><Warning size={13} aria-hidden /> JWKS failing</span>}</dd>
        <dt>Domains</dt><dd>{p.domains.length ? p.domains.map((d) => <span key={d} className="idn-chip">{d}</span>) : <span className="idn-dim">any</span>}</dd>
        {p.group_mappings.length ? (
          <><dt>Groups</dt><dd>{p.group_mappings.map((g) => <span key={g.idp_group} className="idn-chip"><code className="idn-mono">{g.idp_group}</code> → {g.group}</span>)}</dd></>
        ) : null}
        {p.scim_keys.length ? (
          <><dt>SCIM keys</dt><dd>{p.scim_keys.map((k) => <span key={k.id} className="idn-chip" title="Only the key's id is ever shown"><code className="idn-mono">{k.id}</code> · used <Ago iso={k.last_used_at} /></span>)}</dd></>
        ) : null}
      </dl>
      <div className="idn-acts2 idn-prov-opts">
        <Toggle on={p.jit === "domains"} label="Create people on first sign-in" disabled={!can.allowed} reason={can.reason}
          onChange={(v) => patch({ jit: v ? "domains" : "off" })} />
        <Toggle on={p.trust_idp_mfa} label="Trust its MFA" disabled={!can.allowed} reason={can.reason} onChange={(v) => patch({ trust_idp_mfa: v })} />
        <Toggle on={p.enforce_sso} label="Require it for domains" disabled={!can.allowed || !p.domains.length}
          reason={!can.allowed ? can.reason : "Add a domain first"} onChange={(v) => patch({ enforce_sso: v })} />
      </div>
    </article>
  );
}

export function SignInSettings({ providers, policy: initialPolicy, legacy }: {
  providers: Provider[]; policy: SecurityPolicy | null; legacy: Posture["legacy"] | null;
}) {
  const now = useNow();
  const [saved, setSaved] = useState(initialPolicy);
  const [policy, setPolicy] = useState(initialPolicy);
  const [saving, setSaving] = useState(false);
  const manage = useCan("security_policy:manage");
  const guarded = useGuardedCall();
  const locked = !manage.allowed;
  const dirty = JSON.stringify(policy) !== JSON.stringify(saved);
  const set = (change: Partial<SecurityPolicy>) => setPolicy((p) => (p ? { ...p, ...change } : p));

  const save = async (next: SecurityPolicy | null = policy, why = "Policy changes apply to everyone.") => {
    if (!next) return;
    setSaving(true);
    try {
      const out = await guarded(why, () => idPut<SecurityPolicy>("/v1/identity/policy", next));
      setSaved(out);
      setPolicy(out);
      showToast("success", "Policy saved");
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) showToast("error", e instanceof IdentityError ? e.message : "Not saved");
    } finally { setSaving(false); }
  };

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
          <CanButton permission="security_policy:manage" className="idn-btn sm primary" disabled={!dirty || saving} onClick={() => save()} data-testid="save-policy">
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
                <Toggle on={policy.github_require_user_repo_access} label="Agents need your repo access" disabled={locked} reason={manage.reason} onChange={(v) => set({ github_require_user_repo_access: v })} />
              </div>
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

      <section className="idn-card" id="network" aria-labelledby="idn-net-h">
        <div className="idn-card-h">
          <h2 id="idn-net-h"><WifiHigh size={16} aria-hidden /> Network bridge</h2>
          {policy?.network_mode.active ? <Pill tone="bad">on{policy.network_mode.ends_at ? ` · ends ${shortDate(policy.network_mode.ends_at)}` : ""}</Pill> : <Pill tone="ok">off</Pill>}
          <span className="idn-spacer" />
          {policy?.network_mode.active ? (
            <CanButton permission="security_policy:manage" className="idn-btn sm danger" data-testid="require-sign-in" tip="Ends the network bridge now; everyone signs in"
              onClick={() => save({ ...policy, network_mode: { active: false, ends_at: null } }, "Everyone on this network must sign in after this.")}>Require sign-in</CanButton>
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
              <div key={l.kind} className="idn-sa" data-testid={`legacy-${l.kind}`}>
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
