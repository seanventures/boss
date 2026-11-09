"use client";

/**
 * ADD OR EDIT A SIGN-IN PROVIDER (IDENTITY.md §9.8): pick a kind, fill what that kind needs, and
 * see the URLs to paste into the IdP. The client secret is write-only: BOSS never shows it back,
 * only whether one is stored. After Add, the sheet stays open on "Finish in your IdP" with the
 * redirect URL, which needs the new provider's id.
 */
import React, { useState } from "react";
import { Check, Copy, Plus, PencilSimple } from "@phosphor-icons/react";

import { useGuardedCall } from "../../../components/identity/identity-provider";
import { ErrorLine, Sheet } from "../../../components/identity/kit";
import { providerView } from "../../../lib/identity/adapt-admin";
import { IdentityError, idPatch, idPost } from "../../../lib/identity/api";
import type { Provider } from "../../../lib/identity/types";
import type { ProviderDetail } from "../../../lib/identity/types-sso";
import { showToast } from "../../client-forms";
import {
  KINDS, createBody, emptyForm, formOf, idpUrls, missing, needsIssuer, patchBody, protocolOf, providerRefusal, reserveProviderId,
  type ProviderForm, type ProviderKind,
} from "./provider-form";
import { GUIDES, SIGN_IN_DOC, type Guide } from "../../../lib/identity/sso-guides";

/** A value the admin copies into their IdP: label, the value, Copy. */
export function CopyRow({ label, value, tip }: { label: string; value: string; tip?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="idn-field">
      <span className="idn-label" title={tip}>{label}</span>
      <div className="idn-secret idn-copyrow">
        <code data-copy="data" data-raw-ok>{value}</code>
        <button type="button" className="idn-btn sm" onClick={async () => {
          try { await navigator.clipboard.writeText(value); setCopied(true); showToast("success", "Copied"); }
          catch { showToast("error", "Copy blocked, select it"); }
        }}>
          {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />} {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

/** The URLs for the IdP, or why they cannot be shown yet. */
export function IdpUrls({ origin, provider }: { origin: string | null | undefined; provider: { id: string | null; protocol: Provider["protocol"]; saml_sp_entity_id?: string | null; saved?: boolean } }) {
  const urls = idpUrls(origin, provider);
  if (!origin) {
    return <p className="idn-note" data-copy="prose">Set the public URL (below) first: your IdP sends people back to it.</p>;
  }
  if (!urls) return <p className="idn-dim idn-small" data-copy="prose">The redirect URL appears once the provider is added.</p>;
  return <div className="idn-sso-urls">{urls.map((u) => <CopyRow key={u.label} label={u.label} value={u.value} tip={u.tip} />)}</div>;
}

function Check2({ on, label, tip, onChange }: { on: boolean; label: string; tip?: string; onChange: (v: boolean) => void }) {
  return (
    <label className="idn-check" title={tip}><input type="checkbox" checked={on} onChange={(e) => onChange(e.target.checked)} /> {label}</label>
  );
}

function Field({ id, label, tip, children }: { id: string; label: string; tip?: string; children: React.ReactNode }) {
  return (
    <div className="idn-field">
      <label htmlFor={id} title={tip}>{label}</label>
      {children}
    </div>
  );
}

export function ProviderSheet({ initial, origin, onSaved, onClose }: {
  /** The provider to edit; absent: Add. */
  initial?: ProviderDetail;
  origin: string | null | undefined;
  onSaved: (p: Provider) => void;
  onClose: () => void;
}) {
  const editing = Boolean(initial);
  const [form, setForm] = useState<ProviderForm | null>(initial ? formOf(initial) : null);
  const [added, setAdded] = useState<Provider | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  // Reserved now, so the redirect URL can be pasted into the IdP BEFORE its client id exists.
  const [reserved] = useState<string | null>(() => (initial ? null : reserveProviderId()));
  const guarded = useGuardedCall();
  const set = (change: Partial<ProviderForm>) => setForm((f) => (f ? { ...f, ...change } : f));

  if (added) {
    return (
      <Sheet title={`${added.name} added`} icon={<Check size={18} aria-hidden />} onClose={onClose} wide testId="provider-added">
        <p className="idn-lede2" data-copy="prose">Finish in your IdP: register BOSS with these values, then press Test sign-in on its card.</p>
        <IdpUrls origin={origin} provider={added} />
        <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn primary" onClick={onClose} data-testid="provider-done">Done</button></div>
      </Sheet>
    );
  }

  if (!form) {
    return (
      <Sheet title="Add a sign-in provider" icon={<Plus size={18} aria-hidden />} onClose={onClose} wide testId="provider-kind">
        <p className="idn-lede2" data-copy="prose">People will see it as a button on the sign-in page.</p>
        <div className="idn-sso-kinds" role="list">
          {KINDS.map((k) => (
            <button key={k.kind} type="button" className="idn-sso-kind" role="listitem" title={k.tip} data-testid={`kind-${k.kind}`}
              onClick={() => setForm(emptyForm(k.kind))}>
              <b>{k.label}</b>
            </button>
          ))}
        </div>
      </Sheet>
    );
  }

  const kind: ProviderKind = form.kind;
  const protocol = protocolOf(kind);
  const why = missing(form, editing);
  const kindLabel = KINDS.find((k) => k.kind === kind)?.label ?? "Provider";

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      if (initial) {
        const body = patchBody(initial, form);
        if (!Object.keys(body).length) { onClose(); return; }
        const out = await guarded(`Changing ${initial.name} changes how people sign in.`,
          () => idPatch<unknown>(`/v1/identity/providers/${encodeURIComponent(initial.id)}`, body));
        onSaved(providerView(out));
        showToast("success", "Provider saved");
        onClose();
      } else {
        const out = providerView(await guarded("Adding a provider changes how people sign in.",
          () => idPost<unknown>("/v1/identity/providers", createBody(form, reserved))));
        onSaved(out);
        setAdded(out);
      }
    } catch (e) {
      if (e instanceof IdentityError && e.code === "step_up_cancelled") { setBusy(false); return; }
      setError(e instanceof IdentityError ? providerRefusal(e.code, e.detail) || e.message : "Not saved");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet title={editing ? `Edit ${initial!.name}` : `Add ${kindLabel}`} icon={editing ? <PencilSimple size={18} aria-hidden /> : <Plus size={18} aria-hidden />}
      onClose={onClose} wide testId="provider-sheet">
      {!editing ? (
        <section className="idn-sso-first" data-testid="provider-first">
          <b>1. Paste this into {kindLabel}</b>
          <IdpUrls origin={origin} provider={{ id: reserved, protocol, saved: false }} />
          <SetupGuide kind={kind} />
        </section>
      ) : null}
      {!editing ? <b className="idn-sso-step">2. Its client ID and secret</b> : null}
      <div className="idn-form idn-sso-form">
        <Field id="pv-name" label="Name" tip="The button people see on the sign-in page">
          <input id="pv-name" className="idn-input" value={form.name} onChange={(e) => set({ name: e.target.value })} data-autofocus />
        </Field>
        {kind === "microsoft" && !editing ? (
          <Field id="pv-tenant" label="Tenant ID" tip="Entra ID › Overview › Tenant ID">
            <input id="pv-tenant" className="idn-input" value={form.tenant} placeholder="contoso.onmicrosoft.com" onChange={(e) => set({ tenant: e.target.value })} />
          </Field>
        ) : null}
        {needsIssuer(kind) ? (
          <Field id="pv-issuer" label={kind === "saml" ? "IdP entity ID" : "Issuer URL"} tip={editing ? "Fixed once added: add a new provider to change it" : kind === "saml" ? "From your IdP's SAML metadata" : "Your provider's base URL"}>
            <input id="pv-issuer" className="idn-input" value={form.issuer} readOnly={editing} placeholder={kind === "okta" ? "https://acme.okta.com" : kind === "keycloak" ? "https://sso.acme.dev/realms/acme" : "https://"}
              onChange={(e) => set({ issuer: e.target.value })} />
          </Field>
        ) : null}
        {protocol === "saml" ? (
          <>
            <Field id="pv-sso" label="Sign-in URL" tip="Your IdP's SAML single sign-on URL (https)">
              <input id="pv-sso" className="idn-input" value={form.saml_sso_url} placeholder="https://" onChange={(e) => set({ saml_sso_url: e.target.value })} />
            </Field>
            <Field id="pv-cert" label={editing ? "New signing certificate" : "Signing certificate"} tip={editing ? `${initial!.saml_certificate_count} stored, paste one to replace` : "The IdP's X.509 certificate (PEM)"}>
              <textarea id="pv-cert" className="idn-input idn-mono" rows={4} value={form.saml_certificate} placeholder="-----BEGIN CERTIFICATE-----"
                onChange={(e) => set({ saml_certificate: e.target.value })} />
            </Field>
          </>
        ) : (
          <>
            <Field id="pv-cid" label="Client ID" tip="From the app you registered in the IdP">
              <input id="pv-cid" className="idn-input" value={form.client_id} onChange={(e) => set({ client_id: e.target.value })} autoComplete="off" />
            </Field>
            {form.client_auth === "client_secret" ? (
              <Field id="pv-secret" label="Client secret" tip="Stored encrypted; never shown again">
                <input id="pv-secret" className="idn-input" type="password" autoComplete="new-password" value={form.client_secret}
                  placeholder={initial?.has_client_secret ? "Stored, type to replace" : ""} onChange={(e) => set({ client_secret: e.target.value })} />
              </Field>
            ) : null}
          </>
        )}
        <Field id="pv-domains" label="Email domains" tip="Who this provider is for; blank means anyone it vouches for">
          <input id="pv-domains" className="idn-input" value={form.domains} placeholder="acme.dev" onChange={(e) => set({ domains: e.target.value })} />
        </Field>
        <div className="idn-field idn-full idn-sso-checks">
          <Check2 on={form.jit} label="Create people on first sign-in" tip="Only for the email domains above" onChange={(v) => set({ jit: v })} />
          <Check2 on={form.trust_mfa} label="Trust its MFA" tip="Skip BOSS's second factor when the IdP did one" onChange={(v) => set({ trust_mfa: v })} />
          <Check2 on={form.enforce_sso} label="Require it for these domains" tip="People on these domains can only sign in here" onChange={(v) => set({ enforce_sso: v })} />
        </div>
        <div className="idn-full">
          <button type="button" className="idn-link" aria-expanded={advanced} onClick={() => setAdvanced((a) => !a)}>{advanced ? "Hide advanced" : "Advanced"}</button>
        </div>
        {advanced ? (
          <>
            <Field id="pv-gclaim" label="Groups claim" tip="The token field that lists the person's groups">
              <input id="pv-gclaim" className="idn-input" value={form.groups_claim} placeholder="groups" onChange={(e) => set({ groups_claim: e.target.value })} />
            </Field>
            {protocol === "oidc" ? (
              <Field id="pv-scopes" label="Scopes" tip="What BOSS asks the IdP for">
                <input id="pv-scopes" className="idn-input" value={form.scopes} onChange={(e) => set({ scopes: e.target.value })} />
              </Field>
            ) : null}
            {protocol === "oidc" ? (
              <div className="idn-field">
                <span className="idn-label" title="How BOSS proves itself to the IdP">Client sign-in</span>
                <div className="idn-segs" role="radiogroup" aria-label="Client sign-in">
                  <button type="button" role="radio" aria-checked={form.client_auth === "client_secret"} aria-pressed={form.client_auth === "client_secret"}
                    onClick={() => set({ client_auth: "client_secret" })}>Secret</button>
                  <button type="button" role="radio" aria-checked={form.client_auth === "private_key_jwt"} aria-pressed={form.client_auth === "private_key_jwt"}
                    title="BOSS signs with its own key; register its public key in the IdP" onClick={() => set({ client_auth: "private_key_jwt" })}>Signed key</button>
                </div>
              </div>
            ) : null}
            {protocol === "saml" && !editing ? (
              <Field id="pv-spe" label="BOSS entity ID" tip="Only if your IdP needs a specific one">
                <input id="pv-spe" className="idn-input" value={form.saml_sp_entity_id} placeholder="(default)" onChange={(e) => set({ saml_sp_entity_id: e.target.value })} />
              </Field>
            ) : null}
          </>
        ) : null}
      </div>
      {/* Adding shows these FIRST (step 1, above); editing shows them here. Never both. */}
      {editing ? <>
        <div className="idn-hr" />
        <span className="idn-label">For your IdP</span>
        <IdpUrls origin={origin} provider={{ id: initial?.id ?? null, protocol, saml_sp_entity_id: form.saml_sp_entity_id || null }} />
      </> : null}
      {initial && origin && form.client_auth === "private_key_jwt" && initial.client_auth === "private_key_jwt" ? (
        <CopyRow label="BOSS public key" value={`${origin.replace(/\/+$/, "")}/v1/identity/providers/${encodeURIComponent(initial.id)}/jwks`} tip="Register this key URL in your IdP" />
      ) : null}
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-dim idn-small">{why}</span>
        <span className="idn-spacer" />
        {!editing ? <button type="button" className="idn-btn ghost" onClick={() => setForm(null)}>Back</button> : null}
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={Boolean(why) || busy} onClick={save} data-testid="provider-save" title={why || undefined}>
          {busy ? "Saving…" : editing ? "Save" : "Add provider"}
        </button>
      </div>
    </Sheet>
  );
}

/**
 * "HOW TO SET THIS UP" for one provider kind (lib/identity/sso-guides.ts, the same table the docs
 * are written from). Open the first time someone adds that kind, folded after that.
 */
export function SetupGuide({ kind }: { kind: string }) {
  const guide = (GUIDES as Record<string, Guide | undefined>)[kind];
  const [open] = useState(() => {
    try {
      const key = `boss.sso-guide-seen.${kind}`;
      const seen = window.localStorage.getItem(key) === "1";
      window.localStorage.setItem(key, "1");
      return !seen;
    } catch { return true; }
  });
  if (!guide) return null;
  return (
    <details className="idn-sso-guide" open={open} data-testid="setup-guide">
      <summary>How to set this up</summary>
      <ol>
        {guide.steps.map((st, i) => (
          <li key={i} data-copy="prose">
            {st.text.replace("{redirect}", "the URL above")}
            {st.link ? <> <a className="idn-link" href={st.link.href} target="_blank" rel="noopener noreferrer">{st.link.label} ↗</a></> : null}
          </li>
        ))}
      </ol>
      <p className="idn-small" data-copy="prose"><b>Copy back:</b> {guide.copyBack.join(", ")}</p>
      <p className="idn-small idn-dim" data-copy="prose"><b>Watch out:</b> {guide.gotcha}</p>
      <a className="idn-link idn-small" href={`${SIGN_IN_DOC}#${guide.anchor}`} target="_blank" rel="noopener noreferrer">Full guide ↗</a>
    </details>
  );
}
