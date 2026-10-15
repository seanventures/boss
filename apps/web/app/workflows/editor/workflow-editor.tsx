"use client";

/**
 * THE WORKFLOW EDITOR: one place to create and change a workflow, visually or as JSON.
 *
 * The state is the spec itself (`useWorkflowEditor`), so the lane, the inspector and the JSON
 * can never disagree, and keys this console does not draw survive a save. This component is
 * the shell around it: the top bar, the summary line, the palette, the lane (or the JSON), the
 * inspector drawer, the keyboard and the URL.
 *
 *   ?area=delivery_workflows&wf=<key|new>[&step=<id>][&view=json][&v=<n>]
 *
 * Every edit is one action on the store, so each is one undo step. Saving, checking, starting
 * and the leave guard are the save flow's (`useSaveFlow`).
 */
import { Plus } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { CustomStepDef, FlowLink, PaletteItem, Spec, StepView } from "../../../lib/workflow-editor-types";
import { TOAST_EVENT } from "../../../lib/api-write";
import { checkSpec, deleteWorkflow, draftFromDescription, fetchSpec } from "../../../lib/workflow-api";
import { checkRewire } from "../../../lib/workflow-rewire";
import { newMeta, useWorkflowEditor } from "../../../lib/workflow-editor-store";
import { canRemove, insertStep, layoutFlow, linearOrder } from "../../../lib/workflow-graph";
import { newStepFor } from "../../../lib/step-view";
import {
  blockOfHead, canRemoveBlock, gapAfterSelection, placeBlock, removeRefusal, type ControlBlock,
} from "../../../lib/workflow-blocks";
import { FlowLane, type LaneZoom } from "../flow/flow-lane";
import { laneGeometry, minScale } from "../flow/geometry";
import { viewOf } from "../flow/node-facts";
import { useTips } from "../flow/tip-layer";
import { DefineStepModal } from "./define-step-modal";
import { EmptyState, isNoModel, NO_MODEL_TIP } from "./empty-state";
import { Inspector } from "./inspector/inspector";
import { JsonView } from "./json/json-view";
import { StepPalette } from "./palette";
import { QuickAdd } from "./quick-add";
import { useAutoCheck } from "./save/use-auto-check";
import { useSaveFlow } from "./save/use-save-flow";
import { StartsWith } from "./starts-with";
import { SummaryChips } from "./summary-chips";
import { NO_DELETE_TIP, TopBar } from "./top-bar";
import { TestRunPanel } from "./test-run-panel";
import { useCan } from "../../../components/identity/identity-provider";

const BLOCK_REMOVED: Record<ControlBlock, string> = {
  condition: "Branch removed", parallel: "Parallel steps removed", bounded_loop: "Repeat removed",
  for_each: "For each removed",
};
/** A palette pick with nothing selected, in a flow that ends in more than one place. */
const SEVERAL_ENDS = "This flow ends in more than one place: select the step to add after";
const PALETTE_W = 240;
/** What the lane loses to its column beyond the palette: the grid gap and the lane's border. */
const LANE_CHROME = 12 + 2;
const UNDO_TOAST_MS = 6000;
const PALETTE_PREF = "wf:palette";

function toast(type: "success" | "error", message: string) {
  try { window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type, message } })); } catch { /* no window */ }
}

function readPref(): "open" | "rail" | null {
  try {
    const v = window.localStorage.getItem(PALETTE_PREF);
    return v === "open" || v === "rail" ? v : null;
  } catch { return null; }
}

function writePref(v: "open" | "rail") {
  try { window.localStorage.setItem(PALETTE_PREF, v); } catch { /* storage blocked: the choice lasts this visit */ }
}

/** Whether focus is somewhere typing happens, where single keys and Backspace belong to the field. */
function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || !el.tagName) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

/** The URL, rewritten in place: which workflow, step, view and version are open. */
function writeUrl(patch: Record<string, string | null>, push = false) {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    const next = `${url.pathname}${url.search}${url.hash}`;
    if (next === `${window.location.pathname}${window.location.search}${window.location.hash}`) return;
    if (push) window.history.pushState(window.history.state, "", next);
    else window.history.replaceState(window.history.state, "", next);
  } catch { /* not in a browser */ }
}

type Note = { id: number; message: string; undo?: boolean };

export function WorkflowEditor({ projectId, workflowKey, onBack, onStart, actor, usedFor, onSchedule }: {
  projectId: string; workflowKey: string | "new"; onBack(): void; onStart(key: string): void;
  /** The signed-in member: Grant, Used for and new steps are attributed to them. */
  actor?: string;
  /** The work classes this workflow is mapped to, when the host knows them. */
  usedFor?: string[];
  /** Opens the host's schedule popover for a workflow. */
  onSchedule?(workflowKey: string): void;
}): React.JSX.Element {
  useTips();
  const [key, setKey] = useState(workflowKey);
  useEffect(() => { setKey(workflowKey); }, [workflowKey]);

  const { state, dispatch, palette, dirty, loading, error, restore, refreshPalette } = useWorkflowEditor(key, projectId);
  const { spec, meta } = state;
  const readOnly = meta.readOnlyVersion != null;

  const [view, setView] = useState<"visual" | "json">("visual");
  /** The JSON view's Changes diff, held here so ⋯ > Changes can open the view on it. */
  const [jsonChanges, setJsonChanges] = useState(false);
  const showChanges = useCallback(() => { setView("json"); setJsonChanges(true); }, []);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [zoom, setZoom] = useState<LaneZoom>("fit");
  const [quick, setQuick] = useState<{ at: number; anchor: HTMLElement } | null>(null);
  const [defining, setDefining] = useState<{ edit?: CustomStepDef } | null>(null);
  const [sheet, setSheet] = useState(false);
  const [note, setNote] = useState<Note | null>(null);
  const [describeBlocked, setDescribeBlocked] = useState<string | null>(null);
  // The Test run panel (#512): pins and live steps for a test of the saved version.
  const [testing, setTesting] = useState(false);

  /* ── Save, check, start (the save flow's) ── */
  const keyRef = useRef(key);
  keyRef.current = key;
  const onNavigate = useCallback((next: string) => {
    writeUrl({ wf: next, v: null });
    setKey(next);
  }, []);
  const saveFlow = useSaveFlow({ state, dispatch, projectId, onNavigate, onStart, actor, standardUsedFor: usedFor });
  // Saving publishes the workflow (POST /v1/delivery-workflows/save needs workflow:publish).
  const publishGate = useCan("workflow:publish");
  // Delete (DELETE /v1/delivery-workflows/{id}) and Describe (…/draft) have their own permissions.
  const deleteGate = useCan("workflow:delete");
  const draftGate = useCan("workflow:create");
  useAutoCheck(state, dispatch, projectId);

  /* ── The URL ── */
  const urlRead = useRef(false);
  const urlStep = useRef<string | null>(null);
  const pendingVersion = useRef<number | null>(null);
  useEffect(() => {
    try {
      const q = new URLSearchParams(window.location.search);
      if (q.get("view") === "json") setView("json");
      urlStep.current = q.get("step");
      const v = Number(q.get("v"));
      pendingVersion.current = Number.isInteger(v) && v > 0 ? v : null;
    } catch { /* not in a browser */ }
    urlRead.current = true;
  }, []);
  useEffect(() => {
    if (loading) return;
    const step = urlStep.current;
    if (step && spec.steps.some((s) => s.id === step)) dispatch({ type: "SELECT", id: step });
    urlStep.current = null;
  }, [loading, spec.steps, dispatch]);
  useEffect(() => {
    if (!urlRead.current || loading) return;
    writeUrl({
      wf: key, step: state.selection, view: view === "json" ? "json" : null,
      v: meta.readOnlyVersion != null ? String(meta.readOnlyVersion) : null,
    });
  }, [key, state.selection, view, meta.readOnlyVersion, loading]);
  useEffect(() => {
    const onPop = () => {
      const wf = new URLSearchParams(window.location.search).get("wf");
      if (wf && wf !== keyRef.current) setKey(wf);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  /* ── Derived ── */
  const views = useMemo(() => new Map(spec.steps.map((s) => [s.id, viewOf(s, palette)] as [string, StepView])),
    [spec.steps, palette]);
  const order = useMemo(() => linearOrder(spec), [spec]);
  const typicalMinutes = useMemo(() => {
    if (!palette) return null;
    const byKey = new Map(palette.capabilities.map((c) => [c.key, c.typical_minutes ?? 0]));
    let total = 0;
    for (const s of spec.steps) {
      const cap = (s.binding as { capability?: string } | undefined)?.capability;
      total += cap ? byKey.get(cap) ?? 0 : 0;
    }
    return total || null;
  }, [spec.steps, palette]);

  /* ── Palette: open, rail, or (by default) whichever lets the whole flow fit ── */
  const bodyRef = useRef<HTMLDivElement>(null);
  const [bodyWidth, setBodyWidth] = useState(0);
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const read = () => setBodyWidth(el.clientWidth);
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [loading]);
  const [pref, setPref] = useState<"open" | "rail" | null>(null);
  useEffect(() => { setPref(readPref()); }, []);
  const autoRail = useMemo(() => {
    if (!bodyWidth || !spec.steps.length) return false;
    const types = new Map([...views].map(([id, v]) => [id, v.type] as [string, StepView["type"]]));
    const compact = laneGeometry(layoutFlow(spec), types, "compact").width * minScale("compact");
    // FOLD WHENEVER THE OPEN PALETTE MAKES THE FLOW SCROLL, not only when folding makes it fit:
    // a 10-step flow beside the full console nav was left scrolling with the palette open,
    // when folding it shows most of the flow (walk of ws/step-detail, 2026-10-04).
    return compact > bodyWidth - PALETTE_W - LANE_CHROME;
  }, [bodyWidth, spec, views]);
  const rail = pref ? pref === "rail" : autoRail;
  const togglePalette = () => { const next = rail ? "open" : "rail"; setPref(next); writePref(next); };

  /* ── The drawer floats over the lane: how much of the lane's right edge it covers ── */
  const inspectorOpen = !!palette && (!!state.selection || settingsOpen);
  const [occluded, setOccluded] = useState(0);
  useLayoutEffect(() => {
    const body = bodyRef.current;
    const drawer = body?.querySelector<HTMLElement>(".wf-drawer");
    const lane = body?.querySelector<HTMLElement>(".wf-stage .wf-lane-viewport");
    if (!drawer || !lane || view !== "visual") { setOccluded(0); return; }
    const d = drawer.getBoundingClientRect();
    const l = lane.getBoundingClientRect();
    const overlaps = d.top < l.bottom && d.bottom > l.top && d.left < l.right;
    setOccluded(overlaps ? Math.round(l.right - d.left) : 0);
  }, [inspectorOpen, view, bodyWidth, rail, loading]);

  /* ── Edits ── */
  const show = useCallback((message: string, undo = false) => setNote({ id: Date.now(), message, undo }), []);
  useEffect(() => {
    if (!note) return;
    const t = window.setTimeout(() => setNote(null), UNDO_TOAST_MS);
    return () => window.clearTimeout(t);
  }, [note]);

  /** Places a palette item at `at`; the store wires its inputs there and selects it. */
  const insert = useCallback((at: number, item: PaletteItem) => {
    if (!palette || readOnly) return;
    // The flow ends in more than one place (a Branch's paths, never met again): "the end" is
    // not one place, so say how to choose, rather than a refusal that sends you to JSON.
    const ends = at === -1 && layoutFlow(spec).terminal.length > 1;
    if (item.control) {
      // A branch block lands whole: its head, its paths, its join.
      if (!placeBlock(spec, at, item.control).head) { show(ends ? SEVERAL_ENDS : "Can't place that here"); return; }
      dispatch({ type: "PLACE_BLOCK", at, block: item.control });
      setSettingsOpen(false);
      return;
    }
    const step = newStepFor(item, spec, palette);
    if (insertStep(spec, at, step) === spec) { show(ends ? SEVERAL_ENDS : "Edit branches in JSON"); return; }
    dispatch({ type: "INSERT", at, step });
    setSettingsOpen(false);
  }, [palette, readOnly, spec, dispatch, show]);

  /** A palette click: after the selection, else at the end. */
  const pick = useCallback((item: PaletteItem) => {
    insert(gapAfterSelection(spec, state.selection), item);
    setSheet(false);
  }, [state.selection, spec, insert]);

  const remove = useCallback((id: string) => {
    if (readOnly) return;
    // A block's head takes its whole block with it; anything else refused says why.
    if (canRemove(spec, id)) {
      dispatch({ type: "REMOVE", id });
      show("Step removed · Undo", true);
      return;
    }
    const block = blockOfHead(spec.steps.find((s) => s.id === id));
    if (block && canRemoveBlock(spec, id)) {
      dispatch({ type: "REMOVE", id });
      show(`${BLOCK_REMOVED[block]} · Undo`, true);
      return;
    }
    show(removeRefusal(spec, id));
  }, [dispatch, readOnly, show, spec]);

  const move = useCallback((id: string, to: number) => {
    if (!readOnly) dispatch({ type: "MOVE", id, to });
  }, [dispatch, readOnly]);

  /** An arrow pointed at another step: refused out loud when the spec cannot say it, or when the
   *  validator finds a problem the link would add (lib/workflow-rewire.ts); made as one edit. */
  const specRef = useRef(spec);
  specRef.current = spec;
  const rewire = useCallback(async (link: FlowLink, target: string | null) => {
    if (readOnly) return;
    const before = spec;
    const definitionId = meta.is_catalogue ? (meta.copy?.definition_id ?? null) : meta.definition_id;
    // A refusal names steps as the canvas does, by title, not by the engine's ids.
    const titles = (id: string) => {
      const step = before.steps.find((s) => s.id === id);
      return step ? viewOf(step, palette).title : undefined;
    };
    const answer = await checkRewire(before, link, target, (s) => checkSpec(projectId, s, definitionId),
                                     state.checkedSpec === before ? state.problems : null, titles);
    if (!answer.ok) { show(answer.reason); return; }
    if (answer.spec === before) return;
    // Checked against the flow as it was: an edit since then means the answer is about another flow.
    if (specRef.current !== before) { show("The flow changed while that link was checked: try again"); return; }
    dispatch({ type: "REWIRE", link, target });
    show("Link changed · Undo", true);
  }, [readOnly, spec, meta, projectId, palette, state.checkedSpec, state.problems, show, dispatch]);

  const openSettings = useCallback(() => { dispatch({ type: "SELECT", id: null }); setSettingsOpen(true); }, [dispatch]);
  const closeDrawer = useCallback(() => { dispatch({ type: "SELECT", id: null }); setSettingsOpen(false); }, [dispatch]);

  const selectFirstProblem = useCallback(() => {
    const bad = state.problems.find((x) => x.step_id && spec.steps.some((s) => s.id === x.step_id));
    if (bad?.step_id) { dispatch({ type: "SELECT", id: bad.step_id }); setSettingsOpen(false); }
    else openSettings();
  }, [state.problems, spec.steps, dispatch, openSettings]);

  /* ── A step defined from the palette lands at the selection once the palette knows it ── */
  const pendingStep = useRef<string | null>(null);
  useEffect(() => {
    const stepKey = pendingStep.current;
    if (!stepKey || !palette?.customSteps.some((c) => c.step_key === stepKey)) return;
    pendingStep.current = null;
    pick({ customStep: stepKey });
  }, [palette, pick]);

  /* ── Versions: an older one opens read-only; Restore makes it the draft ── */
  const current = useRef<{ spec: Spec; meta: typeof meta } | null>(null);
  const loadVersions = useCallback(async () => {
    if (!meta.workflow_key) return [];
    return (await fetchSpec(projectId, meta.workflow_key)).versions;
  }, [projectId, meta.workflow_key]);
  const openVersion = useCallback(async (v: number) => {
    if (!meta.workflow_key) return;
    try {
      const got = await fetchSpec(projectId, meta.workflow_key, v);
      if (!readOnly) current.current = { spec: state.loaded ?? spec, meta: state.loadedMeta };
      dispatch({ type: "LOAD", spec: got.spec, meta: { ...got.meta, readOnlyVersion: got.meta.readOnlyVersion ?? null } });
    } catch (e) {
      toast("error", e instanceof Error ? e.message : `Could not open v${v}`);
    }
  }, [projectId, meta.workflow_key, readOnly, state.loaded, state.loadedMeta, spec, dispatch]);
  useEffect(() => {
    const v = pendingVersion.current;
    if (loading || v === null || !meta.workflow_key) return;
    pendingVersion.current = null;
    if (v !== meta.version) void openVersion(v);
  }, [loading, meta.workflow_key, meta.version, openVersion]);
  const leaveVersion = useCallback(() => {
    const back = current.current;
    if (back) dispatch({ type: "LOAD", spec: back.spec, meta: { ...back.meta, readOnlyVersion: null } });
    current.current = null;
  }, [dispatch]);
  const restoreVersion = useCallback(() => {
    const old = spec;
    leaveVersion();
    dispatch({ type: "SET_SPEC", spec: old });
  }, [spec, leaveVersion, dispatch]);

  /* ── Duplicate: an unsaved copy, kept as a new workflow until Save ──
     Only a saved workflow has something to copy (the menu says "Not saved yet" otherwise). The
     copy is held until the new, empty workflow has loaded, then lands as ONE load: its steps
     and a fresh unsaved meta together, so it is not two undo steps and nothing of the source's
     meta survives. A copy still held when the editor goes anywhere but "new" is dropped. */
  const pendingCopy = useRef<{ spec: Spec; name: string; from: string | null } | null>(null);
  const duplicate = useCallback(() => {
    if (!meta.workflow_key) return;
    pendingCopy.current = { spec, name: `${meta.name} (copy)`,
                            from: meta.is_catalogue ? meta.workflow_key : meta.from_workflow_key };
    writeUrl({ wf: "new", step: null, v: null });
    setKey("new");
  }, [spec, meta]);
  useEffect(() => {
    if (key !== "new") pendingCopy.current = null;
  }, [key]);
  /* The Library's Duplicate opens `&wf=new&from=<key>`: the same unsaved copy, read from the
     source's running version. Reading it writes nothing. */
  const [copied, setCopied] = useState(0);
  useEffect(() => {
    let from: string | null = null;
    try { from = new URLSearchParams(window.location.search).get("from"); } catch { /* not in a browser */ }
    if (workflowKey !== "new" || !from) return;
    let live = true;
    fetchSpec(projectId, from).then((got) => {
      if (!live) return;
      pendingCopy.current = { spec: got.spec, name: `${got.meta.name} (copy)`,
                              from: got.meta.is_catalogue ? got.meta.workflow_key : got.meta.from_workflow_key };
      writeUrl({ from: null });
      setCopied((n) => n + 1);
    }, (e) => { if (live) toast("error", String((e as Error)?.message ?? e)); });
    return () => { live = false; };
  }, [workflowKey, projectId]);
  useEffect(() => {
    const copy = pendingCopy.current;
    if (!copy || loading || key !== "new" || meta.workflow_key !== null) return;
    pendingCopy.current = null;
    dispatch({ type: "LOAD", spec: copy.spec, meta: newMeta({ name: copy.name, from_workflow_key: copy.from }) });
  }, [loading, key, meta.workflow_key, dispatch, copied]);

  const deleteThis = useCallback(async () => {
    if (!meta.definition_id) return;
    try {
      const out = await deleteWorkflow(projectId, meta.definition_id);
      toast("success", out.outcome === "retired" ? "Retired" : "Deleted");
      onBack();
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      toast("error", /live run/i.test(message) ? "Has live runs" : message);
    }
  }, [projectId, meta.definition_id, onBack]);

  const describe = useCallback(async (text: string) => {
    try {
      const out = await draftFromDescription(projectId, text);
      dispatch({ type: "SET_SPEC", spec: out.spec });
      if (out.name && !meta.definition_id) dispatch({ type: "SET_META", patch: { name: out.name } });
    } catch (e) {
      if (isNoModel(e)) setDescribeBlocked(NO_MODEL_TIP);
      else toast("error", e instanceof Error ? e.message : "Could not draft steps");
      throw e;
    }
  }, [projectId, dispatch, meta.definition_id]);

  /* ── Keys ── */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const k = e.key.toLowerCase();
      if (mod && k === "s") { e.preventDefault(); if (publishGate.allowed) void saveFlow.save(); return; }
      if (typing(e.target) || e.defaultPrevented) return;
      if (mod && k === "z") { e.preventDefault(); dispatch({ type: e.shiftKey ? "REDO" : "UNDO" }); return; }
      if (mod && k === "y") { e.preventDefault(); dispatch({ type: "REDO" }); return; }
      if (mod && k === "d") {
        e.preventDefault();
        if (state.selection && !readOnly) dispatch({ type: "DUPLICATE", id: state.selection });
        return;
      }
      if (mod || e.altKey) return;
      if (k === "f" && view === "visual") { setZoom("fit"); return; }
      if (k === "delete" || k === "backspace") {
        const active = document.activeElement as HTMLElement | null;
        const node = active?.closest<HTMLElement>(".wf-lane .wf-node[data-step-id]");
        const id = node?.dataset.stepId ?? (!active || active === document.body ? state.selection : null);
        if (id) { e.preventDefault(); remove(id); }
        return;
      }
      if (k === "escape" && (state.selection || settingsOpen)) closeDrawer();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [saveFlow, publishGate.allowed, dispatch, state.selection, readOnly, view, remove, settingsOpen, closeDrawer]);

  /* ── Render ── */
  const gapAction = useCallback((at: number) => (
    <button type="button" className="wf-gap-add" aria-label="Add a step here" data-tip="Add a step"
            onClick={(e) => setQuick({ at, anchor: e.currentTarget })} data-testid={`wf-gap-add-${at}`}>
      <Plus size={12} weight="bold" aria-hidden />
    </button>
  ), []);

  if (loading) {
    return <div className="wf-editor" data-loading="true" aria-busy="true" data-testid="wf-editor">
      <div className="wf-topbar wf-skeleton" />
      <div className="wf-body wf-skeleton" />
    </div>;
  }

  const noSteps = !spec.steps.length;

  return (
    <div className="wf-editor" data-view={view} data-inspector={inspectorOpen ? "open" : undefined}
         data-testid="wf-editor">
      <TopBar state={state} dirty={dirty} view={view} readOnly={readOnly}
              onView={setView} onBack={() => saveFlow.guardLeave(onBack)}
              onName={(name) => dispatch({ type: "SET_META", patch: { name } })}
              nameError={saveFlow.nameError || null}
              autoFocusName={key === "new" && !meta.definition_id && !pendingCopy.current}
              canUndo={state.past.length > 0} canRedo={state.future.length > 0}
              onUndo={() => dispatch({ type: "UNDO" })} onRedo={() => dispatch({ type: "REDO" })}
              loadVersions={loadVersions} onOpenVersion={(v) => void openVersion(v)}
              onRestoreVersion={restoreVersion} onLeaveVersion={leaveVersion}
              onOpenCopy={() => {
                if (!meta.copy) return;
                writeUrl({ wf: meta.copy.workflow_key, step: null, v: null }, true);
                setKey(meta.copy.workflow_key);
              }}
              onDescribe={describe} describeBlocked={!draftGate.allowed ? draftGate.reason : describeBlocked}
              onCheck={() => void saveFlow.check()} checking={saveFlow.checking}
              checkBlocked={noSteps ? "Add a step first" : null}
              onStart={saveFlow.start} startRef={saveFlow.startRef}
              onTestRun={() => setTesting(true)}
              testRunBlocked={!meta.workflow_key || !meta.definition_id ? "Save the workflow first"
                : noSteps ? "Add a step first" : null}
              startBlocked={readOnly ? `Viewing v${meta.readOnlyVersion}` : noSteps ? "Add a step first" : null}
              onSave={() => void saveFlow.save()} saveLabel={saveFlow.label} saveRef={saveFlow.saveRef}
              saveBlocked={!publishGate.allowed ? publishGate.reason : saveFlow.canSave ? null : saveFlow.saving ? "Saving…" : saveFlow.saveTip}
              saveTip={saveFlow.saveTip} saving={saveFlow.saving}
              onDuplicate={duplicate} duplicateBlocked={meta.workflow_key ? null : "Not saved yet"}
              onUseFor={openSettings} onChanges={showChanges}
              onDiscard={() => dispatch({ type: "LOAD", spec: state.loaded ?? { steps: [] }, meta: state.loadedMeta })}
              onDelete={() => void deleteThis()}
              deleteBlocked={!deleteGate.allowed ? deleteGate.reason : meta.is_catalogue ? NO_DELETE_TIP : !meta.definition_id ? "Not saved yet" : null} />
      {saveFlow.popover}
      {testing && meta.workflow_key ? (
        <TestRunPanel projectId={projectId} workflowKey={meta.workflow_key}
                      version={meta.readOnlyVersion ?? meta.version ?? null}
                      steps={(state.loaded ?? spec).steps} capabilities={palette?.capabilities ?? []}
                      dirty={dirty} onClose={() => setTesting(false)}
                      onStarted={(runId) => {
                        setTesting(false);
                        toast("success", `Test run ${runId} started. It is listed under test runs.`);
                      }} />
      ) : null}

      {restore ? (
        <div className="wf-notice" role="status" data-testid="wf-restore-offer">
          <span>Restore unsaved changes?</span>
          <button type="button" className="wf-link" onClick={restore.apply} data-testid="wf-restore-apply">Restore</button>
          <button type="button" className="wf-link" onClick={restore.discard} data-testid="wf-restore-discard">Discard</button>
          {restore.publishedSince !== null ? (
            <>
              <span className="wf-notice-sep" aria-hidden>·</span>
              <span>v{restore.publishedSince} was published since</span>
              <button type="button" className="wf-link" data-tip="Restore, then see the changes in JSON"
                      onClick={() => { restore.apply(); showChanges(); }}>
                Compare
              </button>
            </>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <div className="wf-notice" data-tone="red" role="alert" data-tip={error} data-testid="wf-load-error">
          <span>Could not load everything</span>
          <button type="button" className="wf-link" onClick={() => void refreshPalette()}>Retry</button>
        </div>
      ) : null}

      <SummaryChips state={state} views={views} typicalMinutes={typicalMinutes}
                    onSettings={openSettings} onIssues={selectFirstProblem} />

      <div className="wf-body" ref={bodyRef} data-palette={rail ? "rail" : "open"}>
        {view === "visual" ? (
          <>
            <StepPalette palette={palette} onPick={pick} collapsed={rail} onToggle={togglePalette}
                         disabled={readOnly}
                         onNewStep={() => setDefining({})} onEditStep={(edit) => setDefining({ edit })} />
            <main className="wf-stage" aria-label="Flow">
              <StartsWith schema={spec.input_schema} dispatch={dispatch} readOnly={readOnly} />
              {noSteps ? (
                <EmptyState projectId={projectId} dispatch={dispatch} readOnly={readOnly}
                            onAddFirst={(anchor) => setQuick({ at: -1, anchor })} />
              ) : null}
              <FlowLane spec={spec} palette={palette} mode="design" readOnly={readOnly}
                        selection={state.selection} problems={state.problems}
                        onSelect={(id) => { dispatch({ type: "SELECT", id }); if (id) setSettingsOpen(false); }}
                        onInsert={insert} onMove={move} renderGapAction={readOnly ? undefined : gapAction}
                        onPlace={(id, offset) => dispatch({ type: "PLACE", id, offset })}
                        onResetLayout={() => dispatch({ type: "RESET_LAYOUT" })}
                        onRewire={(link, target) => void rewire(link, target)}
                        zoom={zoom} onZoomChange={setZoom} occludedRight={inspectorOpen ? occluded : 0} />
            </main>
          </>
        ) : (
          <main className="wf-stage wf-stage-json" aria-label="JSON">
            <JsonView state={state} palette={palette} dispatch={dispatch}
                      changes={jsonChanges} onChanges={setJsonChanges} />
          </main>
        )}
        {inspectorOpen && palette ? (
          <div className="wf-drawer" data-testid="wf-drawer">
            <Inspector state={state} palette={palette} dispatch={dispatch} projectId={projectId} actor={actor}
                       refreshPalette={refreshPalette}
                       usedFor={usedFor} onSchedule={onSchedule} onClose={closeDrawer}
                       onRewire={(link, target) => void rewire(link, target)} />
          </div>
        ) : null}
      </div>

      {view === "visual" && !readOnly ? (
        <button type="button" className="wf-fab" aria-label="Add a step" onClick={() => setSheet(true)}
                data-testid="wf-fab">
          <Plus size={20} weight="bold" aria-hidden />
        </button>
      ) : null}
      {sheet ? (
        <div className="wf-sheet" role="dialog" aria-label="Add a step" data-testid="wf-sheet"
             onKeyDown={(e) => { if (e.key === "Escape") setSheet(false); }}>
          <div className="wf-sheet-scrim" onClick={() => setSheet(false)} />
          <div className="wf-sheet-card">
            <StepPalette palette={palette} onPick={pick} sheet disabled={readOnly}
                         onNewStep={() => { setSheet(false); setDefining({}); }}
                         onEditStep={(edit) => { setSheet(false); setDefining({ edit }); }} />
          </div>
        </div>
      ) : null}

      <QuickAdd palette={palette} anchor={quick?.anchor ?? null} open={!!quick}
                onPick={(item) => { if (quick) insert(quick.at, item); setQuick(null); }}
                onClose={() => setQuick(null)} />

      <DefineStepModal projectId={projectId} edit={defining?.edit} open={!!defining} actor={actor}
                       onClose={(created) => {
                         const wasNew = !defining?.edit;
                         setDefining(null);
                         if (!created) return;
                         // The palette learns the step first, so the new node is named by it.
                         if (wasNew) pendingStep.current = created.step_key;
                         void refreshPalette();
                       }} />

      {note ? (
        <div className="wf-toast" role="status" data-testid="wf-toast" key={note.id}>
          <span>{note.message.replace(/ · Undo$/, "")}</span>
          {note.undo ? (
            <button type="button" className="wf-link" data-testid="wf-toast-undo"
                    onClick={() => { dispatch({ type: "UNDO" }); setNote(null); }}>Undo</button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
