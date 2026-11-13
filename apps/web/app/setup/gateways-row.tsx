"use client";

/**
 * Setup › Advanced › Gateways (BOSSAPP-887): route the coding CLIs through the org's own LLM
 * gateway (LiteLLM, Portkey, an internal one). Claude Code goes through an Anthropic gateway,
 * Codex through an OpenAI-compatible one; a sandbox gets a short-lived run token, never the key.
 * Add (name, protocol, URL, key, this project or the org), Test (one live call), Disable/Enable,
 * Remove. The key is never shown again — only that one is set.
 *
 * The row keeps Setup's anatomy (label column, body column) and the Add form is the one form
 * kit (components/form-kit.tsx, BOSSAPP-1039) — never a hand-rolled stack of bare inputs.
 */
import React, { useCallback, useEffect, useState } from "react";

import { Field, FormError, FormGrid } from "../../components/form-kit";
import { GatedButton } from "../../components/gated";
import { SectionLabel } from "../section-label";
import {
  createGateway, deleteGateway, HARNESS_WORDS, readGateways, testGateway, updateGateway, type Gateway,
} from "../../lib/llm-gateways";

export function GatewaysRow({ projectId }: { projectId: string }) {
  const [items, setItems] = useState<Gateway[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    const r = await readGateways(projectId);
    if (r.ok && "data" in r && r.data) { setItems(r.data); setError(null); }
    else if (!r.ok) setError(r.error === "missing" ? "Not on this install yet." : r.error);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const act = async (p: Promise<{ ok: boolean; error: string | null; body?: unknown }>, done: (body: unknown) => string) => {
    const r = await p;
    setSaid(r.ok ? done(r.body) : r.error ?? "Not saved.");
    if (r.ok) void load();
  };

  return (
    <section className="su-row" data-testid="su-row-gateways" aria-label="LLM gateways">
      <div className="su-row-label">
        <SectionLabel as="span" tip="Route the coding CLIs through your own LLM gateway.">Gateways</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        {error ? <div className="su-faint" data-testid="gateways-error">{error}</div> : null}
        {items && items.length === 0 ? (
          <div className="su-faint" data-testid="gateways-empty" title="Without one, the CLIs call their providers directly.">None yet.</div>
        ) : null}
        {(items ?? []).map((g) => (
          <div key={g.id} className="su-line" data-testid={`gateway-${g.id}`} data-status={g.status}>
            <strong title={`${g.protocol === "anthropic" ? "Anthropic" : "OpenAI-compatible"} gateway at ${g.base_url}, for ${g.scope === "org" ? "the whole org" : "this project"}`}>{g.name}</strong>{" "}
            <span className="su-faint" data-testid={`gateway-routes-${g.id}`}>
              {g.routes.map((h) => HARNESS_WORDS[h] ?? h).join(", ") || "nothing"}{g.status === "disabled" ? " · off" : ""}</span>
            {g.last_test ? (
              <span className="su-faint" data-testid={`gateway-test-${g.id}`} title={g.last_test.detail}>
                {" · "}{g.last_test.ok ? "Works" : "Failed"}</span>
            ) : null}
            <span className="su-line su-actions">
              <GatedButton permission="model_provider:manage" projectId={projectId} className="su-link"
                           data-testid={`gateway-test-run-${g.id}`}
                           onClick={() => void act(testGateway(projectId, g.id), (b) => {
                             const o = (b ?? {}) as { ok?: boolean; detail?: string };
                             return o.ok ? "Works." : `Failed: ${o.detail ?? "no answer"}`;
                           })}>Test</GatedButton>
              <GatedButton permission="model_provider:manage" projectId={projectId} className="su-link"
                           data-testid={`gateway-toggle-${g.id}`}
                           onClick={() => void act(updateGateway(projectId, g.id, { status: g.status === "active" ? "disabled" : "active" },
                                                                 g.status === "active" ? "Disable" : "Enable"),
                                                   () => (g.status === "active" ? "Off: CLIs go direct." : "On."))}>
                {g.status === "active" ? "Disable" : "Enable"}
              </GatedButton>
              <GatedButton permission="model_provider:manage" projectId={projectId} className="su-link"
                           data-testid={`gateway-remove-${g.id}`}
                           onClick={() => void act(deleteGateway(projectId, g.id), () => "Removed, with its key.")}>Remove</GatedButton>
            </span>
          </div>
        ))}
        {said ? <div role="status" data-testid="gateways-said">{said}</div> : null}
        <div className="su-line">
          <GatedButton permission="model_provider:manage" projectId={projectId} className="su-link"
                       data-testid="gateways-add" onClick={() => setAdding((a) => !a)}>{adding ? "Close" : "Add gateway"}</GatedButton>
        </div>
        {adding ? <AddGateway projectId={projectId} onAdded={() => { setAdding(false); setSaid("Added. Press Test."); void load(); }} /> : null}
      </div>
    </section>
  );
}

function AddGateway({ projectId, onAdded }: { projectId: string; onAdded: () => void }) {
  const [name, setName] = useState("LiteLLM");
  const [protocol, setProtocol] = useState<"openai" | "anthropic">("anthropic");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [scope, setScope] = useState<"project" | "org">("project");
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    const r = await createGateway(projectId, { name: name.trim(), protocol, base_url: url.trim(), secret, scope });
    setSecret("");
    if (r.ok) onAdded(); else setErr(r.error ?? "Not saved.");
  };
  return (
    <FormGrid label="Add gateway" testId="gateways-form">
      <Field label="Name"><input aria-label="Gateway name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
      <Field label="Speaks">
        <select aria-label="Gateway protocol" value={protocol} onChange={(e) => setProtocol(e.target.value as "openai" | "anthropic")}>
          <option value="anthropic">Anthropic (Claude Code)</option><option value="openai">OpenAI-compatible (Codex)</option></select>
      </Field>
      <Field label="URL" wide><input aria-label="Gateway URL" value={url} placeholder="https://llm-gateway.example.com" onChange={(e) => setUrl(e.target.value)} /></Field>
      <Field label="Key"><input aria-label="Gateway key" type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} /></Field>
      <Field label="For">
        <select aria-label="Gateway scope" value={scope} onChange={(e) => setScope(e.target.value as "project" | "org")}>
          <option value="project">This project</option><option value="org">The whole org</option></select>
      </Field>
      <FormError>{err}</FormError>
      <GatedButton permission="model_provider:manage" projectId={projectId} className="su-link" data-testid="gateways-save"
                   disabled={!url.trim() || !secret} onClick={() => void save()}>Save gateway</GatedButton>
    </FormGrid>
  );
}
