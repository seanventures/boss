"use client";

/**
 * Prompt bench (BOSSAPP-1261). One prompt, one model, the output, and the
 * receipt. Comparison columns belong to 1262 and are not here.
 *
 * Aesthetic: a harbor timing bench. Sea-ink on mist, Literata for the words,
 * Syne for the name, and one perforated ticket for the receipt.
 */
import React, { useState } from "react";
import { apiWrite } from "../../lib/api-write";

export type PlaygroundResult = {
  prompt: string;
  model: string;
  output: string;
  receipt_id: string;
};

export type PlaygroundRequest = {
  projectId: string;
  prompt: string;
  model: string;
  actor?: string;
};

const CSS = `
.pb1261 {
  --ink: #14343A;
  --mist: #E7F2EC;
  --glass: #F7FBFA;
  --line: #8AA8A0;
  --run: #0E6B5C;
  --run-ink: #F3FBF7;
  --ticket: #F4E1C1;
  --seal: #8C3A2F;
  --quiet: #4E6A66;
  background: var(--mist);
  color: var(--ink);
  font-family: "Literata", "Iowan Old Style", Palatino, "Palatino Linotype", serif;
  max-width: 40rem;
  margin: 0 auto;
  padding: 1.75rem 1.25rem 2.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.35rem;
  box-sizing: border-box;
}
.pb1261 *, .pb1261 *::before, .pb1261 *::after { box-sizing: border-box; }
.pb1261 h1 {
  font-family: Syne, "Avenir Next", "Segoe UI", sans-serif;
  font-weight: 700;
  font-size: clamp(2.1rem, 6vw, 3rem);
  letter-spacing: -0.045em;
  line-height: 0.92;
  margin: 0.15rem 0 0;
}
.pb1261 .eyebrow, .pb1261 .kicker {
  font-family: Syne, "Avenir Next", sans-serif;
  font-size: 0.72rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--quiet);
  margin: 0;
}
.pb1261 .lede { margin: 0.4rem 0 0; font-size: 1.05rem; }
.pb1261 form { display: flex; flex-direction: column; gap: 0.9rem; }
.pb1261 label {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  font-family: Syne, "Avenir Next", sans-serif;
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--quiet);
}
.pb1261 input, .pb1261 textarea {
  font-family: Inconsolata, ui-monospace, monospace;
  font-size: 1.05rem;
  letter-spacing: 0.01em;
  color: var(--ink);
  background: var(--glass);
  border: 1px solid var(--line);
  border-radius: 2px;
  padding: 0.7rem 0.75rem;
  width: 100%;
}
.pb1261 textarea {
  font-family: "Literata", Palatino, serif;
  font-size: 1.12rem;
  line-height: 1.45;
  min-height: 9rem;
  resize: vertical;
}
.pb1261 button {
  font-family: Syne, "Avenir Next", sans-serif;
  font-weight: 700;
  letter-spacing: 0.04em;
  background: var(--run);
  color: var(--run-ink);
  border: 0;
  border-radius: 999px;
  padding: 0.75rem 1.3rem;
  align-self: flex-start;
  cursor: pointer;
}
.pb1261 button:disabled { opacity: 0.45; cursor: not-allowed; }
.pb1261 :focus-visible {
  outline: 2px solid var(--run);
  outline-offset: 3px;
}
.pb1261 .sheet {
  background: transparent;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.pb1261 .output {
  margin: 0;
  min-height: 3.2rem;
  font-size: 1.2rem;
  line-height: 1.45;
  white-space: pre-wrap;
}
.pb1261 .ticket {
  background: var(--ticket);
  border-top: 2px dashed var(--seal);
  padding: 0.85rem 1rem 1rem;
  border-radius: 0 0 16px 16px;
  animation: pb1261-rise 280ms ease-out;
}
.pb1261 .ticket .kicker { color: var(--seal); }
.pb1261 .receipt {
  margin: 0.2rem 0 0;
  font-family: Inconsolata, ui-monospace, monospace;
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: 0.03em;
  color: var(--seal);
  word-break: break-all;
}
.pb1261 .error {
  margin: 0;
  color: var(--seal);
  font-size: 1.02rem;
}
@keyframes pb1261-rise {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .pb1261 .ticket { animation: none; }
}
@media (max-width: 640px) {
  .pb1261 { padding: 1.25rem 1rem 2rem; }
  .pb1261 button { align-self: stretch; }
}
`;

export async function runPlayground(input: PlaygroundRequest): Promise<PlaygroundResult> {
  const response = await apiWrite("/v1/playground/runs", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      project_id: input.projectId,
      prompt: input.prompt,
      model: input.model,
      actor: input.actor ?? "operator",
    }),
  });
  let body: { detail?: unknown; output?: unknown; receipt_id?: unknown; prompt?: unknown; model?: unknown } = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "The run failed.";
    throw new Error(detail);
  }
  if (typeof body.output !== "string" || typeof body.receipt_id !== "string") {
    throw new Error("The run came back without an output.");
  }
  return {
    prompt: typeof body.prompt === "string" ? body.prompt : input.prompt,
    model: typeof body.model === "string" ? body.model : input.model,
    output: body.output,
    receipt_id: body.receipt_id,
  };
}

export function PlaygroundScreen({
  projectId,
  model: initialModel = "",
  actor = "operator",
  run = runPlayground,
}: {
  projectId: string;
  model?: string;
  actor?: string;
  run?: (input: PlaygroundRequest) => Promise<PlaygroundResult>;
}) {
  const [model, setModel] = useState(initialModel);
  const [prompt, setPrompt] = useState("");
  const [output, setOutput] = useState("");
  const [receiptId, setReceiptId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const ready = Boolean(projectId.trim() && prompt.trim() && model.trim()) && !busy;

  async function submit() {
    if (!projectId.trim()) {
      setError("Open a project first.");
      return;
    }
    if (!prompt.trim() || !model.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await run({
        projectId: projectId.trim(),
        prompt: prompt.trim(),
        model: model.trim(),
        actor,
      });
      setOutput(result.output);
      setReceiptId(result.receipt_id);
    } catch (caught) {
      setOutput("");
      setReceiptId("");
      setError(caught instanceof Error ? caught.message : "The run failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="pb1261"
      data-testid="playground-screen"
      data-layout="stack"
      aria-busy={busy}
    >
      <style>{CSS}</style>
      <header>
        <p className="eyebrow">One model</p>
        <h1>Prompt bench</h1>
        <p className="lede">One model answers.</p>
      </header>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label>
          Model
          <input
            data-testid="playground-model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            maxLength={200}
          />
        </label>
        <label>
          Prompt
          <textarea
            data-testid="playground-prompt"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="Write the prompt"
            maxLength={8000}
          />
        </label>
        <button
          type="submit"
          data-testid="playground-run"
          disabled={!ready}
          title="Runs this prompt once."
        >
          {busy ? "Running" : "Run prompt"}
        </button>
      </form>
      {error ? (
        <p className="error" data-testid="playground-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="sheet">
        <p className="kicker">Output</p>
        <p className="output" data-testid="playground-output" aria-live="polite">
          {output || "No output yet."}
        </p>
        {receiptId ? (
          <div className="ticket">
            <p className="kicker">Receipt</p>
            <p className="receipt" data-testid="playground-receipt">
              {receiptId}
            </p>
          </div>
        ) : null}
      </div>
    </section>
  );
}
