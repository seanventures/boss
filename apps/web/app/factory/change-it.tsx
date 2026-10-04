"use client";

/**
 * `⋯ › Change it` (factory spec §4.5.7): one sheet replacing Iterate, Feedback and Rebuild with a
 * fresh spec. Same server actions as before (submitCodeTaskFeedbackAction proposes,
 * confirmCodeTaskFeedbackAction starts); only the surface changed. The agent scopes the change,
 * the proposal shows inline, and nothing runs until `Start` is pressed.
 */
import React, { useEffect, useId, useRef, useState } from "react";

import { callAction } from "../../lib/call-action";
import { confirmCodeTaskFeedbackAction, submitCodeTaskFeedbackAction, type FeedbackActionResult } from "../actions";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";

/** The chips. Each fills the fields the way the old templates did; both stay editable. */
export const CHANGE_CHIPS: { key: string; label: string; tip: string; message: string; expected: string }[] = [
  { key: "page", label: "New screen", tip: "A page or view users navigate to",
    message: "Add a new page at /<route> that shows ",
    expected: "the page loads, is reachable from the nav, and renders real data from the API" },
  { key: "api", label: "API endpoint", tip: "A server route and its storage",
    message: "Add an API endpoint that ",
    expected: "the endpoint returns the right shape, persists through the database, and is covered by a test" },
  { key: "data", label: "Store data", tip: "A migration and the routes for it",
    message: "Persist ",
    expected: "the data survives a reload and a restart, through a migration rather than local state" },
  { key: "auth", label: "Access", tip: "Sign-in, roles and permissions",
    message: "Add accounts so that ",
    expected: "a signed-out visitor cannot reach protected pages, and each user sees only their own data" },
  { key: "design", label: "Polish", tip: "Layout, styling and phone widths",
    message: "Improve the look of ",
    expected: "it reads clearly on a phone and a laptop, and matches the rest of the app" },
  { key: "bug", label: "Something's wrong", tip: "Fix behaviour that is broken",
    message: "Fix: ",
    expected: "the described behaviour no longer happens, and a test proves it" },
];

type Workflow = { id: string; name: string; active_version_id?: string | null };

function useWorkflows(projectId: string): Workflow[] {
  const [items, setItems] = useState<Workflow[]>([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`/api/delivery-workflows?view=installed&project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" });
        if (!r.ok) return;
        const body = (await r.json()) as { items?: Workflow[] };
        if (alive) setItems((body.items ?? []).filter((w) => w.active_version_id));
      } catch { /* the sheet still works inline */ }
    })();
    return () => { alive = false; };
  }, [projectId]);
  return items;
}

export function ChangeIt({ projectId, runId, actor, onClose, onStarted }: {
  projectId: string; runId: string; actor: string; onClose: () => void;
  /** A new build (or workflow run) started from the proposal. */
  onStarted?: (runId: string) => void;
}) {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  const workflows = useWorkflows(projectId);
  const [picked, setPicked] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [expected, setExpected] = useState("");
  const [through, setThrough] = useState("");
  const [busy, setBusy] = useState(false);
  const [proposal, setProposal] = useState<FeedbackActionResult | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    box.current?.querySelector<HTMLElement>("textarea")?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const propose = async () => {
    if (!message.trim() || busy) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.set("run_id", runId);
    form.set("message", message);
    form.set("expected", expected);
    form.set("workflow_version_id", through);
    form.set("actor", actor);
    const r = await callAction(submitCodeTaskFeedbackAction)(null, form);
    setBusy(false);
    if (r?.error) setError(r.error); else setProposal(r);
  };

  const start = async () => {
    if (!proposal?.thread_id || busy) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.set("thread_id", proposal.thread_id);
    form.set("actor", actor);
    const r = await callAction(confirmCodeTaskFeedbackAction)(null, form);
    setBusy(false);
    if (r?.error) { setError(r.error); return; }
    notifyChanged("code-tasks");
    showToast("success", r?.path === "workflow" ? `Started ${r.workflow_name ?? "the workflow"}` : "Change started");
    if (r?.run_id) onStarted?.(r.run_id);
    onClose();
  };

  return (
    <div className="fx-sheet-back" data-testid="fx-change-it" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={box} className="fx-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header className="fx-sheet-head">
          <h2 id={titleId} className="fx-sheet-title">Change it</h2>
          <button type="button" className="ag-link" onClick={onClose} aria-label="Close">×</button>
        </header>
        {proposal?.proposal ? (
          <div className="fx-sheet-body" data-testid="fx-change-proposal">
            <span className="fx-out-sub" title={proposal.proposal.path === "workflow"
              ? `Runs as the ${proposal.proposal.workflow_name ?? ""} workflow` : "Runs inline in the Factory"}>
              {proposal.proposal.path === "workflow" ? proposal.proposal.workflow_name : "Inline"}
            </span>
            <pre className="fx-log fx-log-small" data-user-text>{proposal.proposal.fix_note}</pre>
            <span className="fx-out-links">
              <button type="button" className="ag-btn ag-btn-primary" disabled={busy} data-testid="fx-change-start" onClick={() => void start()}>Start</button>
              <button type="button" className="ag-link" disabled={busy} onClick={() => setProposal(null)}>Edit</button>
            </span>
          </div>
        ) : (
          <div className="fx-sheet-body">
            <span className="fx-chips" role="group" aria-label="What kind of change">
              {CHANGE_CHIPS.map((c) => (
                <button key={c.key} type="button" className="fx-chip" aria-pressed={picked === c.key} title={c.tip}
                        data-testid={`fx-change-chip-${c.key}`}
                        onClick={() => { setPicked(c.key); setMessage(c.message); setExpected(c.expected); }}>
                  {c.label}
                </button>
              ))}
            </span>
            <label className="fx-field">
              <span>What to change</span>
              <textarea className="textarea" rows={3} value={message} onChange={(e) => setMessage(e.target.value)}
                        data-testid="fx-change-what" />
            </label>
            <label className="fx-field">
              <span>Done when</span>
              <input className="input" value={expected} onChange={(e) => setExpected(e.target.value)} data-testid="fx-change-done" />
            </label>
            <label className="fx-field">
              <span>Run through</span>
              <select className="mr-select" value={through} onChange={(e) => setThrough(e.target.value)} data-testid="fx-change-through">
                <option value="">Inline</option>
                {workflows.map((w) => <option key={w.id} value={w.active_version_id as string}>{w.name}</option>)}
              </select>
            </label>
            {proposal?.question ? <span className="fx-ask" data-user-text title="The agent needs this answered">{proposal.question}</span> : null}
            <span className="fx-out-links">
              <button type="button" className="ag-btn ag-btn-primary" disabled={busy || !message.trim()} data-testid="fx-change-propose"
                      onClick={() => void propose()}>Propose</button>
            </span>
          </div>
        )}
        {error ? <span className="fx-err" role="alert" data-user-text>{error}</span> : null}
      </div>
    </div>
  );
}
