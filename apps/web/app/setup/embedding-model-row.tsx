"use client";

/**
 * EMBEDDING MODEL — its own slot, like Decisions (operator decision 2026-10-22; engine
 * services/knowledge/embedding_slot.py). Azure OpenAI (endpoint + deployment), OpenAI or any
 * OpenAI-compatible endpoint, or Ollama (local). Save & check stores the slot (the key goes into
 * the vault and is never shown again) and runs ONE tiny embedding through BOSS's gateway, in the
 * `knowledge` budget; the row shows the model's real reply or its error. No model, or a check
 * that failed: meaning-based search is off and the row says why, in the server's words.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { SetupFailure, setupWrite } from "../../lib/setup-client";
import type { EmbeddingModel, EmbeddingProvider, SetupView } from "../../lib/setup-types";
import { SectionLabel } from "../section-label";
import { Consent, toast, useBoard } from "./parts";
import { Dot } from "./pill";

const TIP = "Turns facts into vectors for meaning-based search. Checked with one tiny embedding.";
const FIELD_LABELS: Record<string, string> = {
  endpoint: "Endpoint", deployment: "Deployment", api_version: "API version", base_url: "Base URL",
  model: "Model", api_key: "API key", dimension: "Dimension",
};
const PLACEHOLDERS: Record<string, string> = {
  endpoint: "https://NAME.openai.azure.com", api_version: "2024-10-21",
  base_url: "https://api.openai.com/v1",
};

type Form = Record<string, string>;

function initialForm(slot: EmbeddingModel, provider: EmbeddingProvider | undefined): Form {
  const same = slot.configured && slot.provider === provider?.provider;
  return {
    model: same ? slot.model ?? "" : provider?.example_model ?? "",
    dimension: String(same ? slot.dimension ?? "" : provider?.example_dimension ?? ""),
    base_url: same ? slot.base_url ?? "" : provider?.example_base_url ?? "",
    endpoint: same ? slot.endpoint ?? "" : "", deployment: same ? slot.deployment ?? "" : "",
    api_version: same ? slot.api_version ?? "" : "", api_key: "",
  };
}

function CheckLine({ slot }: { slot: EmbeddingModel }) {
  const check = slot.check ?? { state: "unchecked" as const };
  const tone = check.state === "ok" ? "green" : check.state === "failed" ? "red" : "amber";
  return (
    <span className="su-line" data-testid="su-embedding-check" data-state={check.state}>
      <Dot tone={tone} />
      <strong>{slot.label} · <span data-user-text>{slot.model}</span> · {slot.dimension} dims</strong>
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

export function EmbeddingModelRow() {
  const { projectId, view, apply } = useBoard();
  const slot = view.embedding_model;
  const [editing, setEditing] = useState(false);
  const [provider, setProvider] = useState<string>(slot?.provider ?? slot?.providers[0]?.provider ?? "openai");
  const chosen = slot?.providers.find((p) => p.provider === provider);
  const [form, setForm] = useState<Form>(() => (slot ? initialForm(slot, chosen) : {}));
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (slot) setForm(initialForm(slot, chosen)); }, [provider]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!slot) return null;
  const open = editing || !slot.configured;
  const needsConsent = view.secrets.needs_consent && Boolean(form.api_key?.trim());
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
    const fields = chosen?.fields ?? [];
    const body: Record<string, unknown> = { provider };
    for (const name of fields) if (form[name]?.trim()) body[name] = form[name].trim();
    if (needsConsent) body.accept_database_fallback = consent;
    setForm((f) => ({ ...f, api_key: "" }));
    if (await write("embedding_model", body, "Save & check", "Saved · Working")) setEditing(false);
  };
  return (
    <section className="su-row" data-testid="su-row-embedding-model" aria-label="Embedding model" id="su-embedding-model">
      <div className="su-row-label"><SectionLabel as="span" tip={TIP}>Embedding model</SectionLabel></div>
      <div className="su-row-body su-stack">
        {slot.configured ? (
          <div className="su-line">
            <CheckLine slot={slot} />
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
        ) : (
          // The reason is the server's, whole, in the title; the line itself stays short.
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
            <div className="su-line">
              {(chosen?.fields ?? []).map((name) => (
                <label key={name} className="su-field">
                  <span className="su-field-label">{FIELD_LABELS[name] ?? name}</span>
                  <input type={name === "api_key" ? "password" : name === "dimension" ? "number" : "text"}
                         autoComplete="off" aria-label={FIELD_LABELS[name] ?? name}
                         data-testid={`su-embedding-${name}`} value={form[name] ?? ""}
                         placeholder={name === "api_key" && slot.has_key && slot.provider === provider
                           ? "Stored · leave empty to keep" : PLACEHOLDERS[name] ?? ""}
                         title={name === "api_key" ? "Stored in BOSS's secret store. Never shown again." : undefined}
                         onChange={(e) => setForm((f) => ({ ...f, [name]: e.target.value }))} />
                </label>
              ))}
            </div>
            {needsConsent ? <Consent checked={consent} onChange={setConsent} /> : null}
            <div className="su-line">
              <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-primary"
                           disabled={busy || !form.dimension?.trim() || (needsConsent && !consent)}
                           data-testid="su-embedding-save" onClick={() => void save()}>Save &amp; check</GatedButton>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}
