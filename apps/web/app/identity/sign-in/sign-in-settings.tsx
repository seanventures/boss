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
  AppleLogo, ArrowsClockwise, LockSimple, CheckCircle, EnvelopeSimple, GithubLogo, GoogleLogo, Key, PencilSimple, Plugs, Plus, ShieldCheck, Trash, Warning,
  WindowsLogo, WifiHigh, Globe,
} from "@phosphor-icons/react";

import { CanButton, useCan, useGuardedCall } from "../../../components/identity/identity-provider";
import { ErrorLine, Perm, Pill, Sheet, daysUntil, shortDate, useNow } from "../../../components/identity/kit";
import { EmptyState, ErrorState, type ReadFailure } from "../../../components/identity/states";
import { ListHeader } from "../../../components/identity/list-header";
import { GITHUB_ACCESS_AUTOMATIC, REQUIRE_SIGN_IN, policyChanges, providerView } from "../../../lib/identity/adapt-admin";
import { IdentityError, idDelete, idPatch, idPost, idPut } from "../../../lib/identity/api";
import type { AuthOptions, Posture, Provider, SecurityPolicy } from "../../../lib/identity/types";
import { PUBLIC_URL_HOWTO } from "../../../lib/identity/public-url";
import { showToast } from "../../client-forms";
import { providerRefusal, testFailureWords } from "./provider-form";
import { ProviderSheet } from "./provider-sheet";
import { ProviderSync } from "./provider-sync";

const PRESET_ICON: Partial<Record<Provider["preset"], React.ReactNode>> = {
  google: <GoogleLogo size={18} weight="bold" aria-hidden />, github: <GithubLogo size={18} weight="fill" aria-hidden />,
  microsoft: <WindowsLogo size={18} weight="fill" aria-hidden />, apple: <AppleLogo size={18} weight="fill" aria-hidden />,
};
const ROLES = ["owner", "admin", "operator", "viewer", "auditor"] as const;
const PROTOCOL_LABEL: Record<Provider["protocol"], string> = { oidc: "OIDC", saml: "SAML", oauth2: "OAuth" };
const PROTOCOL_TIP: Record<Provider["protocol"], string> = { oidc: "OpenID Connect", saml: "SAML 2.0", oauth2: "GitHub sign-in (OAuth)" };

/** The API's own sentence for a policy refusal (409 policy_refused, 422 invalid_policy). */
function refusalText(e: unknown, fallback: string): string {
  if (!(e instanceof IdentityError)) return fallback;
  return e.detail && (e.code === "policy_refused" || e.code === "invalid_policy") ? e.detail : e.message;
}

function NeedsLine({ p }: { p: "security_policy:read" | "user:read" }) {
  return <div className="idn-row idn-dim"><LockSimple size={14} aria-hidden /> Needs <Perm p={p} sm /></div>;
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

/** Remove a provider: it stops showing on sign-in for good and its sync tokens are revoked. */
function RemoveProvider({ p, onDone, onClose }: { p: Provider; onDone: (p: Provider) => void; onClose: () => void }) {
  const guarded = useGuardedCall();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title={`Remove ${p.name}?`} icon={<Trash size={18} aria-hidden />} onClose={onClose} testId="remove-provider">
      <p className="idn-lede2" data-copy="prose">Nobody can sign in with it after this, and its directory-sync tokens stop working. People keep their BOSS accounts; their links stay for the audit trail.</p>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger" disabled={busy} data-testid="remove-provider-confirm" onClick={async () => {
          setBusy(true);
          try {
            await guarded(`Removing ${p.name} changes how people sign in.`, () => idDelete(`/v1/identity/providers/${encodeURIComponent(p.id)}`));
            showToast("success", "Provider removed");
            onDone({ ...p, status: "disabled" });
            onClose();
          } catch (e) {
            if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(e instanceof IdentityError ? e.message : "Not removed");
            setBusy(false);
          }
        }}><Trash size={14} aria-hidden /> {busy ? "Removing…" : "Remove"}</button>
      </div>
    </Sheet>
  );
}

function ProviderCard({ initial, origin, onChange }: { initial: Provider; origin: string | null | undefined; onChange: (p: Provider) => void }) {
  const [p, setPState] = useState(() => providerView(initial));
  const setP = (next: Provider) => { setPState(next); onChange(next); };
  const [testing, setTesting] = useState(false);
  const [sheet, setSheet] = useState<"edit" | "sync" | "remove" | null>(null);
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
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) {
        showToast("error", e instanceof IdentityError ? providerRefusal(e.code, e.detail) || refusalText(e, "Not saved") : "Not saved");
      }
    }
  };
  return (
    <article className="idn-card idn-prov" data-testid={`provider-${p.id}`} aria-labelledby={`prov-${p.id}`}>
      <div className="idn-card-h">
        <span className="idn-ico">{PRESET_ICON[p.preset] ?? <Key size={16} aria-hidden />}</span>
        <h3 id={`prov-${p.id}`}>{p.name}</h3>
        <span className="idn-tag" title={PROTOCOL_TIP[p.protocol]}>{PROTOCOL_LABEL[p.protocol]}</span>
        <Pill tone={p.status === "active" ? "ok" : "mute"} tip={p.status === "active" ? "Shown on the sign-in page" : "Hidden from the sign-in page"}>{p.status === "active" ? "on" : "off"}</Pill>
        <span className="idn-spacer" />
        <CanButton permission="identity_provider:manage" className="idn-btn sm" disabled={testing} tip="Checks BOSS can reach it and read its keys"
          onClick={async () => {
            setTesting(true);
            try {
              const r = await idPost<{ ok: boolean; error?: string }>(`/v1/identity/providers/${encodeURIComponent(p.id)}/test`, {});
              showToast(r.ok ? "success" : "error", r.ok ? "Test passed" : testFailureWords(r.error));
            } catch (e) {
              showToast("error", e instanceof IdentityError ? e.message : "Test failed");
            } finally { setTesting(false); }
          }}>
          <Plugs size={14} aria-hidden /> {testing ? "Testing…" : "Test"}
        </CanButton>
        <CanButton permission="identity_provider:manage" className="idn-btn sm" tip="Name, client, domains and options" onClick={() => setSheet("edit")} data-testid={`provider-edit-${p.id}`}>
          <PencilSimple size={14} aria-hidden /> Edit
        </CanButton>
      </div>
      <dl className="idn-kv">
        {p.issuer ? <><dt>Issuer</dt><dd><code className="idn-mono" data-copy="data">{p.issuer}</code></dd></> : null}
        <dt>Keys</dt>
        <dd>{p.protocol === "oauth2" ? <span className="idn-dim">GitHub account, checked at each sign-in</span>
          : p.protocol === "saml" ? <span className="idn-dim">{p.saml_certificate_count} signing certificate{p.saml_certificate_count === 1 ? "" : "s"}</span>
          : <span className="idn-dim">checked by Test sign-in</span>}</dd>
        <dt>Domains</dt><dd>{p.domains.length ? p.domains.map((d) => <span key={d} className="idn-chip">{d}</span>) : <span className="idn-dim">any</span>}</dd>
        {p.groups_claim ? <><dt>Groups</dt><dd>read from “{p.groups_claim}”</dd></> : null}
      </dl>
      <div className="idn-acts2 idn-prov-opts">
        <Toggle on={p.jit_mode === "domain"} label="Create people on first sign-in" disabled={!can.allowed} reason={can.reason}
          onChange={(v) => patch({ jit_mode: v ? "domain" : "off" })} />
        <Toggle on={p.trust_idp_mfa} label="Trust its MFA" disabled={!can.allowed} reason={can.reason} onChange={(v) => patch({ trust_idp_mfa: v })} />
        <Toggle on={p.enforce_sso} label="Require it for domains" disabled={!can.allowed || !p.domains.length}
          reason={!can.allowed ? can.reason : "Add a domain first"} onChange={(v) => patch({ enforce_sso: v })} />
        <Toggle on={p.status === "active"} label="On sign-in page" disabled={!can.allowed} reason={can.reason} testId={`provider-status-${p.id}`}
          onChange={(v) => patch({ status: v ? "active" : "disabled" })} />
      </div>
      <div className="idn-row idn-prov-foot">
        <CanButton permission="identity_provider:manage" className="idn-btn sm ghost" tip={p.protocol === "oauth2" ? "Directory sync tokens" : "Group mappings and directory sync"}
          onClick={() => setSheet("sync")} data-testid={`provider-sync-${p.id}`}>
          <ArrowsClockwise size={14} aria-hidden /> {p.protocol === "oauth2" ? "Sync" : "Groups and sync"}
        </CanButton>
        <span className="idn-spacer" />
        <CanButton permission="identity_provider:manage" className="idn-btn sm ghost" tip="Stops sign-in with it for good" onClick={() => setSheet("remove")} data-testid={`provider-remove-${p.id}`}>
          <Trash size={14} aria-hidden /> Remove
        </CanButton>
      </div>
      {sheet === "edit" ? <ProviderSheet initial={p} origin={origin} onSaved={(n) => setP(n)} onClose={() => setSheet(null)} /> : null}
      {sheet === "sync" ? <ProviderSync provider={p} origin={origin} onClose={() => setSheet(null)} /> : null}
      {sheet === "remove" ? <RemoveProvider p={p} onDone={setP} onClose={() => setSheet(null)} /> : null}
    </article>
  );
}

export function SignInSettings({ providers, policy: initialPolicy, legacy, publicUrl, failures = {} }: {
  providers: Provider[]; policy: SecurityPolicy | null; legacy: Posture["legacy"] | null;
  /** Reads that FAILED (not merely refused): shown as an error, never as an empty list. */
  failures?: { providers?: ReadFailure | null; policy?: ReadFailure | null; legacy?: ReadFailure | null };
  /** GET /v1/auth/options public_origin / public_origin_source (H2); the posture's "origin" fix links here. */
  publicUrl?: Pick<AuthOptions, "public_origin" | "public_origin_source">;
}) {
  const now = useNow();
  const [list, setList] = useState<Provider[]>(() => providers.map(providerView));
  const [adding, setAdding] = useState(false);
  const origin = publicUrl?.public_origin ?? null;
  const shown = [...list].sort((a, b) => Number(b.status === "active") - Number(a.status === "active"));
  const onCount = list.filter((p) => p.status === "active").length;
  const addProvider = (
    <CanButton permission="identity_provider:manage" className="idn-btn primary" onClick={() => setAdding(true)} tip="Google, Microsoft, Okta, SAML and more" data-testid="provider-add">
      <Plus size={15} weight="bold" aria-hidden /> Add provider
    </CanButton>
  );
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
        <ListHeader id="idn-prov-h" title="Sign-in & SSO" empty={!list.length || !!failures.providers} create={addProvider}
          sub={!failures.providers && list.length ? <span title="Each one that is on shows as a button on the sign-in page">{onCount} of {list.length} on the sign-in page</span> : null} />
        {failures.providers ? <ErrorState what="sign-in providers" failure={failures.providers} testId="providers-error" /> : list.length ? (
          <div className="idn-prov-grid">
            {shown.map((p) => <ProviderCard key={p.id} initial={p} origin={origin}
              onChange={(n) => setList((l) => l.map((x) => (x.id === n.id ? n : x)))} />)}
          </div>
        ) : (
          <EmptyState icon={<Key size={20} />} title="No single sign-on yet" testId="providers-empty"
            action={addProvider}>
            People sign in with a passkey or password. Add your company's identity provider so they can use their work account.
          </EmptyState>
        )}
        {adding ? <ProviderSheet origin={origin} onSaved={(n) => setList((l) => [...l.filter((x) => x.id !== n.id), n])} onClose={() => setAdding(false)} /> : null}
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
                  <Toggle key={r} on={policy.mfa_required_roles.includes(r)} label={r[0].toUpperCase() + r.slice(1)} disabled={locked || r === "owner" || r === "admin"}
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
        ) : failures.policy ? <ErrorState compact what="the security policy" failure={failures.policy} /> : <NeedsLine p="security_policy:read" />}
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
        {legacy === null ? (failures.legacy ? <ErrorState compact what="the legacy paths" failure={failures.legacy} /> : <NeedsLine p="user:read" />)
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
