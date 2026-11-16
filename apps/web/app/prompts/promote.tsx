"use client";

/**
 * Prompt desk (BOSSAPP-1263). A prompt stays words until a decision approves
 * hiring it. This screen never calls POST /v1/agents.
 *
 * Tracing table: cool glass, indigo ink, one registration cross, one stamp.
 * Literata for the words, Outfit for the controls.
 */
import React, { useEffect, useState } from "react";

const CSS = `
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500&family=Literata:opsz,wght@7..72,500;7..72,650&family=Outfit:wght@400;500;700&display=swap');
.pp {
  --pp-glass: #D7E7EA;
  --pp-ink: #1B2A4A;
  --pp-steel: #5E7C8A;
  --pp-mark: #C4372A;
  --pp-slip: #F4FAFB;
  --pp-born: #1F6B4A;
  --pp-display: "Literata", "Iowan Old Style", Palatino, serif;
  --pp-ui: "Outfit", "Avenir Next", "Segoe UI", sans-serif;
  --pp-mono: "IBM Plex Mono", ui-monospace, monospace;
  background: var(--pp-glass);
  color: var(--pp-ink);
  min-height: 100%;
  padding: 32px 22px 56px;
  font-family: var(--pp-ui);
}
.pp-desk { max-width: 720px; margin: 0 auto; }
.pp-eyebrow {
  font-family: var(--pp-mono);
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-size: 12px;
  color: var(--pp-mark);
  margin: 0 0 8px;
}
.pp h1 {
  font-family: var(--pp-display);
  font-weight: 500;
  font-size: 3rem;
  line-height: 0.95;
  letter-spacing: -0.03em;
  margin: 0 0 12px;
}
.pp-lead { margin: 0 0 8px; max-width: 38rem; }
.pp-composer, .pp-slip { display: flex; background: var(--pp-slip); margin-top: 18px; }
.pp-gutter {
  width: 42px;
  flex: none;
  background: var(--pp-ink);
  display: flex;
  justify-content: center;
  padding-top: 16px;
}
.pp-cross { width: 14px; height: 14px; position: relative; }
.pp-cross::before, .pp-cross::after {
  content: "";
  position: absolute;
  background: var(--pp-mark);
}
.pp-cross::before { left: 6px; top: 0; width: 2px; height: 14px; }
.pp-cross::after { top: 6px; left: 0; height: 2px; width: 14px; }
.pp-sheet { flex: 1; padding: 16px 18px 18px; min-width: 0; }
.pp label { display: block; font-size: 13px; color: var(--pp-steel); margin-top: 12px; }
.pp input, .pp textarea {
  font-family: var(--pp-display);
  font-size: 1.05rem;
  width: 100%;
  box-sizing: border-box;
  border: 0;
  border-bottom: 2px solid var(--pp-ink);
  background: transparent;
  color: var(--pp-ink);
  padding: 8px 0;
}
.pp textarea { min-height: 120px; resize: vertical; }
.pp input:focus-visible, .pp textarea:focus-visible, .pp button:focus-visible {
  outline: 2px solid var(--pp-mark);
  outline-offset: 3px;
}
.pp-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.pp button {
  font-family: var(--pp-ui);
  font-weight: 500;
  background: var(--pp-ink);
  color: var(--pp-slip);
  border: 0;
  padding: 10px 14px;
  cursor: pointer;
}
.pp button:disabled { opacity: 0.45; cursor: not-allowed; }
.pp-promote { background: var(--pp-mark); }
.pp-note { margin: 10px 0 0; color: var(--pp-steel); }
.pp-slip-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.pp-card-title {
  font-family: var(--pp-display);
  font-size: 1.45rem;
  line-height: 1.15;
  margin: 0;
}
.pp-body { font-family: var(--pp-display); white-space: pre-wrap; margin: 10px 0; }
.pp-stamp {
  font-family: var(--pp-mono);
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  border: 2px solid currentColor;
  color: var(--pp-mark);
  padding: 3px 7px;
  transform: rotate(-8deg);
  flex: none;
}
.pp-stamp[data-state="agent"] { color: var(--pp-born); }
.pp-error { color: var(--pp-mark); margin: 8px 0 0; }
.pp-born { color: var(--pp-born); font-weight: 500; }
@media (max-width: 640px) {
  .pp { padding: 16px 12px 40px; }
  .pp h1 { font-size: 2.2rem; }
  .pp-actions button { flex: 1 1 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .pp-stamp { transform: none; }
}
`;

export type PromptRow = {
  id: string;
  title: string;
  body: string;
  decision_id?: string | null;
  decision_status?: string | null;
  role_key?: string | null;
  agent_created?: boolean;
  agent_name?: string | null;
};

type Answer = { status: number; data: Record<string, unknown> };

async function send(url: string, body: unknown): Promise<Answer> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({})) as Record<string, unknown>;
  return { status: response.status, data };
}

function detailOf(data: Record<string, unknown>, fallback: string): string {
  return typeof data.detail === "string" && data.detail.trim() ? data.detail : fallback;
}

function stampFor(prompt: PromptRow): { state: string; label: string } {
  if (prompt.role_key) return { state: "agent", label: "Agent" };
  if (prompt.decision_status === "approved") return { state: "approved", label: "Approved" };
  if (prompt.decision_id) return { state: "asked", label: "Asked" };
  return { state: "saved", label: "Saved" };
}

export function PromptPromote({ projectId, actor }: { projectId: string; actor: string }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [prompts, setPrompts] = useState<PromptRow[]>([]);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [cardError, setCardError] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");

  useEffect(() => {
    let cancel = false;
    (async () => {
      try {
        const response = await fetch(`/v1/prompts?project_id=${encodeURIComponent(projectId)}`);
        const data = await response.json().catch(() => ({})) as { prompts?: PromptRow[]; detail?: string };
        if (!response.ok) throw new Error(data.detail || "Could not load prompts.");
        if (!cancel) setPrompts(data.prompts ?? []);
      } catch (error) {
        if (!cancel) setLoadError(error instanceof Error ? error.message : "Could not load prompts.");
      }
    })();
    return () => { cancel = true; };
  }, [projectId]);

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    setSaveError("");
    setBusy("save");
    try {
      const answer = await send("/v1/prompts", { project_id: projectId, actor, title, body });
      if (answer.status >= 400) {
        setSaveError(detailOf(answer.data, "Could not save the prompt."));
        return;
      }
      const saved = answer.data as unknown as PromptRow;
      setPrompts((current) => current.some((row) => row.id === saved.id) ? current : [saved, ...current]);
      setTitle("");
      setBody("");
    } catch {
      setSaveError("Could not save the prompt.");
    } finally {
      setBusy("");
    }
  }

  async function onAsk(prompt: PromptRow) {
    setCardError((current) => ({ ...current, [prompt.id]: "" }));
    setBusy(prompt.id + ":ask");
    try {
      const answer = await send(`/v1/prompts/${prompt.id}/decision`, { project_id: projectId, actor });
      if (answer.status >= 400) {
        setCardError((current) => ({ ...current, [prompt.id]: detailOf(answer.data, "Could not open a decision.") }));
        return;
      }
      setPrompts((current) => current.map((row) => row.id === prompt.id ? {
        ...row,
        decision_id: String(answer.data.decision_id ?? row.decision_id ?? ""),
        decision_status: String(answer.data.decision_status ?? "pending"),
      } : row));
    } catch {
      setCardError((current) => ({ ...current, [prompt.id]: "Could not open a decision." }));
    } finally {
      setBusy("");
    }
  }

  async function onPromote(prompt: PromptRow) {
    setCardError((current) => ({ ...current, [prompt.id]: "" }));
    setBusy(prompt.id + ":promote");
    try {
      const answer = await send(`/v1/prompts/${prompt.id}/promote`, { project_id: projectId, actor });
      if (answer.status >= 400) {
        setCardError((current) => ({
          ...current,
          [prompt.id]: detailOf(answer.data, "Promotion was refused."),
        }));
        return;
      }
      const agent = (answer.data.agent ?? {}) as { name?: string };
      setPrompts((current) => current.map((row) => row.id === prompt.id ? {
        ...row,
        role_key: String(answer.data.role_key ?? ""),
        agent_created: true,
        agent_name: agent.name || row.title,
        decision_status: "approved",
      } : row));
    } catch {
      setCardError((current) => ({ ...current, [prompt.id]: "Promotion was refused." }));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="pp" data-testid="prompt-promote">
      <style>{CSS}</style>
      <div className="pp-desk">
        <p className="pp-eyebrow">Prompt desk</p>
        <h1>Words first.</h1>
        <p className="pp-lead">A saved prompt stays a prompt.</p>
        <p className="pp-lead">An agent exists only after a decision is approved.</p>
        <form className="pp-composer" onSubmit={onSave}>
          <div className="pp-gutter" aria-hidden="true"><span className="pp-cross" /></div>
          <div className="pp-sheet">
            <label htmlFor="prompt-title">Title</label>
            <input id="prompt-title" value={title} onChange={(event) => setTitle(event.target.value)} />
            <label htmlFor="prompt-body">Prompt</label>
            <textarea id="prompt-body" value={body} onChange={(event) => setBody(event.target.value)} />
            <div className="pp-actions">
              <button type="submit" data-testid="save-prompt" title="Stores the words only" disabled={busy === "save"}>
                Save prompt
              </button>
            </div>
            <p className="pp-note">Saving does not create an agent.</p>
            {saveError ? <p className="pp-error" data-testid="save-error">{saveError}</p> : null}
          </div>
        </form>
        {loadError ? <p className="pp-error">{loadError}</p> : null}
        {prompts.length === 0 ? <p className="pp-note">Nothing is saved yet.</p> : null}
        {prompts.map((prompt) => {
          const stamp = stampFor(prompt);
          const born = Boolean(prompt.role_key);
          return (
            <article className="pp-slip" data-testid="prompt-card" key={prompt.id}>
              <div className="pp-gutter" aria-hidden="true"><span className="pp-cross" /></div>
              <div className="pp-sheet">
                <div className="pp-slip-head">
                  <p className="pp-card-title">{prompt.title}</p>
                  <span className="pp-stamp" data-state={stamp.state} data-testid="prompt-stamp">{stamp.label}</span>
                </div>
                <p className="pp-body">{prompt.body}</p>
                {born ? <p className="pp-born" data-testid="agent-born">Born as {prompt.agent_name || prompt.title}.</p> : (
                  <p className="pp-note">{prompt.decision_status === "approved" ? "Decision approved." : prompt.decision_id ? "Decision pending." : "No decision yet."}</p>
                )}
                {born ? null : (
                  <div className="pp-actions">
                    {prompt.decision_id ? null : (
                      <button type="button" data-testid="ask-decision" title="Opens a decision. Creates nothing."
                        disabled={Boolean(busy)} onClick={() => onAsk(prompt)}>
                        Ask for a decision
                      </button>
                    )}
                    <button type="button" className="pp-promote" data-testid="promote-prompt"
                      title="Needs an approved decision" disabled={Boolean(busy)} onClick={() => onPromote(prompt)}>
                      Promote
                    </button>
                  </div>
                )}
                {cardError[prompt.id] ? <p className="pp-error" data-testid="promote-error">{cardError[prompt.id]}</p> : null}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
