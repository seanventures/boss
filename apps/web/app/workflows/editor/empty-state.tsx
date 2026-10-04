"use client";

/**
 * A new workflow's first screen: three ways in, and none of them writes anything.
 *
 *  - Describe it: BOSS drafts the steps from a sentence; they land on the canvas to review.
 *  - Start from a standard: its steps are loaded into this draft, named "<Standard> (mine)".
 *  - Blank: one dashed target that opens QuickAdd.
 *
 * Nothing is saved until Save. Dragging a row from the palette onto the lane works here too.
 */
import { Plus, Sparkle, SquaresFour, Stack } from "@phosphor-icons/react";
import React, { useState } from "react";

import type { EditorAction, WorkflowSummary } from "../../../lib/workflow-editor-types";
import { draftFromDescription, fetchLibrary, fetchSpec } from "../../../lib/workflow-api";
import { TOAST_EVENT } from "../../../lib/api-write";
import { MiniLane } from "../flow/mini-lane";

export const NO_MODEL_TIP = "Needs a model. Connect one in Repo and models.";

function toastError(message: string) {
  try { window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type: "error", message } })); } catch { /* no window */ }
}

/** The draft endpoint answers 409 "no model" when no model is configured. */
export function isNoModel(e: unknown): boolean {
  const text = e instanceof Error ? e.message : String(e ?? "");
  return /no model/i.test(text);
}

export function EmptyState({ projectId, dispatch, onAddFirst, readOnly }: {
  projectId: string;
  dispatch(a: EditorAction): void;
  onAddFirst(anchor: HTMLElement): void;
  readOnly?: boolean;
}) {
  const [open, setOpen] = useState<"describe" | "standard" | null>(null);
  const [text, setText] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [noModel, setNoModel] = useState(false);
  const [standards, setStandards] = useState<WorkflowSummary[] | null>(null);
  const [loadingKey, setLoadingKey] = useState<string | null>(null);

  const draft = async () => {
    if (!text.trim() || drafting) return;
    setDrafting(true);
    try {
      const out = await draftFromDescription(projectId, text.trim());
      dispatch({ type: "SET_SPEC", spec: out.spec });
      if (out.name) dispatch({ type: "SET_META", patch: { name: out.name } });
    } catch (e) {
      if (isNoModel(e)) setNoModel(true);
      else toastError(e instanceof Error ? e.message : "Could not draft steps");
    } finally {
      setDrafting(false);
    }
  };

  const openStandards = async () => {
    setOpen("standard");
    if (standards) return;
    try {
      setStandards((await fetchLibrary(projectId)).filter((w) => w.is_catalogue));
    } catch (e) {
      setStandards([]);
      toastError(e instanceof Error ? e.message : "Could not load the standards");
    }
  };

  const pickStandard = async (w: WorkflowSummary) => {
    setLoadingKey(w.workflow_key);
    try {
      const { spec } = await fetchSpec(projectId, w.workflow_key);
      dispatch({ type: "SET_SPEC", spec });
      dispatch({ type: "SET_META", patch: { name: `${w.name} (mine)`, from_workflow_key: w.workflow_key } });
    } catch (e) {
      toastError(e instanceof Error ? e.message : "Could not load that workflow");
    } finally {
      setLoadingKey(null);
    }
  };

  return (
    <div className="wf-empty" data-testid="wf-empty">
      <div className="wf-empty-tiles">
        <div className="wf-tile" data-open={open === "describe" ? "true" : undefined}>
          <button type="button" className="wf-tile-head" disabled={readOnly || noModel}
                  aria-expanded={open === "describe"}
                  data-tip={noModel ? NO_MODEL_TIP : "BOSS drafts the steps. You review before saving."}
                  onClick={() => setOpen(open === "describe" ? null : "describe")} data-testid="wf-tile-describe">
            <Sparkle size={20} weight="bold" aria-hidden /><span>Describe it</span>
          </button>
          {open === "describe" && !noModel ? (
            <div className="wf-tile-body">
              <textarea className="wf-input" rows={3} placeholder="What should it do?" value={text} autoFocus
                        aria-label="What should it do?" onChange={(e) => setText(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void draft(); }} />
              <button type="button" className="wf-btn wf-btn-primary" disabled={!text.trim() || drafting}
                      onClick={() => void draft()} data-testid="wf-draft-steps">
                {drafting ? "Drafting…" : "Draft steps"}
              </button>
            </div>
          ) : null}
        </div>

        <div className="wf-tile" data-open={open === "standard" ? "true" : undefined}>
          <button type="button" className="wf-tile-head" disabled={readOnly} aria-expanded={open === "standard"}
                  data-tip="Copy a standard's steps into this draft"
                  onClick={() => (open === "standard" ? setOpen(null) : void openStandards())}
                  data-testid="wf-tile-standard">
            <SquaresFour size={20} weight="bold" aria-hidden /><span>Start from a standard</span>
          </button>
        </div>

        <div className="wf-tile" data-open="true">
          <span className="wf-tile-head" data-static="true">
            <Plus size={20} weight="bold" aria-hidden /><span>Blank</span>
          </span>
          <div className="wf-tile-body">
            <button type="button" className="wf-add-first" disabled={readOnly}
                    onClick={(e) => onAddFirst(e.currentTarget)} data-testid="wf-add-first">
              + Add first step
            </button>
          </div>
        </div>
      </div>

      {open === "standard" ? (
        <ul className="wf-standards" aria-label="Standards" data-testid="wf-standards">
          {standards === null ? <li className="wf-standard-loading" aria-busy="true" /> : null}
          {(standards ?? []).map((w) => (
            <li key={w.workflow_key}>
              <button type="button" className="wf-standard" disabled={!!loadingKey}
                      aria-busy={loadingKey === w.workflow_key || undefined}
                      data-tip={w.description || undefined} onClick={() => void pickStandard(w)}
                      data-testid={`wf-standard-${w.workflow_key}`}>
                <span className="wf-standard-name"><Stack size={14} weight="bold" aria-hidden />{w.name}</span>
                <MiniLane steps={w.steps} />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
