"use client";

import React, { useCallback, useEffect, useRef, useState, type Dispatch, type ReactNode, type RefObject } from "react";

import { TOAST_EVENT } from "../../../../lib/api-write";
import {
  NameInUseError, ProblemsError, checkSpec, isDirty, publishSpec,
} from "../../../../lib/workflow-editor-types";
import type {
  CheckResult, EditorAction, EditorMeta, EditorState, Problem, PublishRequest, Spec,
} from "../../../../lib/workflow-editor-types";
import { applyWorkClass, workClassLabel } from "../inspector/workflow-settings";
import { Popover } from "../inspector/popover";
import { jsonGate } from "../json/json-view";
import { SaveCopyPopover, type CopyChoice } from "./save-copy-popover";

export const SAVE_TIP = "Publish as the next version (Ctrl+S)";
export const CHECK_TIP = "Runs every check Save runs, without saving.";

const EMPTY_SUMMARY: CheckResult["summary"] = { steps: 0, agents: [], approvals: [], typical_minutes: null, needs: [] };
const USE_FOR_KEY = (key: string) => `wf-use-for:${key}`;

function toast(type: "success" | "error", message: string) {
  try { window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type, message } })); } catch { /* */ }
}

/** What the server would refuse that the client can see for itself: a gate with no question. */
export function localProblems(spec: Spec): Problem[] {
  return spec.steps
    .filter((s) => s.kind === "decision" && !String(s.question ?? "").trim())
    .map((s) => ({ step_id: s.id, field: "question", message: "Question is required" }));
}

/** The first problem's step, in flow order, so Save lands the operator on the node to fix. */
function firstBadStep(spec: Spec, problems: Problem[]): string | null {
  const ids = new Set(problems.map((p) => p.step_id).filter(Boolean));
  return spec.steps.find((s) => ids.has(s.id))?.id ?? null;
}

type Pop = null | "copy" | "issues" | "start" | "use-for";

/**
 * Check, Save, Save as my copy and Start — the publish half of the editor.
 *
 * ONE PATH TO A VERSION. Save always runs `/check` first, then ONE `publish` call: an own
 * workflow gets v(n+1); a new one is created and published at once; a standard becomes your
 * copy (or updates the copy you already have) in the same single call. Nothing is written
 * before the check passes, and Save is disabled while one is in flight — so a double press, a
 * failed publish or a second Save of a standard cannot leave an orphan or a second copy.
 *
 * The host (the editor's top bar) attaches `saveRef` / `startRef` to its buttons and renders
 * `popover` anywhere; the popovers place themselves against those buttons.
 */
export function useSaveFlow({ state, dispatch, projectId, onNavigate, onStart, actor, standardUsedFor }: {
  state: EditorState;
  dispatch: Dispatch<EditorAction>;
  projectId: string;
  onNavigate(key: string): void;
  /** Opens the Runs composer on this workflow. */
  onStart?(key: string): void;
  actor?: string;
  /** The work classes a standard being edited is mapped to, for the after-copy `Use for` chip. */
  standardUsedFor?: string[];
}): {
  save(): Promise<void>; check(): Promise<void>; start(): void; guardLeave(go: () => void): void;
  saving: boolean; checking: boolean; popover: ReactNode; label: "Save" | "Save as my copy";
  canSave: boolean; saveTip: string; nameError: string; dirty: boolean;
  saveRef: RefObject<HTMLButtonElement | null>; startRef: RefObject<HTMLButtonElement | null>;
} {
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [pop, setPop] = useState<Pop>(null);
  const [nameError, setNameError] = useState("");
  const [leave, setLeave] = useState<null | (() => void)>(null);
  const [useFor, setUseFor] = useState<string | null>(null);
  const saveRef = useRef<HTMLButtonElement | null>(null);
  const startRef = useRef<HTMLButtonElement | null>(null);
  const inFlight = useRef(false);
  const startAfter = useRef(false);
  // The handlers read the latest state, not the state of the render that made them.
  const live = useRef(state);
  live.current = state;

  const { meta } = state;
  const dirty = isDirty(state);
  const gate = jsonGate(state);
  const standard = meta.is_catalogue;
  const label: "Save" | "Save as my copy" = standard ? "Save as my copy" : "Save";
  const readOnly = meta.readOnlyVersion != null;

  const canSave = !saving && !gate.blocked && !readOnly && state.spec.steps.length > 0
    // A standard can be saved as a copy unchanged: that IS the change.
    && (dirty || standard);
  const saveTip = gate.blocked ? gate.tip
    : readOnly ? `Viewing v${meta.readOnlyVersion}`
    : state.spec.steps.length === 0 ? "Add a step first"
    : !dirty && !standard ? "No changes"
    : standard ? "Shipped with BOSS. Saving makes your own copy."
    : SAVE_TIP;

  // A copy just saved from a standard that is mapped to a class of work: say the copy is not
  // routed until chosen. Kept across the URL change to the copy's key.
  useEffect(() => {
    const key = meta.workflow_key;
    if (!key) return;
    try {
      const pending = window.sessionStorage.getItem(USE_FOR_KEY(key));
      if (pending) { window.sessionStorage.removeItem(USE_FOR_KEY(key)); setUseFor(pending); setPop("use-for"); }
    } catch { /* storage blocked: the chip is a convenience */ }
  }, [meta.workflow_key]);

  const showProblems = useCallback((problems: Problem[]) => {
    const s = live.current;
    dispatch({ type: "SET_PROBLEMS", result: { ok: false, problems, summary: s.summary ?? EMPTY_SUMMARY } });
    const first = firstBadStep(s.spec, problems);
    if (first) dispatch({ type: "SELECT", id: first });
    setPop("issues");
  }, [dispatch]);

  const runCheck = useCallback(async (): Promise<boolean> => {
    const s = live.current;
    const local = localProblems(s.spec);
    if (local.length) { showProblems(local); return false; }
    const definitionId = s.meta.is_catalogue ? (s.meta.copy?.definition_id ?? null) : s.meta.definition_id;
    const result = await checkSpec(projectId, s.spec, definitionId);
    dispatch({ type: "SET_PROBLEMS", result });
    if (!result.ok || result.problems.length) {
      const first = firstBadStep(s.spec, result.problems);
      if (first) dispatch({ type: "SELECT", id: first });
      setPop("issues");
      return false;
    }
    return true;
  }, [dispatch, projectId, showProblems]);

  const check = useCallback(async () => {
    if (live.current.jsonError || checking) return;
    setChecking(true);
    try { await runCheck(); }
    catch (e) { toast("error", String((e as Error)?.message ?? e)); }
    finally { setChecking(false); }
  }, [checking, runCheck]);

  const publish = useCallback(async (copy: CopyChoice | null) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    setNameError("");
    const s = live.current;
    const m = s.meta;
    let req: PublishRequest;
    if (m.is_catalogue) {
      const updating = copy?.mode === "update" && m.copy;
      req = {
        definition_id: null,
        name: updating ? m.copy!.name : (copy?.mode === "new" ? copy.name : `${m.name} (mine)`),
        description: m.description,
        spec: s.spec,
        from_workflow_key: m.workflow_key,
        update_copy_id: updating ? m.copy!.definition_id : null,
      };
    } else {
      req = { definition_id: m.definition_id, name: m.name.trim(), description: m.description, spec: s.spec,
              from_workflow_key: m.from_workflow_key };
    }
    try {
      const result = await publishSpec(projectId, req);
      setPop(null);
      if ("unchanged" in result) {
        toast("success", "No changes");
        // Updating a copy that already says exactly this: go to the copy, as a save would.
        const target = m.is_catalogue && req.update_copy_id ? m.copy!.workflow_key : m.workflow_key;
        if (target !== m.workflow_key) onNavigate(target!);
        else dispatch({ type: "SAVED", spec: s.spec, meta: m, sent: m });
        if (startAfter.current && target) onStart?.(target);
        return;
      }
      const next: EditorMeta = {
        ...m,
        definition_id: result.definition.id,
        workflow_key: result.definition.workflow_key,
        name: result.definition.name,
        version: result.version,
        is_catalogue: false,
        from_workflow_key: m.is_catalogue ? m.workflow_key : m.from_workflow_key,
        copy: null,
        readOnlyVersion: null,
      };
      // `s` is the snapshot that was published: it becomes the baseline, and anything edited
      // since (the editor stays live during a save) is kept as the next unsaved change.
      dispatch({ type: "SAVED", spec: s.spec, meta: next, sent: m });
      toast("success", m.is_catalogue ? `Saved as your copy · v${result.version}` : `Saved · v${result.version}`);
      const used = m.is_catalogue ? standardUsedFor?.[0] : undefined;
      if (used) {
        try { window.sessionStorage.setItem(USE_FOR_KEY(result.definition.workflow_key), used); } catch { /* */ }
      }
      if (result.definition.workflow_key !== m.workflow_key) onNavigate(result.definition.workflow_key);
      else if (used) { setUseFor(used); setPop("use-for"); }
      if (startAfter.current) onStart?.(result.definition.workflow_key);
    } catch (e) {
      if (e instanceof ProblemsError) showProblems(e.problems);
      else if (e instanceof NameInUseError) {
        setNameError("Name in use");
        if (!m.is_catalogue) toast("error", "Name in use");
      } else toast("error", String((e as Error)?.message ?? e));
    } finally {
      inFlight.current = false;
      startAfter.current = false;
      setSaving(false);
    }
  }, [dispatch, onNavigate, onStart, projectId, showProblems, standardUsedFor]);

  const save = useCallback(async () => {
    if (inFlight.current || !canSave) return;
    inFlight.current = true;
    setSaving(true);
    let ok = false;
    try { ok = await runCheck(); }
    catch (e) { toast("error", String((e as Error)?.message ?? e)); }
    finally { inFlight.current = false; setSaving(false); }
    if (!ok) { startAfter.current = false; return; }
    if (live.current.meta.is_catalogue) { setNameError(""); setPop("copy"); return; }
    await publish(null);
  }, [canSave, runCheck, publish]);

  const start = useCallback(() => {
    const m = live.current.meta;
    if (!isDirty(live.current) && m.workflow_key && m.definition_id) { onStart?.(m.workflow_key); return; }
    setPop("start");
  }, [onStart]);

  const guardLeave = useCallback((go: () => void) => {
    if (!isDirty(live.current)) { go(); return; }
    setLeave(() => go);
  }, []);

  const startVersion = meta.workflow_key && meta.definition_id && meta.version != null ? meta.version : null;

  const popover = (
    <>
      <SaveCopyPopover anchor={saveRef} open={pop === "copy"} onClose={() => { setPop(null); startAfter.current = false; }}
                       standardName={meta.name} copy={meta.copy} busy={saving} nameError={nameError}
                       onSave={(choice) => void publish(choice)} />
      <Popover anchor={saveRef} open={pop === "issues"} onClose={() => setPop(null)} label="Issues"
               width={320} testId="wf-issues">
        <div className="wf-pop-title">{state.problems.length} issue{state.problems.length === 1 ? "" : "s"}</div>
        <ul className="wf-pop-list">
          {state.problems.slice(0, 8).map((p, i) => (
            <li key={i}>
              <button type="button" className="wf-pop-row" title={p.message}
                      onClick={() => { if (p.step_id) dispatch({ type: "SELECT", id: p.step_id }); setPop(null); }}>
                {p.step_id ? <span className="wf-mono wf-faint">{p.step_id}</span> : null}
                <span className="wf-pop-msg">{p.message}</span>
              </button>
            </li>
          ))}
        </ul>
      </Popover>
      <Popover anchor={startRef} open={pop === "start"} onClose={() => setPop(null)} label="Save first?"
               width={260} testId="wf-start-pop">
        <div className="wf-pop-title">Save first?</div>
        <div className="wf-pop-actions">
          <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-save-and-start"
                  disabled={!canSave}
                  title={canSave ? undefined : saveTip}
                  onClick={() => { setPop(null); startAfter.current = true; void save(); }}>Save and start</button>
          {startVersion !== null ? (
            <button type="button" className="wf-btn" data-testid="wf-start-saved"
                    onClick={() => { setPop(null); onStart?.(meta.workflow_key!); }}>Start v{startVersion}</button>
          ) : null}
        </div>
      </Popover>
      <Popover anchor={saveRef} open={pop === "use-for" && Boolean(useFor)} onClose={() => setPop(null)}
               label="Use for" width={300} testId="wf-use-for-pop">
        <button type="button" className="wf-chip wf-chip-action" data-testid="wf-use-for"
                title="Your copy isn't used for new work until you choose it"
                onClick={async () => {
                  const key = live.current.meta.workflow_key;
                  if (!key || !useFor) return;
                  if (await applyWorkClass({ projectId, actor, workflowKey: key, workClass: useFor })) setPop(null);
                }}>
          Use for {workClassLabel(useFor ?? "").toLowerCase()} instead of the standard
        </button>
      </Popover>
      {leave ? (
        <div className="wf-confirm-backdrop" role="presentation" onClick={() => setLeave(null)}>
          <div className="wf-confirm" role="alertdialog" aria-modal="true" aria-label="Discard changes?"
               data-testid="wf-leave-confirm" onClick={(e) => e.stopPropagation()}
               onKeyDown={(e) => { if (e.key === "Escape") setLeave(null); }}>
            <div className="wf-pop-title">Discard changes?</div>
            <div className="wf-pop-actions">
              <button type="button" className="wf-btn" autoFocus onClick={() => setLeave(null)}>Keep editing</button>
              <button type="button" className="wf-btn wf-btn-danger" data-testid="wf-discard"
                      onClick={() => { const go = leave; setLeave(null); go(); }}>Discard</button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );

  return { save, check, start, guardLeave, saving, checking, popover, label, canSave, saveTip,
           nameError, dirty, saveRef, startRef };
}
