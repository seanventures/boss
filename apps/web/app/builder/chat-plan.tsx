"use client";

/**
 * Builder thread (BOSSAPP-1244). Chat and Plan never write code.
 * Build stays shut until the person approves the plan.
 */
import React, { useEffect, useState } from "react";
import { apiWrite } from "../../lib/api-write";

type Turn = { mode: string; text: string; reply: string; wrote_code: boolean };
type Plan = { id: string; steps: string[]; writes_code: boolean };
type Approval = { id: string; plan_id: string; person_id: string };

type ThreadBody = {
  thread_id?: string;
  mode?: string;
  wrote_code?: boolean;
  deduped?: boolean;
  code_files?: string[];
  plan?: Plan | null;
  approval?: Approval | null;
  turns?: Turn[];
  reply?: string;
  message?: string;
  error?: string;
  approved?: boolean;
};

type Props = { projectId?: string };

const CSS = `
.bcp {
  --sheet: #e7eef3;
  --ink: #142033;
  --brass: #c6a15a;
  --brass-deep: #6e4e16;
  --torch: #9d3416;
  --pencil: #1f4e6d;
  --rule: #c5d0d8;
  --bed: #f6f8f9;
  --quiet: #4e5b66;
  box-sizing: border-box;
  min-height: 100%;
  padding: 1.4rem 1.5rem 2rem;
  background: var(--sheet);
  color: var(--ink);
  font-family: "Literata", "Iowan Old Style", Palatino, serif;
}
.bcp *, .bcp *::before, .bcp *::after { box-sizing: border-box; }
.bcp-eyebrow, .bcp-rail, .bcp-mark, .bcp button {
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}
.bcp-eyebrow {
  margin: 0 0 0.2rem;
  color: var(--brass-deep);
  font-size: 0.95rem;
  font-weight: 700;
}
.bcp h1 {
  margin: 0 0 0.6rem;
  font-size: clamp(2rem, 4vw, 3.1rem);
  font-weight: 650;
  letter-spacing: -0.03em;
  line-height: 0.95;
}
.bcp-lead { max-width: 38rem; margin: 0 0 0.45rem; line-height: 1.45; }
.bcp-bench {
  display: grid;
  grid-template-columns: 7.2rem minmax(0, 1fr) minmax(12rem, 17rem);
  margin-top: 1.1rem;
  border: 1px solid var(--rule);
  background: var(--bed);
  min-height: 28rem;
}
.bcp-rail {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  padding: 0.75rem 0.45rem;
  border-right: 1px solid var(--rule);
  background: #dce6ee;
}
.bcp-rail button, .bcp-send, .bcp-approve, .bcp-retry {
  border: 1px solid var(--ink);
  background: transparent;
  color: var(--ink);
  border-radius: 2px;
  padding: 0.35rem 0.4rem;
  font-size: 1rem;
  font-weight: 600;
  cursor: pointer;
}
.bcp-rail button[aria-pressed="true"] { background: var(--ink); color: var(--bed); }
.bcp-gate {
  position: relative;
  height: 14px;
  margin: 0.55rem 0.15rem;
  background: repeating-linear-gradient(90deg, var(--brass) 0 7px, #8d6a2f 7px 11px);
}
.bcp-latch {
  position: absolute;
  top: -5px;
  left: 12%;
  width: 18px;
  height: 24px;
  background: var(--torch);
  box-shadow: inset 0 0 0 2px #5c1d0c;
  transition: left 180ms ease;
}
.bcp-gate[data-open="yes"] .bcp-latch { left: 68%; }
.bcp-build { margin-top: 0.15rem; }
.bcp-thread { padding: 0.9rem 1rem 1rem; min-width: 0; }
.bcp-proof { list-style: none; margin: 0; padding: 0; }
.bcp-proof li {
  display: grid;
  grid-template-columns: 4.6rem minmax(0, 1fr);
  gap: 0.6rem;
  padding: 0.55rem 0;
  border-bottom: 1px solid var(--rule);
}
.bcp-mark { color: var(--pencil); font-size: 1.05rem; padding-top: 0.15rem; }
.bcp-proof p, .bcp-plan p, .bcp-note { margin: 0; line-height: 1.45; }
.bcp-plan {
  margin: 0.8rem 0 0;
  padding-top: 0.7rem;
  border-top: 1px solid var(--rule);
}
.bcp-plan ol { margin: 0.35rem 0 0; padding-left: 1.1rem; }
.bcp-compose { display: grid; gap: 0.45rem; margin-top: 0.9rem; }
.bcp textarea {
  width: 100%;
  min-height: 4.6rem;
  resize: vertical;
  padding: 0.55rem 0.65rem;
  border: 1px solid var(--rule);
  border-radius: 2px;
  background: #fff;
  color: var(--ink);
  font: inherit;
  line-height: 1.4;
}
.bcp-actions { display: flex; flex-wrap: wrap; gap: 0.45rem; align-items: center; }
.bcp-approve { background: var(--torch); color: #fffaf7; border-color: #6e2410; }
.bcp-note { color: var(--quiet); font-size: 0.95rem; }
.bcp-error { color: var(--torch); margin: 0.6rem 0 0; }
.bcp-bed {
  padding: 0.9rem 0.85rem;
  border-left: 1px solid var(--rule);
  background:
    linear-gradient(#e7eef3, #e7eef3) 0 0 / 100% 2.4rem no-repeat,
    repeating-linear-gradient(#f6f8f9, #f6f8f9 2.15rem, #d5dee6 2.15rem 2.25rem);
}
.bcp-bed h2 {
  margin: 0 0 0.8rem;
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  font-size: 1.15rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-weight: 700;
}
.bcp-slots { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.45rem; }
.bcp-slot {
  min-height: 2.3rem;
  padding: 0.35rem 0.45rem;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid transparent;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.78rem;
}
.bcp-slot[data-filled="yes"] { border-color: var(--brass-deep); }
.bcp button:disabled { opacity: 0.42; cursor: not-allowed; }
.bcp button:focus-visible, .bcp textarea:focus-visible {
  outline: 2px solid var(--torch);
  outline-offset: 2px;
}
@media (max-width: 800px) {
  .bcp-bench { grid-template-columns: 1fr; }
  .bcp-rail { flex-direction: row; flex-wrap: wrap; border-right: 0; border-bottom: 1px solid var(--rule); }
  .bcp-gate { flex: 1 0 100%; }
  .bcp-bed { border-left: 0; border-top: 1px solid var(--rule); }
}
@media (prefers-reduced-motion: reduce) {
  .bcp-latch { transition: none; }
}
`;

async function post(url: string, payload: object): Promise<{ ok: boolean; body: ThreadBody }> {
  const response = await apiWrite(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let body: ThreadBody = {};
  try {
    body = await response.json() as ThreadBody;
  } catch {
    body = { message: "The thread did not answer." };
  }
  return { ok: response.ok, body };
}

export function BuilderChatPlan({ projectId }: Props) {
  const [threadId, setThreadId] = useState("");
  const [mode, setMode] = useState<"chat" | "plan">("chat");
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [opening, setOpening] = useState(true);
  const [retry, setRetry] = useState<(() => void) | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const result = await post("/v1/builder/threads", { name: "Builder thread" });
        if (!alive) return;
        if (!result.ok || !result.body.thread_id) {
          setError(result.body.message || "The thread did not open.");
          setRetry(() => () => window.location.reload());
          return;
        }
        setThreadId(result.body.thread_id);
        setFiles(result.body.code_files ?? []);
      } catch {
        if (alive) setError("The thread did not open.");
      } finally {
        if (alive) setOpening(false);
      }
    })();
    return () => { alive = false; };
  }, []);

  function apply(body: ThreadBody): void {
    if (body.turns) setTurns(body.turns);
    if ("plan" in body) setPlan(body.plan ?? null);
    if ("approval" in body) setApproval(body.approval ?? null);
    if (Array.isArray(body.code_files)) setFiles(body.code_files);
    if ((body.mode === "chat" || body.mode === "plan") && body.wrote_code) {
      setError("Chat and Plan cannot write code.");
    }
  }

  async function run(action: () => Promise<{ ok: boolean; body: ThreadBody }>): Promise<void> {
    setBusy(true);
    setError("");
    setRetry(null);
    try {
      const result = await action();
      if (!result.ok) {
        setError(result.body.message || "The turn failed.");
        setRetry(() => () => { void run(action); });
        return;
      }
      apply(result.body);
    } catch {
      setError("The thread did not answer.");
      setRetry(() => () => { void run(action); });
    } finally {
      setBusy(false);
    }
  }

  function onSend(event: React.FormEvent): void {
    event.preventDefault();
    const text = draft.trim();
    if (!text || !threadId || busy) return;
    void run(() => post(`/v1/builder/threads/${threadId}/turns`, { mode, text }));
    setDraft("");
  }

  const approved = Boolean(plan && approval && approval.plan_id === plan.id);

  function onApprove(): void {
    if (!threadId || !plan || approved || busy) return;
    void run(() => post(`/v1/builder/threads/${threadId}/approvals`, { person_id: "operator" }));
  }

  function onBuild(): void {
    if (!approved || !threadId || busy) {
      setError("Build stayed closed until you approve.");
      return;
    }
    void run(() => post(`/v1/builder/threads/${threadId}/turns`, { mode: "build", text: "" }));
  }

  const slots = files.length ? files : ["No code files."];

  return (
    <section className="bcp" data-testid="builder-chat-plan" data-project={projectId ?? ""} aria-label="Builder thread">
      <style>{CSS}</style>
      <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=Literata:opsz,wght@7..72,560;7..72,700&display=swap" />
      <p className="bcp-eyebrow">Builder thread</p>
      <h1>Nothing is written yet.</h1>
      <p className="bcp-lead">Chat and Plan stay on this thread. They do not write code.</p>
      <p className="bcp-lead">A build writes code only after you approve it.</p>
      <div className="bcp-bench">
        <div className="bcp-rail" role="radiogroup" aria-label="Thread mode">
          <button type="button" data-testid="bcp-mode-chat" aria-pressed={mode === "chat"} title="Talk, and leave the files" onClick={() => setMode("chat")}>Chat</button>
          <button type="button" data-testid="bcp-mode-plan" aria-pressed={mode === "plan"} title="A plan writes no code" onClick={() => setMode("plan")}>Plan</button>
          <div className="bcp-gate" data-testid="bcp-gate" data-open={approved ? "yes" : "no"} aria-hidden="true">
            <span className="bcp-latch" />
          </div>
          <button type="button" className="bcp-build" data-testid="bcp-build" disabled={!approved || busy} title={approved ? "Write the approved plan" : "Closed until you approve"} onClick={onBuild}>Build</button>
        </div>
        <div className="bcp-thread">
          {opening ? <p>Opening the thread.</p> : null}
          {!opening && turns.length === 0 ? <p className="bcp-note">The thread is empty.</p> : null}
          <ol className="bcp-proof">
            {turns.map((turn, index) => (
              <li key={`${turn.mode}-${index}`}>
                <span className="bcp-mark">{turn.mode}</span>
                <p>{turn.reply || turn.text}</p>
              </li>
            ))}
          </ol>
          {plan ? (
            <div className="bcp-plan" data-testid="bcp-plan">
              <span className="bcp-mark">Plan</span>
              <p>This plan writes nothing.</p>
              <ol>
                {plan.steps.map((step) => <li key={step}><p>{step}</p></li>)}
              </ol>
            </div>
          ) : null}
          <form className="bcp-compose" onSubmit={onSend}>
            <textarea data-testid="bcp-message" aria-label="Message" placeholder="Say what to plan" value={draft} onChange={(event) => setDraft(event.target.value)} />
            <div className="bcp-actions">
              <button type="submit" className="bcp-send" data-testid="bcp-send" disabled={!threadId || busy}>Send</button>
              <button type="button" className="bcp-approve" data-testid="bcp-approve" title="You allow one build" disabled={!plan || approved || busy} onClick={onApprove}>Approve plan</button>
              <p className="bcp-note">{approved ? "Ready to build." : "Closed until you approve."}</p>
            </div>
          </form>
          {error ? <p className="bcp-error" role="alert" data-testid="bcp-error">{error}</p> : null}
          {retry ? <button type="button" className="bcp-retry" data-testid="bcp-retry" onClick={retry}>Retry</button> : null}
        </div>
        <aside className="bcp-bed" aria-label="Code files">
          <h2>Code files</h2>
          <ul className="bcp-slots" data-testid="bcp-files">
            {slots.map((slot) => (
              <li key={slot} className="bcp-slot" data-filled={slot === "No code files." ? "no" : "yes"}>{slot}</li>
            ))}
            {files.length ? <li className="bcp-slot" data-filled="no">These came from the approved build.</li> : null}
          </ul>
        </aside>
      </div>
    </section>
  );
}
