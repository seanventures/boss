"use client";

/**
 * Add a model: three kinds of thing, never one dropdown mixing them. A subscription you sign
 * into, an API key you paste, or a local model on your hardware. Every Connect stores the
 * credential and asks one tiny question — budgeted and receipted — before saying Working.
 */
import React, { useEffect, useRef, useState } from "react";
import { Copy } from "@phosphor-icons/react";

import { SetupFailure, setupWrite } from "../../lib/setup-client";
import type { CatalogueEntry, Detected, Found, SetupRefusal, SetupView } from "../../lib/setup-types";
import { Consent, toast, useBoard } from "./parts";
import { Out, Refusal, Tip } from "./pill";

type Tile = "subscription" | "api_key" | "local";

const TILES: Array<{ tile: Tile; label: string; tip: string }> = [
  { tile: "subscription", label: "Subscription", tip: "Your plan. No per-call charge; plan limits apply." },
  { tile: "api_key", label: "API key", tip: "Billed per call to your key. Counts toward the budget." },
  { tile: "local", label: "Local", tip: "Runs on your hardware. Free." },
];

const FOUND_LABEL: Record<Found, string> = {
  signed_in: "Found", found: "Found", needs_token: "Needs token", needs_file: "Needs file",
  not_on_server: "Not on server",
};

function refusalOf(error: unknown): SetupRefusal {
  if (error instanceof SetupFailure && error.refusal) return error.refusal;
  return { state: "failed", headline: "Check failed", fix: String((error as Error)?.message ?? error) };
}

function CopyChip({ text, tip }: { text: string; tip: string }) {
  return (
    <button type="button" className="su-copy" title={tip} data-tip={tip}
            data-testid="su-copy-chip"
            onClick={() => { void navigator.clipboard?.writeText(text).catch(() => undefined); }}>
      <code>{text}</code><Copy size={12} aria-label="copy" />
    </button>
  );
}

export function AddModelPanel({ initialTile, initialProvider, onDone }: {
  initialTile: Tile; initialProvider: string; onDone?: () => void;
}) {
  const { projectId, view, apply } = useBoard();
  const [tile, setTile] = useState<Tile>(initialTile);
  const [provider, setProvider] = useState(initialProvider);
  const [secret, setSecret] = useState("");
  const [file, setFile] = useState("");
  const [fileName, setFileName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  const listTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const entry: CatalogueEntry | undefined = view.catalogue.find((c) => c.provider === provider);
  const detected: Detected | undefined = view.detected.find((d) => d.provider === provider);
  const chips = view.catalogue.filter((c) => c.kind === tile);
  const signedIn = detected?.found === "signed_in";
  const onServerOnly = provider === "grok" || provider === "cursor";

  useEffect(() => {
    // A new provider is a new form: nothing typed for the last one carries over.
    setSecret(""); setFile(""); setFileName(""); setModels([]); setRefusal(null);
    setModel(entry?.example_model ?? "");
    setBaseUrl(provider === "ollama" ? (detected?.base_url ?? "") : "");
    if (provider === "ollama" && detected?.found === "found") void list("", detected.base_url ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  const list = async (key: string, url: string) => {
    try {
      const answer = await setupWrite<{ models: string[] }>(projectId, "model_catalogue", {
        provider, kind: tile, api_key: key || undefined, base_url: url || undefined,
      }, { label: "Model", background: true });
      setModels(answer.models ?? []);
    } catch { setModels([]); }
  };
  const onKey = (value: string) => {
    setSecret(value);
    if (listTimer.current) clearTimeout(listTimer.current);
    if (tile === "api_key" && provider !== "custom" && value.trim().length >= 12) {
      listTimer.current = setTimeout(() => void list(value.trim(), ""), 400);
    }
  };
  useEffect(() => () => { if (listTimer.current) clearTimeout(listTimer.current); }, []);

  const needsConsent = view.secrets.needs_consent && (tile !== "local" || !!secret);
  const fields = entry?.fields ?? [];
  let ready = !busy && (!needsConsent || consent);
  if (tile === "subscription") {
    if (onServerOnly) ready = ready && signedIn;
    else if (!signedIn) ready = ready && (provider === "codex" ? !!file : !!secret.trim());
  } else if (tile === "api_key") {
    ready = ready && (provider === "custom" ? !!baseUrl.trim() && !!model.trim()
                                            : !!secret.trim());
  } else {
    ready = ready && !!model.trim() && (provider === "ollama" || !!baseUrl.trim());
  }

  const connect = async () => {
    setBusy(true);
    setRefusal(null);
    const sent = { secret, file };
    setSecret(""); setFile(""); setFileName("");           // cleared on every submit
    try {
      const next = await setupWrite<SetupView>(projectId, "models", {
        provider, kind: tile,
        api_key: tile !== "subscription" ? sent.secret || undefined : undefined,
        credential: tile === "subscription" && sent.secret ? sent.secret : undefined,
        credential_file: tile === "subscription" && sent.file ? sent.file : undefined,
        base_url: baseUrl || undefined, model: model || undefined,
        accept_database_fallback: needsConsent ? consent : undefined,
      }, { label: "Connect" });
      apply(next);
      toast("Connected · Working");
      onDone?.();
    } catch (error) {
      setRefusal(refusalOf(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="su-add-model" data-testid="su-add-model">
      <div className="su-tiles" role="tablist" aria-label="Add a model">
        {TILES.map((t) => (
          <button key={t.tile} type="button" role="tab" aria-selected={tile === t.tile}
                  className="su-tile" title={t.tip} data-tip={t.tip}
                  data-testid={`su-tile-${t.tile}`}
                  onClick={() => {
                    setTile(t.tile);
                    const firstChip = view.catalogue.find((c) => c.kind === t.tile);
                    if (firstChip) setProvider(firstChip.provider);
                  }}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="su-chips" role="radiogroup" aria-label="Provider">
        {chips.map((c) => {
          const d = view.detected.find((x) => x.provider === c.provider);
          return (
            <button key={c.provider} type="button" role="radio" aria-checked={provider === c.provider}
                    className="su-provider" data-testid={`su-provider-${c.provider}`}
                    onClick={() => setProvider(c.provider)}>
              {c.label}
              {d && (tile === "subscription" || d.found === "found") ? (
                <Tip tip={d.found === "not_on_server" ? "Install and sign in on the BOSS server first."
                     : d.found === "signed_in" ? "Already signed in on the BOSS server."
                     : d.found === "found" ? "Found on this server." : "Paste it below."}
                     className={`su-found su-found-${d.found}`}>
                  {FOUND_LABEL[d.found]}
                </Tip>
              ) : null}
            </button>
          );
        })}
      </div>

      <div className="su-form">
        {tile === "subscription" && signedIn ? (
          <Tip tip="Already signed in on the BOSS server." className="su-faint">Found</Tip>
        ) : null}
        {tile === "subscription" && !signedIn && provider === "claude_code" ? (
          <>
            <CopyChip text="claude setup-token" tip="Run on your computer. Paste the token it prints." />
            <label className="su-field">
              <span className="su-field-label">Token</span>
              <input type="password" autoComplete="off" spellCheck={false} value={secret}
                     data-testid="su-model-token" aria-label="Token"
                     title="Stored in BOSS's secret store. Never shown again."
                     onChange={(e) => setSecret(e.target.value)} />
            </label>
          </>
        ) : null}
        {tile === "subscription" && !signedIn && provider === "codex" ? (
          <>
            <CopyChip text="~/.codex/auth.json" tip="After codex login on your computer, drop this file." />
            <label className="su-drop" data-testid="su-model-file">
              <input type="file" accept=".json,application/json" aria-label="Drop auth.json"
                     onChange={async (e) => {
                       const picked = e.target.files?.[0];
                       e.target.value = "";
                       if (!picked) return;
                       setFile(await picked.text());
                       setFileName(picked.name);
                     }} />
              <span>{fileName ? fileName : "Drop auth.json"}</span>
            </label>
          </>
        ) : null}
        {tile === "subscription" && !signedIn && onServerOnly ? (
          <Tip tip="Install and sign in on the BOSS server, then Connect." className="su-faint">
            Not on server
          </Tip>
        ) : null}

        {fields.includes("base_url") ? (
          <label className="su-field">
            <span className="su-field-label">{tile === "local" ? "URL" : "Base URL"}</span>
            <input type="url" value={baseUrl} data-testid="su-model-base-url"
                   aria-label={tile === "local" ? "URL" : "Base URL"}
                   placeholder={provider === "ollama" ? "http://127.0.0.1:11434/v1" : "http://host:8000/v1"}
                   onChange={(e) => setBaseUrl(e.target.value)}
                   onBlur={() => { if (tile === "local" && baseUrl) void list("", baseUrl); }} />
          </label>
        ) : null}
        {tile !== "subscription" && fields.includes("api_key") ? (
          <label className="su-field">
            <span className="su-field-label">API key</span>
            <input type="password" autoComplete="off" spellCheck={false} value={secret}
                   data-testid="su-model-key" aria-label="API key"
                   title="Stored in BOSS's secret store. Never shown again."
                   onChange={(e) => onKey(e.target.value)} />
          </label>
        ) : null}
        {tile !== "subscription" && fields.includes("model") ? (
          <label className="su-field">
            <span className="su-field-label">Model</span>
            <input list="su-model-options" value={model} data-testid="su-model-name"
                   aria-label="Model" onChange={(e) => setModel(e.target.value)} />
            <datalist id="su-model-options">
              {models.map((m) => <option key={m} value={m} />)}
            </datalist>
          </label>
        ) : null}
        {entry?.key_url ? <Out href={entry.key_url}>Get a key</Out> : null}
        {needsConsent ? <Consent checked={consent} onChange={setConsent} /> : null}
        <div className="su-line">
          <button type="button" className="su-btn su-btn-primary" disabled={!ready}
                  data-testid="su-model-connect"
                  title="Saves it, then asks one tiny question. Budgeted and receipted."
                  data-tip="Saves it, then asks one tiny question. Budgeted and receipted."
                  onClick={connect}>
            {busy ? "Checking…" : "Connect"}
          </button>
          {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
        </div>
      </div>
    </div>
  );
}
