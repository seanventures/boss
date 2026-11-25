"use client";

/**
 * Build a workflow from the library, then publish through publishSpec.
 * The saved spec has no trigger field, so the start sentence is the description.
 * A person is the captain "human". A gate is a decision step, the kind the editor places.
 */
import React, { useEffect, useRef, useState } from "react";

import type { Spec, SpecStep } from "../../../lib/workflow-editor-types";
import { publishSpec } from "../../../lib/workflow-api";
import { couldNot, reasonOf } from "../../../lib/action-result";
import "./wizard.css";

type Who = "person" | "role";
type DraftStep = { name: string; who: Who; role: string; instruction: string };
type Phase = "name" | "starts" | "steps" | "approval" | "review" | "saved";

const BEATS: { id: Exclude<Phase, "saved">; label: string }[] = [
  { id: "name", label: "Name" },
  { id: "starts", label: "Starts" },
  { id: "steps", label: "Steps" },
  { id: "approval", label: "Approval" },
  { id: "review", label: "Review" },
];

const EMPTY_DRAFT: DraftStep = { name: "", who: "person", role: "", instruction: "" };

const PERSON = "human";

function stepId(base: string, taken: Set<string>): string {
  const clean = base.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "step";
  if (!taken.has(clean)) return clean;
  for (let n = 2; ; n += 1) {
    const id = `${clean}_${n}`;
    if (!taken.has(id)) return id;
  }
}

/** Steps in the editor's shape: agent work in a line, then a decision step when a person approves. */
function buildWorkflowSpec(steps: DraftStep[], approve: boolean): Spec {
  const taken = new Set<string>();
  const out: SpecStep[] = steps.map((step) => {
    const id = stepId(step.name, taken);
    taken.add(id);
    return {
      id,
      kind: "capability",
      title: step.name,
      captain: step.who === "person" ? PERSON : step.role,
      next: [],
      binding: { agent_work: true, inputs: { instructions: step.instruction } },
    };
  });
  if (approve) {
    const id = stepId("approval", taken);
    taken.add(id);
    out.push({
      id,
      kind: "decision",
      title: "A person approves",
      next: [],
      question: "Approve before this run finishes?",
      guidance: "",
      approver_role: PERSON,
    });
  }
  for (let i = 0; i < out.length - 1; i += 1) out[i] = { ...out[i], next: [out[i + 1].id] };
  return { entry_step: out[0]?.id, steps: out };
}

function instructionOf(step: SpecStep): string {
  const binding = step.binding;
  if (!binding || typeof binding !== "object") return "";
  const inputs = (binding as { inputs?: { instructions?: unknown } }).inputs;
  const text = inputs?.instructions;
  return typeof text === "string" ? text : "";
}

function whoOf(step: SpecStep): string {
  if (step.kind === "decision" || step.captain === PERSON) return "A person";
  return typeof step.captain === "string" && step.captain ? step.captain : "A person";
}

export function WorkflowWizard({ projectId, onClose, onOpen }: {
  projectId: string;
  onClose(): void;
  onOpen(workflowKey: string): void;
}) {
  const [phase, setPhase] = useState<Phase>("name");
  const [name, setName] = useState("");
  const [starts, setStarts] = useState("");
  const [steps, setSteps] = useState<DraftStep[]>([]);
  const [draft, setDraft] = useState<DraftStep>(EMPTY_DRAFT);
  const [approve, setApprove] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<{ name: string; key: string } | null>(null);
  const saving = useRef(false);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    const field = panel.current?.querySelector<HTMLElement>("input, textarea, button");
    field?.focus();
  }, [phase]);

  const shown = phase === "saved" ? "review" : phase;
  const spec = phase === "review" || phase === "saved" ? buildWorkflowSpec(steps, approve === true) : null;

  function forward() {
    if (phase === "name") {
      if (!name.trim()) { setError("Name it first."); return; }
      setError("");
      setPhase("starts");
      return;
    }
    if (phase === "starts") {
      setError("");
      setPhase("steps");
      return;
    }
    if (phase === "steps") {
      if (!steps.length) {
        const half = draft.name.trim() || draft.instruction.trim() || draft.role.trim();
        setError(half ? "Add the step first." : "Add a step first.");
        return;
      }
      setError("");
      setPhase("approval");
      return;
    }
    if (phase === "approval") {
      if (approve === null) { setError("Say whether a person approves."); return; }
      setError("");
      setPhase("review");
    }
  }

  function back() {
    const order: Phase[] = ["name", "starts", "steps", "approval", "review"];
    const at = order.indexOf(phase);
    setError("");
    if (at > 0) setPhase(order[at - 1]);
  }

  function addStep() {
    const title = draft.name.trim();
    const instruction = draft.instruction.trim();
    const role = draft.role.trim();
    if (!title) { setError("Name the step."); return; }
    if (draft.who === "role" && !role) { setError("Name the role."); return; }
    if (!instruction) { setError("Write the instruction."); return; }
    setSteps((cur) => [...cur, { name: title, who: draft.who, role: draft.who === "role" ? role : "", instruction }]);
    setDraft(EMPTY_DRAFT);
    setError("");
  }

  async function save() {
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    setError("");
    const sentence = starts.trim();
    try {
      const result = await publishSpec(projectId, {
        definition_id: null,
        name: name.trim(),
        ...(sentence ? { description: sentence } : {}),
        spec: buildWorkflowSpec(steps, approve === true),
      });
      if ("unchanged" in result) {
        setError("Nothing was saved.");
        return;
      }
      const key = result.definition.workflow_key;
      if (!key) {
        setError("Nothing was saved.");
        return;
      }
      setSaved({ name: result.definition.name || name.trim(), key });
      setPhase("saved");
    } catch (e) {
      setSaved(null);
      setError(couldNot("save the workflow", reasonOf(e), "Nothing was saved."));
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="wfw-scrim" data-testid="wf-wizard">
      <div className="wfw-sheet" role="dialog" aria-modal="true" aria-labelledby="wfw-title">
        <header className="wfw-head">
          <h2 id="wfw-title" className="wfw-title">Build a workflow</h2>
          <button type="button" className="wf-btn wf-btn-ghost" onClick={onClose}>Close</button>
        </header>
        <ol className="wfw-beats">
          {BEATS.map((beat) => (
            <li key={beat.id} aria-current={shown === beat.id ? "step" : undefined}>{beat.label}</li>
          ))}
        </ol>
        <div className="wfw-body" ref={panel}>
          {phase === "name" ? (
            <label className="wf-field">
              <span className="wf-label">Name</span>
              <input className="wf-input" data-testid="wf-wizard-name" value={name}
                     aria-invalid={error ? true : undefined}
                     onChange={(e) => { setName(e.target.value); setError(""); }} />
            </label>
          ) : null}

          {phase === "starts" ? (
            <label className="wf-field">
              <span className="wf-label">When it starts</span>
              <textarea className="wf-text" rows={3} data-testid="wf-wizard-starts" value={starts}
                        onChange={(e) => setStarts(e.target.value)} />
            </label>
          ) : null}

          {phase === "steps" ? (
            <>
              <label className="wf-field">
                <span className="wf-label">Name</span>
                <input className="wf-input" data-testid="wf-wizard-step-name" value={draft.name}
                       onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
              </label>
              <div className="wf-field">
                <span className="wf-label">Who</span>
                <div className="wf-seg" role="group" aria-label="Who">
                  <button type="button" aria-pressed={draft.who === "person"} data-testid="wf-wizard-who-person"
                          onClick={() => setDraft({ ...draft, who: "person" })}>A person</button>
                  <button type="button" aria-pressed={draft.who === "role"} data-testid="wf-wizard-who-role"
                          onClick={() => setDraft({ ...draft, who: "role" })}>A role</button>
                </div>
              </div>
              {draft.who === "role" ? (
                <label className="wf-field">
                  <span className="wf-label">Role</span>
                  <input className="wf-input" data-testid="wf-wizard-role" value={draft.role}
                         onChange={(e) => setDraft({ ...draft, role: e.target.value })} />
                </label>
              ) : null}
              <label className="wf-field">
                <span className="wf-label">Instruction</span>
                <input className="wf-input" data-testid="wf-wizard-instruction" value={draft.instruction}
                       onChange={(e) => setDraft({ ...draft, instruction: e.target.value })} />
              </label>
              <button type="button" className="wf-btn" data-testid="wf-wizard-add-step" onClick={addStep}>
                Add step
              </button>
              {steps.length ? (
                <ol className="wfw-lane">
                  {steps.map((step, i) => (
                    <li key={`${step.name}-${i}`} className="wfw-node">
                      <span className="wfw-rail" aria-hidden><span className="wfw-dot" /><span className="wfw-stem" /></span>
                      <div className="wfw-node-body">
                        <p className="wfw-node-title">{step.name}</p>
                        <p className="wfw-node-meta">{step.who === "person" ? "A person" : step.role}</p>
                        <p className="wfw-node-meta">{step.instruction}</p>
                        <button type="button" className="wf-link" onClick={() => setSteps((cur) => cur.filter((_, n) => n !== i))}>
                          Remove
                        </button>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : <p className="wfw-quiet">No steps yet.</p>}
            </>
          ) : null}

          {phase === "approval" ? (
            <div className="wf-field">
              <span className="wf-label">Approve before it finishes</span>
              <div className="wf-seg" role="group" aria-label="Approve before it finishes">
                <button type="button" aria-pressed={approve === true} data-testid="wf-wizard-approve-yes"
                        onClick={() => { setApprove(true); setError(""); }}>Yes</button>
                <button type="button" aria-pressed={approve === false} data-testid="wf-wizard-approve-no"
                        onClick={() => { setApprove(false); setError(""); }}>No</button>
              </div>
            </div>
          ) : null}

          {phase === "review" && spec ? (
            <>
              <p className="wfw-name" data-testid="wf-wizard-review-name">{name.trim()}</p>
              {starts.trim() ? (
                <>
                  <p className="wfw-starts" data-testid="wf-wizard-review-starts">{starts.trim()}</p>
                  <p className="wfw-note" data-testid="wf-wizard-review-note">This is the description. The spec has no trigger.</p>
                </>
              ) : <p className="wfw-note" data-testid="wf-wizard-review-starts">No start sentence.</p>}
              <ol className="wfw-lane">
                {spec.steps.map((step) => (
                  <li key={step.id} className="wfw-node" data-kind={step.kind === "decision" ? "gate" : "work"}>
                    <span className="wfw-rail" aria-hidden><span className="wfw-dot" /><span className="wfw-stem" /></span>
                    <div className="wfw-node-body">
                      <p className="wfw-node-title">{step.title}</p>
                      <p className="wfw-node-meta">{whoOf(step)}</p>
                      {instructionOf(step) ? <p className="wfw-node-meta">{instructionOf(step)}</p> : null}
                    </div>
                  </li>
                ))}
              </ol>
            </>
          ) : null}

          {phase === "saved" && saved ? (
            <>
              <p className="wfw-status" data-testid="wf-wizard-saved">Saved</p>
              <p className="wfw-name" data-testid="wf-wizard-saved-name">{saved.name}</p>
              <p className="wfw-key" data-testid="wf-wizard-key">{saved.key}</p>
              <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-wizard-open-editor"
                      onClick={() => onOpen(saved.key)}>
                Open in editor
              </button>
            </>
          ) : null}
        </div>
        {phase !== "saved" ? (
          <footer className="wfw-foot">
            {error ? <p className="wfw-error" role="alert" data-testid="wf-wizard-error">{error}</p> : null}
            <div className="wfw-actions">
              {phase === "name" ? <span /> : (
                <button type="button" className="wf-btn" data-testid="wf-wizard-back" onClick={back}>Back</button>
              )}
              {phase === "review" ? (
                <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-wizard-save"
                        disabled={busy} onClick={() => save()}>
                  {busy ? "Saving…" : "Save"}
                </button>
              ) : (
                <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-wizard-continue"
                        onClick={forward}>
                  Continue
                </button>
              )}
            </div>
          </footer>
        ) : null}
      </div>
    </div>
  );
}
