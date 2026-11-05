"use client";

/**
 * EMBEDDING MODEL — one for the whole org, in the Models step (operator decisions 2026-10-22/23;
 * engine services/knowledge/embedding_slot.py). Set once; every project uses it.
 *
 * ONE URL, NOT FIVE FIELDS. For Azure the operator pastes the endpoint or the whole Target URI
 * Azure shows (…/openai/deployments/<name>/embeddings?api-version=…) and the key; the deployment
 * is read out of the URI (editable under More) and the server swaps an API version from before
 * 2024 for one that takes `dimensions`. The DIMENSION IS NOT ASKED: the check learns it from the
 * model's own reply (a size can be set under More). OpenAI-compatible: URL, model, key. Ollama:
 * URL and model. Save & check stores the slot (the key goes into the vault, at org scope, and is
 * never shown again) and runs ONE tiny embedding through BOSS's gateway, in the `knowledge`
 * budget; the row shows the model's real reply or its error. No model, or a failed check:
 * meaning-based search is off and the row says why, in the server's words.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { SetupFailure, setupWrite } from "../../lib/setup-client";
import type { EmbeddingModel, EmbeddingProvider, SetupView } from "../../lib/setup-types";
import { SectionLabel } from "../section-label";
import { Consent, toast, useBoard } from "./parts";
import { Dot, Tip } from "./pill";

const TIP = "One for every project: turns facts into vectors for meaning search.";
const FIELD_LABELS: Record<string, string> = {
  url: "URL", endpoint: "Endpoint", deployment: "Deployment", api_version: "API version",
  base_url: "Base URL", model: "Model", api_key: "API key", dimension: "Dimension",
};
const PLACEHOLDERS: Record<string, string> = {
  endpoint: "https://NAME.openai.azure.com", api_version: "2024-10-21",
  base_url: "https://api.openai.com/v1", dimension: "Learned from the model",
};
const FIELD_TIPS: Record<string, string> = {
  url: "Paste the endpoint, or the whole Target URI from Azure.",
  deployment: "Read from the Target URI. Change it if yours differs.",
  dimension: "Empty: the model's own size. Set one to ask for smaller vectors.",
  api_key: "Stored in BOSS's secret store. Never shown again.",
};

type Form = Record<string, string>;

/** What one pasted URL says — the engine's parse_url, mirrored so the form can prefill. */
export function parseUrl(provider: string, url: string): { endpoint?: string; deployment?: string;
                                                           api_version?: string; base_url?: string } {
  const raw = (url ?? "").trim();
  if (!raw) return {};
  let parsed: URL;
  try { parsed = new URL(raw); } catch { return {}; }
  if (provider === "azure") {
    const found = /\/openai\/deployments\/([^/]+)/.exec(parsed.pathname);
    const version = parsed.searchParams.get("api-version") ?? "";
    const year = Number(/^(\d{4})-\d{2}-\d{2}/.exec(version)?.[1] ?? 0);
    return { endpoint: parsed.origin, api_version: year >= 2024 ? version : "2024-10-21",
             ...(found ? { deployment: decodeURIComponent(found[1]) } : {}) };
  }
  let base = `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
  for (const tail of ["/embeddings", "/api/embeddings", "/api/embed"]) {
    if (base.endsWith(tail)) { base = base.slice(0, -tail.length); break; }
  }
  if (provider === "ollama" && base.endsWith("/v1")) base = base.slice(0, -3);
  return { base_url: base };
}

function urlLabel(provider: string): string {
  return provider === "azure" ? "Endpoint or Target URI" : "URL";
}

function initialForm(slot: EmbeddingModel, provider: EmbeddingProvider | undefined): Form {
  const same = slot.configured && slot.provider === provider?.provider;
  const url = same ? (provider?.provider === "azure" ? slot.endpoint : slot.base_url) ?? ""
    : provider?.example_base_url ?? "";
  return {
    url, model: same ? slot.model ?? "" : provider?.example_model ?? "",
    deployment: same ? slot.deployment ?? "" : "", api_version: same ? slot.api_version ?? "" : "",
    dimension: same && slot.dimension_set ? String(slot.dimension ?? "") : "",
    // An older API's form (endpoint, base_url, …) still renders and still saves.
    endpoint: same ? slot.endpoint ?? "" : "", base_url: same ? slot.base_url ?? "" : provider?.example_base_url ?? "",
    api_key: "",
  };
}

function CheckLine({ slot }: { slot: EmbeddingModel }) {
  const check = slot.check ?? { state: "unchecked" as const };
  const tone = check.state === "ok" ? "green" : check.state === "failed" ? "red" : "amber";
  return (
    <span className="su-line" data-testid="su-embedding-check" data-state={check.state}>
      <Dot tone={tone} />
      <strong>{slot.label} · <span data-user-text>{slot.model}</span>{slot.dimension ? ` · ${slot.dimension} dims` : ""}</strong>
      <span className="su-faint">{check.state === "ok" ? "Working" : check.state === "failed" ? "Check failed" : "Not checked"}</span>
      {/* The model's own reply, or its own error: the provider's words, not BOSS's copy. */}
      {check.state !== "unchecked" ? (
        <code className="su-mono" data-user-text data-testid="su-embedding-reply">
          {check.state === "ok" ? check.reply ?? "answered" : check.error ?? check.reply ?? "no reason given"}
        </code>
      ) : null}
    </span>
  );
}

export function EmbeddingModelRow({ startOpen = false }: {
  /** A deep link to it (`focus=embedding_model`): the person came to set it, so the form is open. */
  startOpen?: boolean;
}) {
  const { projectId, view, apply } = useBoard();
  const slot = view.embedding_model;
  const [editing, setEditing] = useState(false);
  // OPTIONAL, SO FOLDED (boss-1, 2026-11-05): an amber line and an empty form on every visit read
  // as "Setup is broken". Not set is one quiet line and Set up; the form opens on a press.
  const [settingUp, setSettingUp] = useState(startOpen);
  useEffect(() => { if (startOpen) setSettingUp(true); }, [startOpen]);
  const [more, setMore] = useState(false);
  const [provider, setProvider] = useState<string>(slot?.provider ?? slot?.providers[0]?.provider ?? "openai");
  const chosen = slot?.providers.find((p) => p.provider === provider);
  const [form, setForm] = useState<Form>(() => (slot ? initialForm(slot, chosen) : {}));
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (slot) setForm(initialForm(slot, chosen)); }, [provider]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!slot) return null;
  const open = editing || (!slot.configured && settingUp);
  const needsConsent = view.secrets.needs_consent && Boolean(form.api_key?.trim());
  const derived = parseUrl(provider, form.url ?? "");
  const fields = chosen?.fields ?? [];
  // A base Azure endpoint names no deployment: then the deployment is asked for in the row.
  const askDeployment = provider === "azure" && fields.includes("url") && Boolean(derived.endpoint)
    && !derived.deployment && !form.deployment?.trim();
  const shown = askDeployment ? [...fields.slice(0, 1), "deployment", ...fields.slice(1)] : fields;
  const extra = (chosen?.more ?? []).filter((name) => !shown.includes(name));
  const value = (name: string) => form[name] || (name === "deployment" ? derived.deployment ?? ""
    : name === "api_version" ? (derived.api_version ?? "") : "");
  const write = async (op: string, body: Record<string, unknown>, label: string, done: string) => {
    setBusy(true);
    try {
      const next = await setupWrite<SetupView>(projectId, op, body, { label });
      apply(next);
      const state = next.embedding_model?.check?.state;
      toast(state === "failed" ? "Saved · check failed" : done, state === "failed" ? "error" : "success");
      return true;
    } catch (error) {
      toast((error as SetupFailure).message || "Not saved", "error");
      return false;
    } finally { setBusy(false); }
  };
  const save = async () => {
    const body: Record<string, unknown> = { provider };
    for (const name of [...shown, ...extra]) if (form[name]?.trim()) body[name] = form[name].trim();
    if (needsConsent) body.accept_database_fallback = consent;
    setForm((f) => ({ ...f, api_key: "" }));
    if (await write("embedding_model", body, "Save & check", "Saved · Working")) setEditing(false);
  };
  const ready = !(fields.includes("url") && provider === "azure" && !form.url?.trim())
    && !(fields.includes("dimension") && !form.dimension?.trim());
  const input = (name: string) => (
    <label key={name} className="su-field">
      <span className="su-field-label">{name === "url" ? urlLabel(provider) : FIELD_LABELS[name] ?? name}</span>
      <input type={name === "api_key" ? "password" : name === "dimension" ? "number" : "text"}
             autoComplete="off" aria-label={name === "url" ? urlLabel(provider) : FIELD_LABELS[name] ?? name}
             data-testid={`su-embedding-${name}`} value={value(name)}
             placeholder={name === "api_key" && slot.has_key && slot.provider === provider
               ? "Stored · leave empty to keep"
               : name === "url" ? chosen?.example_base_url ?? PLACEHOLDERS.endpoint : PLACEHOLDERS[name] ?? ""}
             title={FIELD_TIPS[name]}
             onChange={(e) => setForm((f) => ({ ...f, [name]: e.target.value }))} />
    </label>
  );
  return (
    <section className="su-row" data-testid="su-row-embedding-model" aria-label="Embedding model" id="su-embedding-model">
      <div className="su-row-label"><SectionLabel as="span" tip={TIP}>Embedding model</SectionLabel></div>
      <div className="su-row-body su-stack">
        {slot.configured ? (
          <div className="su-line">
            <CheckLine slot={slot} />
            {slot.scope === "project" ? (
              <Tip tip="From an older setup. Check or save to share it." className="su-chip">This project</Tip>
            ) : (
              <Tip tip="Set once: every project in this org uses it." className="su-chip">All projects</Tip>
            )}
            <span className="su-spacer" />
            <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm"
                         disabled={busy} data-testid="su-embedding-recheck"
                         title="One tiny embedding through BOSS's budget." data-tip="One tiny embedding through BOSS's budget."
                         onClick={() => void write("embedding_model_check", {}, "Check", "Checked · Working")}>Check</GatedButton>
            <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm"
                         disabled={busy} data-testid="su-embedding-change" onClick={() => setEditing((v) => !v)}>
              {editing ? "Cancel" : "Change"}
            </GatedButton>
            <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm"
                         disabled={busy} data-testid="su-embedding-remove"
                         onClick={() => void write("embedding_model_remove", {}, "Remove", "Removed")}>Remove</GatedButton>
          </div>
        ) : null}
        {slot.search ? (
          <span className="su-line su-faint" data-testid="su-embedding-on">
            <span>Meaning search on</span>
            {slot.reembed_pending ? (
              <span title="New vectors are made in the background, a batch at a time."
                    data-testid="su-embedding-pending">Re-embedding: {slot.reembed_pending} left</span>
            ) : null}
          </span>
        ) : !slot.configured ? (
          <span className="su-line">
            {/* The reason is the server's, whole, in the title; the line itself stays short. */}
            <span className="su-line su-faint" data-testid="su-embedding-off" title={slot.reason}>
              <Dot tone="grey" />Not set · search uses text and graph only
            </span>
            <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm"
                         data-testid="su-embedding-setup" aria-expanded={settingUp}
                         title="Optional. Adds meaning search across every project."
                         data-tip="Optional. Adds meaning search across every project."
                         onClick={() => setSettingUp((v) => !v)}>
              {settingUp ? "Cancel" : "Set up"}
            </GatedButton>
          </span>
        ) : (
          <span className="su-line" data-testid="su-embedding-off" title={slot.reason}>
            <Dot tone="amber" />Search: text and graph only
          </span>
        )}
        {open ? (
          <div className="su-stack" data-testid="su-embedding-form">
            <div className="su-line" role="radiogroup" aria-label="Embedding provider">
              {slot.providers.map((p) => (
                <button key={p.provider} type="button" role="radio" aria-checked={p.provider === provider}
                        className={`su-btn su-btn-sm${p.provider === provider ? " su-btn-primary" : ""}`}
                        data-testid={`su-embedding-provider-${p.provider}`} onClick={() => setProvider(p.provider)}>
                  {p.label}
                </button>
              ))}
            </div>
            <div className="su-line">{shown.map(input)}</div>
            {extra.length ? (
              <button type="button" className="su-link" data-testid="su-embedding-more" aria-expanded={more}
                      title="Deployment, API version and vector size." onClick={() => setMore((v) => !v)}>
                {more ? "Less" : "More"}
              </button>
            ) : null}
            {more && extra.length ? <div className="su-line" data-testid="su-embedding-more-fields">{extra.map(input)}</div> : null}
            {needsConsent ? <Consent checked={consent} onChange={setConsent} /> : null}
            <div className="su-line">
              <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-primary"
                           disabled={busy || !ready || (needsConsent && !consent)}
                           data-testid="su-embedding-save" onClick={() => void save()}>Save &amp; check</GatedButton>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
