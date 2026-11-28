"use client";

/**
 * Provider terms (BOSSAPP-1264).
 *
 * The prompt stays on this screen until region, log retention, and whether
 * the provider may train have been shown. A fact that was not recorded reads
 * unknown. This screen does not fill a region or a training flag in.
 */

import React, { useState } from "react";
import { apiWrite } from "../../lib/api-write";
import { GatedButton } from "../../components/gated";

const EMPTY = "Not shown";
const FACT_KEYS = ["region", "log_retention", "may_train"] as const;

type FactKey = (typeof FACT_KEYS)[number];
type Facts = Record<FactKey, string>;
type Gate = "held" | "clear";

const HIDDEN: Facts = { region: EMPTY, log_retention: EMPTY, may_train: EMPTY };

const METERS: { key: FactKey; testId: string; label: string }[] = [
  { key: "region", testId: "provider-terms-region", label: "Region" },
  { key: "log_retention", testId: "provider-terms-retention", label: "Log retention" },
  { key: "may_train", testId: "provider-terms-train", label: "May train" },
];

type Reply = { ok: boolean; body: Record<string, unknown>; error: string };

function wordsOf(value: unknown): string {
  if (value == null) return "";
  return String(value).trim();
}

function shownFacts(body: Record<string, unknown> | null): Facts | null {
  if (!body || body.shown !== true) return null;
  const next = { ...HIDDEN };
  for (const key of FACT_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(body, key)) return null;
    const text = wordsOf(body[key]);
    next[key] = text === "" || text.toLowerCase() === "unknown" ? "unknown" : text;
  }
  return next;
}

async function post(path: string, body: Record<string, unknown>): Promise<Reply> {
  let response: Response;
  try {
    response = await apiWrite(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, body: {}, error: "Terms could not be read." };
  }
  let parsed: unknown = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  const record = parsed && typeof parsed === "object" ? parsed as Record<string, unknown> : {};
  if (!response.ok) {
    const detail = wordsOf(record.detail);
    return { ok: false, body: record, error: detail || "Terms could not be read." };
  }
  return { ok: true, body: record, error: "" };
}

function Meter({ testId, label, value }: { testId: string; label: string; value: string }) {
  const state = value === EMPTY ? "empty" : value === "unknown" ? "unknown" : "known";
  const tip = state === "unknown" ? "No recorded fact" : state === "empty" ? "Not on screen yet" : undefined;
  return (
    <div className="pt-meter" data-testid={testId} data-state={state}>
      <span className="pt-meter-label">{label}</span>
      <span className="pt-value" data-fact title={tip}>{value}</span>
    </div>
  );
}

export function ProviderTermsScreen({ projectId = "" }: { projectId?: string | null }) {
  const project = wordsOf(projectId);
  const [provider, setProvider] = useState("");
  const [prompt, setPrompt] = useState("");
  const [facts, setFacts] = useState<Facts>(HIDDEN);
  const [gate, setGate] = useState<Gate>("held");
  const [disclosureId, setDisclosureId] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [saved, setSaved] = useState("");
  const [knownRegion, setKnownRegion] = useState("");
  const [knownRetention, setKnownRetention] = useState("");
  const [knownTrain, setKnownTrain] = useState("");

  const hold = () => {
    setGate("held");
    setFacts(HIDDEN);
    setDisclosureId("");
  };

  const editProvider = (value: string) => {
    setProvider(value);
    setResult("");
    hold();
  };

  const editPrompt = (value: string) => {
    setPrompt(value);
    setResult("");
    hold();
  };

  const canShow = busy === "" && project !== "" && provider.trim() !== "" && prompt.trim() !== "";

  const showTerms = async () => {
    if (!canShow) return;
    setBusy("show");
    setError("");
    setResult("");
    setSaved("");
    try {
      const disclosed = await post("/v1/provider-terms/disclose", {
        project_id: project,
        provider: provider.trim(),
        prompt: prompt.trim(),
      });
      if (!disclosed.ok) {
        hold();
        setError(disclosed.error);
        return;
      }
      const id = wordsOf(disclosed.body.disclosure_id);
      const shown = await post("/v1/provider-terms/shown", {
        project_id: project,
        disclosure_id: id,
      });
      const next = shownFacts(shown.ok ? shown.body : null);
      if (!shown.ok || !next || !id) {
        hold();
        setError(shown.ok ? "The three facts are not shown." : shown.error);
        return;
      }
      setFacts(next);
      setDisclosureId(id);
      setGate("clear");
    } finally {
      setBusy("");
    }
  };

  const runPrompt = async () => {
    if (gate !== "clear" || !disclosureId || busy) return;
    setBusy("run");
    setError("");
    setSaved("");
    try {
      const started = await post("/v1/provider-terms/run", {
        project_id: project,
        disclosure_id: disclosureId,
      });
      if (!started.ok || started.body.started !== true) {
        hold();
        setResult("");
        setError(started.error || "Terms could not be read.");
        return;
      }
      setResult("Run started");
    } finally {
      setBusy("");
    }
  };

  const saveKnown = async () => {
    if (busy) return;
    const name = provider.trim();
    if (!project) {
      setError("Pick a project.");
      return;
    }
    if (!name) {
      setError("Name the provider.");
      return;
    }
    const train = knownTrain.trim().toLowerCase();
    if (train && train !== "yes" && train !== "no") {
      setError("may_train must be yes or no.");
      return;
    }
    const region = knownRegion.trim();
    const retention = knownRetention.trim();
    const body: Record<string, string> = { project_id: project, provider: name };
    if (region) body.region = region;
    if (retention) body.log_retention = retention;
    if (train) body.may_train = train;
    if (!region && !retention && !train) {
      setError("Type a fact first.");
      return;
    }
    setBusy("save");
    setError("");
    try {
      const savedReply = await post("/v1/provider-terms/record", body);
      if (!savedReply.ok) {
        setError(savedReply.error);
        return;
      }
      hold();
      setResult("");
      setSaved("Facts saved");
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="pt-screen" data-gate={gate} data-area="govern" data-testid="provider-terms-screen" aria-label="Provider terms">
      <style>{SCREEN_CSS}</style>
      <header className="pt-head">
        <div>
          <span className="pt-kicker">Before the prompt</span>
          <h1>Three facts</h1>
        </div>
        <div className="pt-hold" title="Run waits on three facts">
          <span className="pt-lamp" aria-hidden="true" />
          <span>{gate === "clear" ? "Clear" : "Held"}</span>
        </div>
      </header>
      <p className="pt-note" data-testid="provider-terms-note">
        The prompt stays put until region, log retention, and may train are on this screen. A missing fact reads unknown. Nothing is guessed.
      </p>
      <div className="pt-compose">
        <label className="pt-field">
          <span>Provider</span>
          <input
            data-testid="provider-terms-provider"
            aria-label="Provider"
            value={provider}
            autoComplete="off"
            onChange={(event) => editProvider(event.target.value)}
          />
        </label>
        <label className="pt-field pt-field-prompt">
          <span>Prompt</span>
          <textarea
            data-testid="provider-terms-prompt"
            aria-label="Prompt"
            rows={3}
            value={prompt}
            onChange={(event) => editPrompt(event.target.value)}
          />
        </label>
      </div>
      <div className="pt-actions">
        <button type="button" data-testid="provider-terms-show" disabled={!canShow} title="Read the three facts" onClick={() => void showTerms()}>
          Show terms
        </button>
        <GatedButton
          permission="model_provider:manage"
          type="button"
          className="pt-run"
          data-testid="provider-terms-run"
          disabled={gate !== "clear" || busy !== ""}
          title={gate === "clear" ? "Send this prompt" : "Show the three facts first"}
          onClick={() => void runPrompt()}
        >
          Run prompt
        </GatedButton>
      </div>
      <div className="pt-meters" aria-live="polite">
        {METERS.map((meter) => (
          <Meter key={meter.key} testId={meter.testId} label={meter.label} value={facts[meter.key]} />
        ))}
      </div>
      {result ? <p className="pt-result" data-testid="provider-terms-result">{result}</p> : null}
      {error ? <p className="pt-error" role="alert" data-testid="provider-terms-error">{error}</p> : null}
      {saved ? <p className="pt-saved" data-testid="provider-terms-saved">{saved}</p> : null}
      <form className="pt-known" onSubmit={(event) => { event.preventDefault(); void saveKnown(); }}>
        <h2>Known facts</h2>
        <p>Leave a blank. A blank is not a guess.</p>
        <div className="pt-known-grid">
          <label>
            <span>Region</span>
            <input data-testid="provider-terms-known-region" aria-label="Recorded region" value={knownRegion} autoComplete="off" onChange={(event) => setKnownRegion(event.target.value)} />
          </label>
          <label>
            <span>Log retention</span>
            <input data-testid="provider-terms-known-retention" aria-label="Recorded retention" value={knownRetention} autoComplete="off" onChange={(event) => setKnownRetention(event.target.value)} />
          </label>
          <label>
            <span>May train</span>
            <input data-testid="provider-terms-known-train" aria-label="Recorded training" value={knownTrain} autoComplete="off" onChange={(event) => setKnownTrain(event.target.value)} />
          </label>
        </div>
        <button type="button" data-testid="provider-terms-save" disabled={busy !== ""} title="Store only typed facts" onClick={() => void saveKnown()}>
          Save known facts
        </button>
      </form>
    </section>
  );
}

const SCREEN_CSS = `
@import url('https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600&family=IBM+Plex+Mono:wght@500&family=Syne:wght@700&display=swap');

.pt-screen {
  --pt-face: #e8eef2;
  --pt-ink: #16323a;
  --pt-copper: #3e7c76;
  --pt-lamp: #e3a008;
  --pt-window: #12343b;
  --pt-rule: #c5d2d8;
  --pt-paper: #f7fafb;
  box-sizing: border-box;
  max-width: 880px;
  margin: 0;
  padding: 28px 28px 32px;
  background: var(--pt-face);
  color: var(--pt-ink);
  font-family: "Figtree", "Avenir Next", "Segoe UI", sans-serif;
  border: 1px solid var(--pt-rule);
  border-radius: 18px;
}
.pt-screen *, .pt-screen *::before, .pt-screen *::after { box-sizing: border-box; }
.pt-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; }
.pt-kicker {
  display: block;
  font-family: "Syne", "Avenir Next", sans-serif;
  font-size: 13px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--pt-copper);
}
.pt-screen h1 {
  margin: 4px 0 0;
  font-family: "Syne", "Avenir Next", sans-serif;
  font-size: 44px;
  line-height: 0.92;
  letter-spacing: -0.045em;
  font-weight: 700;
}
.pt-hold {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: "Syne", "Avenir Next", sans-serif;
  font-size: 14px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.pt-lamp {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--pt-lamp);
  box-shadow: 0 0 0 5px rgba(227, 160, 8, 0.22);
}
.pt-screen[data-gate="clear"] .pt-lamp {
  background: var(--pt-copper);
  box-shadow: 0 0 0 5px rgba(62, 124, 118, 0.22);
}
.pt-note, .pt-known p, .pt-error, .pt-result, .pt-saved {
  max-width: 62ch;
  font-size: 16px;
  line-height: 1.45;
}
.pt-note { margin: 18px 0 0; }
.pt-compose, .pt-known-grid {
  display: grid;
  grid-template-columns: 1fr 1.4fr;
  gap: 12px;
  margin-top: 18px;
}
.pt-field, .pt-known label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
.pt-field-prompt { grid-column: 1 / -1; }
.pt-screen input, .pt-screen textarea {
  width: 100%;
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--pt-rule);
  border-radius: 10px;
  background: var(--pt-paper);
  color: var(--pt-ink);
  font: inherit;
}
.pt-screen textarea { resize: vertical; min-height: 76px; }
.pt-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.pt-screen button {
  font-family: "Syne", "Avenir Next", sans-serif;
  font-size: 15px;
  border: 0;
  border-radius: 999px;
  padding: 12px 18px;
  background: var(--pt-ink);
  color: var(--pt-face);
  cursor: pointer;
}
.pt-screen button:disabled {
  background: transparent;
  color: #6d8186;
  border: 1px dashed #8aa0a6;
  cursor: not-allowed;
}
.pt-run { font-weight: 700; }
.pt-meters {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-top: 18px;
}
.pt-meter {
  min-height: 118px;
  padding: 14px 16px;
  border-radius: 14px;
  background: var(--pt-window);
  color: #e8eef2;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 12px;
}
.pt-meter-label {
  font-size: 12px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: #9fb4b8;
}
.pt-value {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 18px;
  line-height: 1.3;
  overflow-wrap: anywhere;
}
.pt-meter[data-state="unknown"] .pt-value { color: #f2c14e; }
.pt-meter[data-state="empty"] .pt-value { color: #8aa0a6; }
.pt-result { color: var(--pt-copper); font-weight: 600; }
.pt-error { color: #9a3412; }
.pt-known {
  margin-top: 26px;
  padding-top: 18px;
  border-top: 1px solid var(--pt-rule);
}
.pt-known h2 {
  margin: 0;
  font-family: "Syne", "Avenir Next", sans-serif;
  font-size: 22px;
  letter-spacing: -0.03em;
}
.pt-known-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.pt-screen :focus-visible { outline: 2px solid var(--pt-copper); outline-offset: 3px; }
@media (max-width: 720px) {
  .pt-screen { padding: 20px 16px 24px; }
  .pt-screen h1 { font-size: 36px; }
  .pt-compose, .pt-meters, .pt-known-grid { grid-template-columns: 1fr; }
  .pt-head { align-items: flex-start; }
}
@media (prefers-reduced-motion: reduce) {
  .pt-lamp { box-shadow: none; }
}
`;
