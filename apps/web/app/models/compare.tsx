"use client";

/**
 * Model bench (BOSSAPP-1262). One prompt, one lane per model, a receipt under each.
 * Spend is the API's job. This screen only shows what came back.
 *
 * Bench, not a card grid: oxidized aluminum, a copper punch, a teal index line.
 * Display Syne, answers in Source Serif 4, stubs in Spline Sans Mono.
 */
import React, { useState } from "react";
import { apiWrite } from "../../lib/api-write";
import { GatedButton } from "../../components/gated";

export type CompareColumn = {
  model: string;
  status: "succeeded" | "failed" | "in_progress";
  text: string | null;
  error: string | null;
  receipt_id: string | null;
  dedupe_key: string;
  usd_micros: number | null;
  reused: boolean;
};

export type CompareResult = {
  prompt: string;
  columns: CompareColumn[];
};

export type CompareRequest = {
  projectId: string;
  actor?: string;
  prompt: string;
  models: string[];
};

const MAX_LANES = 4;

async function postCompare(input: CompareRequest): Promise<CompareResult> {
  const response = await apiWrite("/v1/model-compares", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      project_id: input.projectId,
      prompt: input.prompt,
      models: input.models,
    }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : "Compare failed.";
    throw new Error(detail);
  }
  return response.json() as Promise<CompareResult>;
}

export function ModelCompare({
  projectId,
  actor,
  compare = postCompare,
}: {
  projectId: string;
  actor?: string;
  compare?: (input: CompareRequest) => Promise<CompareResult>;
}) {
  const [prompt, setPrompt] = useState("");
  const [models, setModels] = useState<string[]>(["", ""]);
  const [formError, setFormError] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CompareResult | null>(null);

  function setModel(index: number, value: string) {
    setModels((current) => current.map((item, at) => (at === index ? value : item)));
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const named = models.map((model) => model.trim()).filter(Boolean);
    if (!prompt.trim()) {
      setFormError("Write a prompt.");
      return;
    }
    if (named.length < 2) {
      setFormError("Name two models.");
      return;
    }
    setFormError(null);
    setRunError(null);
    setBusy(true);
    try {
      const next = await compare({
        projectId,
        actor,
        prompt: prompt.trim(),
        models: named,
      });
      setResult(next);
    } catch (error) {
      setRunError(error instanceof Error ? error.message : "Compare failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mc" aria-labelledby="mc-title">
      <style>{CSS}</style>
      <p className="mc-eyebrow">Model bench</p>
      <h1 id="mc-title" className="mc-title">One prompt. Several answers.</h1>
      <p className="mc-intro" data-testid="compare-intro">
        Each lane spends once through the tenant gateway, and a failed lane leaves the others standing.
      </p>
      <form className="mc-plate" onSubmit={onSubmit}>
        <label className="mc-label" htmlFor="mc-prompt">Prompt</label>
        <textarea
          id="mc-prompt"
          className="mc-prompt"
          data-testid="compare-prompt"
          rows={4}
          value={prompt}
          maxLength={8000}
          onChange={(event) => setPrompt(event.target.value)}
        />
        <div className="mc-names">
          {models.map((model, index) => (
            <label key={index} className="mc-name">
              <span>Lane {index + 1}</span>
              <input
                data-testid={`compare-model-${index}`}
                value={model}
                maxLength={80}
                spellCheck={false}
                onChange={(event) => setModel(index, event.target.value)}
              />
            </label>
          ))}
        </div>
        <div className="mc-actions">
          {models.length < MAX_LANES ? (
            <button
              type="button"
              className="mc-add"
              onClick={() => setModels((current) => [...current, ""])}
            >
              Add a lane
            </button>
          ) : null}
          <GatedButton
            permission="model_provider:manage"
            type="submit"
            className="mc-go"
            data-testid="compare-submit"
            title="Spends once per model."
            disabled={busy}
          >
            {busy ? "Comparing" : "Compare"}
          </GatedButton>
        </div>
        {formError ? <p className="mc-error" data-testid="compare-form-error">{formError}</p> : null}
        {runError ? <p className="mc-error" data-testid="compare-run-error">{runError}</p> : null}
      </form>
      {result ? (
        <div
          className="mc-lanes"
          data-testid="compare-lanes"
          style={{ display: "grid", gridTemplateColumns: `repeat(${result.columns.length}, minmax(0, 1fr))` }}
        >
          {result.columns.map((column) => (
            <article
              key={column.dedupe_key || column.model}
              className="mc-lane"
              data-testid="compare-column"
              data-model={column.model}
              data-status={column.status}
            >
              <header className="mc-lane-head">
                <h2>{column.model}</h2>
                <span className="mc-status">{statusWord(column.status)}</span>
              </header>
              {column.status === "succeeded" ? <p className="mc-answer">{column.text}</p> : null}
              {column.error ? <p className="mc-fail">{column.error}</p> : null}
              <footer className="mc-stub" data-testid="compare-receipt" title="This lane's spend receipt.">
                <span className="mc-punch" aria-hidden="true" />
                <span className="mc-stub-k">Receipt</span>
                <code>{column.receipt_id || "none"}</code>
                {column.usd_micros != null ? <span className="mc-micros">{column.usd_micros}</span> : null}
                {column.reused ? <span className="mc-reused">Same receipt</span> : null}
              </footer>
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function statusWord(status: CompareColumn["status"]): string {
  if (status === "succeeded") return "Stood";
  if (status === "failed") return "Failed";
  return "Running";
}

const CSS = `
.mc {
  --mc-bench: #c5d0d2;
  --mc-ink: #142022;
  --mc-lane: #f3f6f5;
  --mc-filament: #0b6b66;
  --mc-copper: #b8613a;
  --mc-fail: #8c2e2a;
  --mc-rule: #8aa0a4;
  color: var(--mc-ink);
  background: var(--mc-bench);
  font-family: "Source Serif 4", "Iowan Old Style", Palatino, serif;
  padding: 28px 22px 36px;
  min-width: 0;
}
.mc-eyebrow {
  margin: 0 0 6px;
  font-family: "Syne", "Avenir Next Condensed", sans-serif;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--mc-filament);
}
.mc-title {
  margin: 0 0 10px;
  font-family: "Syne", "Avenir Next Condensed", sans-serif;
  font-weight: 750;
  font-size: 2.1rem;
  line-height: 0.95;
  letter-spacing: -0.03em;
}
.mc-intro { margin: 0 0 18px; max-width: 38rem; font-size: 1.05rem; line-height: 1.45; }
.mc-plate {
  background: var(--mc-lane);
  border-top: 4px solid var(--mc-filament);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.mc-label, .mc-name span, .mc-stub-k {
  font-family: "Syne", "Avenir Next Condensed", sans-serif;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.mc-prompt, .mc-name input {
  width: 100%;
  box-sizing: border-box;
  border: 0;
  border-bottom: 1px solid var(--mc-rule);
  background: transparent;
  color: inherit;
  font: inherit;
  padding: 8px 0;
}
.mc-names { display: flex; flex-wrap: wrap; gap: 12px 18px; }
.mc-name { flex: 1 1 140px; display: flex; flex-direction: column; gap: 4px; }
.mc-actions { display: flex; gap: 10px; align-items: center; }
.mc-add, .mc-go {
  font-family: "Syne", "Avenir Next Condensed", sans-serif;
  border: 0;
  cursor: pointer;
  min-height: 40px;
  padding: 0 14px;
}
.mc-add { background: transparent; color: var(--mc-ink); text-decoration: underline; text-underline-offset: 3px; }
.mc-go { background: var(--mc-copper); color: #f3f6f5; font-weight: 700; }
.mc-go:disabled { opacity: 0.6; cursor: progress; }
.mc-error { margin: 0; color: var(--mc-fail); }
.mc-lanes { gap: 0; margin-top: 18px; border-top: 1px solid var(--mc-ink); }
.mc-lane {
  min-width: 0;
  background: var(--mc-lane);
  border-right: 1px solid var(--mc-rule);
  display: flex;
  flex-direction: column;
  min-height: 220px;
}
.mc-lane:last-child { border-right: 0; }
.mc-lane-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: baseline;
  padding: 12px 14px 0;
}
.mc-lane-head h2 {
  margin: 0;
  font-family: "Syne", "Avenir Next Condensed", sans-serif;
  font-size: 1.15rem;
}
.mc-status { font-family: "Spline Sans Mono", ui-monospace, monospace; font-size: 0.72rem; color: var(--mc-filament); }
.mc-lane[data-status="failed"] .mc-status { color: var(--mc-fail); }
.mc-answer, .mc-fail { margin: 12px 14px; line-height: 1.45; }
.mc-fail { color: var(--mc-fail); }
.mc-stub {
  margin-top: auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px 14px 14px;
  border-top: 1px dashed var(--mc-copper);
  background:
    radial-gradient(circle at 16px 50%, transparent 7px, #e7eeed 8px) left center / 28px 100% no-repeat,
    #e7eeed;
  font-family: "Spline Sans Mono", ui-monospace, monospace;
  font-size: 0.75rem;
}
.mc-punch {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid var(--mc-copper);
  box-sizing: border-box;
}
.mc-micros { margin-left: auto; }
.mc button:focus-visible, .mc textarea:focus-visible, .mc input:focus-visible {
  outline: 2px solid var(--mc-filament);
  outline-offset: 2px;
}
@media (max-width: 720px) {
  .mc-lanes { grid-template-columns: 1fr !important; }
  .mc-lane { border-right: 0; border-bottom: 1px solid var(--mc-rule); }
}
@media (prefers-reduced-motion: reduce) {
  .mc-go { transition: none; }
}
`;
