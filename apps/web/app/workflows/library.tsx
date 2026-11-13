"use client";

/**
 * The Library: every workflow this project can run, one card design, one list.
 *
 * It replaces three tabs that each held part of the answer: Yours (cards with a step list),
 * Catalogue (a different card with a rail and "Fork to edit") and Your steps. The same workflow
 * looked different depending on which tab you met it on, and the 54 "(copy)" definitions that
 * forking left behind were visible nowhere. Here:
 *
 *   - your workflows come first, then a thin Standards divider and what BOSS ships;
 *   - two or more copies of one source collapse behind "+n copies" on the source card, so they
 *     can be seen and deleted without burying your own work;
 *   - a card opens the editor (`&wf=`), Start opens the Runs composer, and nothing here
 *     writes a workflow: Duplicate and New open an unsaved draft, and only the editor saves.
 *
 * Words are labels; anything explanatory is a tooltip (spec section 4).
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  CalendarBlank, Check, Clock, CornersOut, DotsThree, ListNumbers, LockSimple, MagnifyingGlass, PencilSimple,
  Play, Plus, PuzzlePiece, Sparkle, Trash, Warning,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { agentMeta } from "../agent-chip";
import { onChanged } from "../changed";
import { formatDateTime } from "../../lib/datetime";
import { openDockAbout } from "../../lib/dock-subject";
import { missingRequired, workflowInputFields } from "../../lib/workflow-inputs";
import { ReadinessPanel, unaccepted, useWorkflowReadiness } from "../workflow-readiness-ui";
import { StepIcon, hasStepIcon } from "../step-icons";
import {
  DefineStepModal, MiniLane, NameInUseError, deleteWorkflow, fetchLibrary, renameWorkflow,
  type CustomStepDef, type WorkflowSummary,
} from "./studio-parts";
import type { CustomStep } from "../delivery-workflows-ui";
import { useAreaKeydown } from "../../lib/area-keys";

/* ── Schedules (moved here with the Yours tab they lived on) ─────────────────────── */

/** When a workflow runs without anybody asking. `describes` is rendered by the API so the
 *  console cannot word a schedule differently from the thing that fires it. */
export type Schedule = {
  id: string;
  workflow_key: string;
  name: string;
  describes: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_error: string | null;
  cadence?: string;
  minute?: number;
  hour?: number;
  weekday?: number | null;
  timezone?: string;
  input?: Record<string, unknown>;
  /** What was missing the last time a firing was refused, each with its fix. */
  last_missing?: { key: string; label: string; fix?: string; area?: string | null }[];
  /** Readiness keys this schedule may start without (the unattended Start anyway). */
  start_anyway?: string[];
  /** BOSSAPP-877 routine rules. */
  cron_expr?: string | null;
  concurrency_policy?: Concurrency;
  catch_up?: CatchUp;
  catch_up_max?: number;
  activity_gate?: { kinds: string[]; min_events: number } | null;
};

export type Concurrency = "always_enqueue" | "skip_if_active" | "coalesce";
export type CatchUp = "none" | "one" | "all";
export const CONCURRENCY_WORDS: Record<Concurrency, string> = {
  always_enqueue: "Start anyway", skip_if_active: "Skip it", coalesce: "Run once after",
};
export const CATCH_UP_WORDS: Record<CatchUp, string> = { none: "Skip missed", one: "Run once", all: "Run each (capped)" };
export type Firing = { id: string; scheduled_for: string; outcome: string; reason: string | null; run_id: string | null };
export const FIRING_REASON_WORDS: Record<string, string> = {
  run_active: "last run still going", no_activity: "nothing happened", missed: "missed",
  caught_up_once: "caught up once", catch_up_cap: "over the catch-up cap", not_ready: "not ready",
  authority_ended: "authority ended",
};

/** A five-field cron, said wrong before it is sent ("" is fine: not cron). */
export function cronProblem(expr: string): string | null {
  const t = expr.trim();
  if (!t) return null;
  const parts = t.split(/\s+/);
  if (parts.length !== 5) return `Cron has five fields (minute hour day month weekday); this has ${parts.length}.`;
  if (!parts.every((p) => /^[\w*,\-/]+$/.test(p))) return "Use numbers, names, *, -, / and commas.";
  return null;
}

export type ScheduleDraft = {
  workflowKey: string;
  name: string;
  scheduleId: string | null;
  timezone: string;
  values: Record<string, string>;
  /** Missing things the operator accepted this schedule may start without. */
  startAnyway: string[];
  /** BOSSAPP-877: "" = the weekly default; otherwise a five-field cron. */
  cron: string;
  concurrency: Concurrency;
  catchUp: CatchUp;
  /** Skip a firing when nothing happened in the project since the last run. */
  gate: boolean;
};

function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function scheduleFormValues(input: Record<string, unknown> | undefined): Record<string, string> {
  const values: Record<string, string> = {};
  for (const [key, value] of Object.entries(input ?? {})) {
    if (value == null) continue;
    values[key] = typeof value === "string" ? value : String(value);
  }
  return values;
}

export function scheduleDraftFor(item: { workflow_key: string; name: string },
                                 existing?: Schedule): ScheduleDraft {
  return {
    workflowKey: item.workflow_key,
    name: existing?.name ?? item.name,
    scheduleId: existing?.id ?? null,
    timezone: existing?.timezone || browserTimeZone(),
    values: scheduleFormValues(existing?.input),
    startAnyway: existing?.start_anyway ?? [],
    cron: existing?.cron_expr ?? "",
    concurrency: existing?.concurrency_policy ?? "always_enqueue",
    catchUp: existing?.catch_up ?? "one",
    gate: Boolean(existing?.activity_gate),
  };
}

/* ── Grouping ─────────────────────────────────────────────────────────────────────── */

export type LibraryEntry = { item: WorkflowSummary; copies: WorkflowSummary[] };

/**
 * Yours first, then the standards, with repeated copies collapsed under their source.
 *
 * A source with ONE copy keeps that copy in Yours: it is simply the operator's version of the
 * standard, and hiding the only workflow they made behind a "+1" would empty their own list.
 * Two or more copies of one source are the "(copy) (copy)" pile that forking left behind; those
 * collapse behind the source card, where they can be compared and deleted together.
 */
export function groupLibrary(items: WorkflowSummary[]): { yours: LibraryEntry[]; standards: LibraryEntry[] } {
  const byKey = new Map(items.map((w) => [w.workflow_key, w]));
  const copiesOf = new Map<string, WorkflowSummary[]>();
  for (const w of items) {
    const source = w.forked_from_workflow_key;
    if (w.is_catalogue || !source || !byKey.has(source) || source === w.workflow_key) continue;
    copiesOf.set(source, [...(copiesOf.get(source) ?? []), w]);
  }
  const grouped = new Set<string>();
  for (const [, copies] of copiesOf) {
    if (copies.length >= 2) copies.forEach((c) => grouped.add(c.workflow_key));
  }
  const entry = (w: WorkflowSummary): LibraryEntry => {
    const copies = copiesOf.get(w.workflow_key) ?? [];
    return { item: w, copies: copies.length >= 2 ? copies : [] };
  };
  const byName = (a: LibraryEntry, b: LibraryEntry) => a.item.name.localeCompare(b.item.name);
  return {
    yours: items.filter((w) => !w.is_catalogue && !grouped.has(w.workflow_key)).map(entry).sort(byName),
    standards: items.filter((w) => w.is_catalogue).map(entry).sort(byName),
  };
}

/** How long ago, in the fewest characters that still read. */
export function ageOf(iso: string | null | undefined, now: number | null): string {
  const at = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(at) || now === null) return "";
  const mins = Math.max(0, Math.round((now - at) / 60_000));
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days}d`;
  if (days < 60) return `${Math.round(days / 7)}w`;
  return `${Math.round(days / 30)}mo`;
}

/** `deleteWorkflow` failed because runs are still going (the API's 409). */
function isLiveRunsRefusal(error: unknown): boolean {
  const e = error as { status?: number; message?: string } | null;
  return e?.status === 409 || /live run|409/i.test(String(e?.message ?? ""));
}

function matches(w: WorkflowSummary, query: string): boolean {
  if (!query) return true;
  const hay = [w.name, w.description ?? "", w.workflow_key,
               ...w.steps.map((s) => `${s.title} ${s.captain ?? ""}`)].join(" ").toLowerCase();
  return query.toLowerCase().split(/\s+/).every((word) => hay.includes(word));
}

/** True while this element's console area is the one on screen (all areas stay mounted). */
function onScreen(el: HTMLElement | null): boolean {
  if (!el) return false;
  const area = el.closest("[data-area]")?.getAttribute("data-area");
  const active = document.querySelector("main[data-active]")?.getAttribute("data-active");
  return !area || !active || area === active;
}

/**
 * Closes on Escape or a press outside, like every other popover in the console.
 *
 * FOCUS GOES IN WHEN IT OPENS. Picking Schedule…, Use for… or Delete from the menu unmounted
 * the focused menu button, so the card's actions lost :focus-within and the open form faded out
 * the moment the pointer left the card, still clickable while invisible (review of ws/ia,
 * 2026-10-03). The container also says it is open (`data-open`), which the stylesheet keeps
 * visible; focus moving in is what a keyboard needs.
 */
function usePopover<T extends string>() {
  const [open, setOpen] = useState<T | null>(null);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const panel = ref.current?.querySelector<HTMLElement>(".wf-pop");
    if (panel && !panel.contains(document.activeElement)) {
      // A menu's first live item; a form's first field; a confirm itself, never its Delete.
      const first = panel.querySelector<HTMLElement>(
        '[role="menuitem"]:not(:disabled), input, select, textarea');
      if (!first && !panel.hasAttribute("tabindex")) panel.setAttribute("tabindex", "-1");
      (first ?? panel).focus({ preventScroll: true });
    }
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(null);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(null); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  return { open, setOpen, ref };
}

/* ── The Library ──────────────────────────────────────────────────────────────────── */

export type LibraryProps = {
  projectId: string;
  /** The area tabs (Runs / Library), drawn in the Library's own header row. */
  tabs?: ReactNode;
  busy: string | null;
  schedules: Schedule[];
  /** workflow_key → its start form, for the schedule popover's required inputs. */
  inputSchemas: Record<string, unknown>;
  /** workflow_key → the standard's own example brief, for "Try example". */
  examples: Record<string, { title?: string; directive?: string } | null | undefined>;
  /** The classes of work a workflow can be used for. */
  workClasses: string[];
  customSteps: CustomStep[];
  riskFloor: number;
  /** Standards BOSS ships that this project has not installed yet. */
  uninstalledStandards?: number;
  onInstallStandards?: () => void;
  onStart: (key: string, example?: { title?: string; directive?: string } | null) => void;
  onOpen: (key: string) => void;
  /** Open the workflow with the whole window (app/workflows/full-page.ts). */
  onOpenFull?: (key: string) => void;
  onNew: () => void;
  /** Open the editor on an unsaved copy of this workflow; nothing is written until Save. */
  onDuplicate: (key: string) => void;
  onUseFor: (key: string, workClass: string) => void;
  onSaveSchedule: (draft: ScheduleDraft) => Promise<boolean>;
  onToggleSchedule: (id: string, enabled: boolean) => void;
  onDeleteSchedule: (id: string) => void;
  onRemoveStep: (stepKey: string) => void;
  /** A step was defined or changed in the modal; the parent re-reads custom steps. */
  onStepsChanged: () => void;
  /** Open this workflow's schedule popover once its card is drawn (the editor's Schedule…). */
  scheduleFor?: string | null;
  /** The popover asked for by `scheduleFor` is open; the parent can forget the request. */
  onScheduleShown?: () => void;
};

type LibView = "workflows" | "steps";

function readLibView(): LibView {
  try {
    return new URLSearchParams(window.location.search).get("lib") === "steps" ? "steps" : "workflows";
  } catch { return "workflows"; }
}

export function Library(props: LibraryProps) {
  const { projectId, tabs, onNew } = props;
  const [items, setItems] = useState<WorkflowSummary[] | null>(null);
  const [failed, setFailed] = useState("");
  const [query, setQuery] = useState("");
  const [view, setViewState] = useState<LibView>("workflows");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [liveRuns, setLiveRuns] = useState<Set<string>>(new Set());
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  // THE CLOCK IS READ AFTER MOUNT, never during render: the server and the browser would print
  // different ages and React would tear the card out on hydration.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(t);
  }, []);
  const searchRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await fetchLibrary(projectId));
      setFailed("");
    } catch (e) {
      setFailed(String((e as Error)?.message ?? e));
    }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);
  // Saved in the editor, deleted in the dock, started from another screen: re-read.
  useEffect(() => onChanged(() => { void load(); }, "library"), [load]);

  useEffect(() => {
    const sync = () => setViewState(readLibView());
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const setView = (next: LibView) => {
    setViewState(next);
    try {
      const url = new URL(window.location.href);
      if (next === "steps") url.searchParams.set("lib", "steps"); else url.searchParams.delete("lib");
      window.history.replaceState(null, "", `${url.pathname}${url.search}`);
    } catch { /* non-browser */ }
  };

  // `/` finds, `N` starts a new workflow, only while this area is the one on screen and the
  // operator is not typing somewhere.
  useAreaKeydown((e: KeyboardEvent) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
    const t = e.target as HTMLElement | null;
    if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
    if (!onScreen(rootRef.current)) return;
    if (e.key === "/") { e.preventDefault(); setView("workflows"); searchRef.current?.focus(); }
    else if (e.key === "n" || e.key === "N") { e.preventDefault(); onNew(); }
  });

  const shown = useMemo(() => (items ?? []).filter((w) => matches(w, query.trim())), [items, query]);
  const groups = useMemo(() => groupLibrary(shown), [shown]);
  const usedIn = useMemo(() => {
    const counts = new Map<string, number>();
    for (const w of items ?? []) {
      const keys = new Set(w.steps.map((s) => s.capability).filter(Boolean) as string[]);
      for (const k of keys) counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    return counts;
  }, [items]);

  const toggle = (id: string) => setSelected((cur) => {
    const next = new Set(cur);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const removeOne = async (w: WorkflowSummary): Promise<boolean> => {
    try {
      await deleteWorkflow(projectId, w.definition_id);
      return true;
    } catch (e) {
      if (isLiveRunsRefusal(e)) setLiveRuns((cur) => new Set(cur).add(w.definition_id));
      else setFailed(String((e as Error)?.message ?? e));
      return false;
    }
  };

  const bulkDelete = async () => {
    setBulkBusy(true);
    const doomed = (items ?? []).filter((w) => selected.has(w.definition_id) && !w.is_catalogue);
    const left = new Set<string>();
    for (const w of doomed) if (!(await removeOne(w))) left.add(w.definition_id);
    setSelected(left);
    setBulkConfirm(false);
    setBulkBusy(false);
    await load();
  };

  const cardProps = {
    ...props, now, selected, liveRuns, onToggle: toggle, onDelete: async (w: WorkflowSummary) => {
      if (await removeOne(w)) await load();
    }, onRenamed: load,
  };

  return (
    <div className="wf-library" ref={rootRef} data-testid="wf-library">
      <header className="wf-lib-head">
        {tabs}
        <span className="wf-lib-spacer" />
        {view === "workflows" ? (
          <label className="wf-lib-search">
            <MagnifyingGlass size={14} aria-hidden />
            <input ref={searchRef} type="search" value={query} placeholder="Find a workflow"
                   aria-label="Find a workflow" data-testid="wf-lib-search"
                   onChange={(e) => setQuery(e.target.value)} />
          </label>
        ) : null}
        <div className="wf-seg" role="tablist" aria-label="Library view">
          {(["workflows", "steps"] as const).map((v) => (
            <button key={v} type="button" role="tab" aria-selected={view === v}
                    data-active={view === v} data-testid={`wf-lib-view-${v}`}
                    onClick={() => setView(v)}>
              {v === "workflows" ? "Workflows" : "Steps"}
            </button>
          ))}
        </div>
        <button type="button" className="wf-btn wf-btn-ghost" data-testid="wf-lib-ask"
                title="Describe a workflow or a change, and BOSS drafts it"
                onClick={() => openDockAbout({
                  subject: null, role: "program_manager",
                  // The dock's opener, not on-screen copy: it says what the agent can do.
                  question: "I want to work on this project's workflows. You can draft a new one"
                    + " from a described sequence of steps, change an existing one, define a custom"
                    + " step backed by a skill, or retire one. Start by telling me which"
                    + " workflows this project has and what each is for.",
                })}>
          <Sparkle size={14} aria-hidden /> Ask BOSS
        </button>
        <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-lib-new"
                title="New workflow (N)" onClick={onNew}>
          <Plus size={14} aria-hidden /> New workflow
        </button>
      </header>

      {failed ? (
        <p className="wf-lib-error" role="alert" data-testid="wf-lib-error">
          <Warning size={14} aria-hidden /> <span title={failed}>Could not load</span>
          <button type="button" className="wf-link" onClick={() => void load()}>Retry</button>
        </p>
      ) : null}

      {view === "steps" ? (
        <StepsTable {...props} usedIn={usedIn} />
      ) : items === null && !failed ? (
        <p className="wf-lib-muted" data-testid="wf-lib-loading">Loading…</p>
      ) : (
        <>
          {selected.size ? (
            <div className="wf-lib-selbar" role="status" data-testid="wf-lib-selbar">
              <span>{selected.size} selected</span>
              <span aria-hidden>·</span>
              {bulkConfirm ? (
                <>
                  <span>Delete {selected.size}?</span>
                  <button type="button" className="wf-btn wf-btn-danger" disabled={bulkBusy}
                          data-testid="wf-lib-bulk-confirm" onClick={() => void bulkDelete()}>
                    {bulkBusy ? "Deleting…" : "Delete"}
                  </button>
                  <button type="button" className="wf-link" onClick={() => setBulkConfirm(false)}>
                    Cancel
                  </button>
                </>
              ) : (
                <>
                  <button type="button" className="wf-link wf-link-danger"
                          data-testid="wf-lib-bulk-delete" onClick={() => setBulkConfirm(true)}>
                    Delete
                  </button>
                  <button type="button" className="wf-link" onClick={() => setSelected(new Set())}>
                    Clear
                  </button>
                </>
              )}
            </div>
          ) : null}

          {groups.yours.length ? (
            <div className="wf-lib-grid" data-testid="wf-lib-yours">
              {groups.yours.map((e) => <LibraryCard key={e.item.definition_id} entry={e} {...cardProps} />)}
            </div>
          ) : query.trim() ? null : (
            <div className="wf-lib-empty" data-testid="wf-lib-yours-empty">
              <span>No workflows of your own yet</span>
              <button type="button" className="wf-btn wf-btn-primary" onClick={onNew}>
                <Plus size={14} aria-hidden /> New workflow
              </button>
            </div>
          )}

          {groups.standards.length || props.uninstalledStandards ? (
            <>
              <div className="wf-lib-divider" data-testid="wf-lib-standards">
                <span>Standards</span>
                {props.uninstalledStandards && props.onInstallStandards ? (
                  <button type="button" className="wf-link" disabled={!!props.busy}
                          data-testid="wf-lib-install"
                          title={`${props.uninstalledStandards} standards are not installed in this project`}
                          onClick={props.onInstallStandards}>
                    Install {props.uninstalledStandards}
                  </button>
                ) : null}
              </div>
              <div className="wf-lib-grid">
                {groups.standards.map((e) => (
                  <LibraryCard key={e.item.definition_id} entry={e} {...cardProps} />
                ))}
              </div>
            </>
          ) : null}

          {query.trim() && !groups.yours.length && !groups.standards.length ? (
            <p className="wf-lib-muted" data-testid="wf-lib-no-match">No match</p>
          ) : null}
        </>
      )}
    </div>
  );
}

/* ── One card ─────────────────────────────────────────────────────────────────────── */

type CardProps = LibraryProps & {
  entry: LibraryEntry;
  now: number | null;
  selected: Set<string>;
  liveRuns: Set<string>;
  onToggle: (definitionId: string) => void;
  onDelete: (w: WorkflowSummary) => Promise<void>;
  onRenamed: () => void;
  nested?: boolean;
};

type CardPop = "menu" | "delete" | "schedule" | "usefor";

function LibraryCard(p: CardProps) {
  const { entry, nested } = p;
  const w = entry.item;
  const mine = !w.is_catalogue;
  const pop = usePopover<CardPop>();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(w.name);
  const [nameError, setNameError] = useState("");
  const [showCopies, setShowCopies] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // A PRESS THAT DISMISSES DOES NOTHING ELSE. The outside press closes a popover (or blurs the
  // rename field) on pointerdown, and React commits that before the click, so the card's own
  // "is anything open?" check was already false and a click meant to dismiss the menu opened
  // the editor instead (review of ws/ia, 2026-10-03). What was open is read when the press
  // BEGINS, in the capture phase, before anything closes.
  const pressDismisses = useRef(false);
  const askedForSchedule = mine && p.scheduleFor === w.workflow_key;
  const { onScheduleShown } = p;
  const { setOpen } = pop;
  useEffect(() => {
    if (!askedForSchedule) return;
    setOpen("schedule");
    onScheduleShown?.();
  }, [askedForSchedule, setOpen, onScheduleShown]);
  const retire = w.run_count > 0;
  const schedules = p.schedules.filter((s) => s.workflow_key === w.workflow_key);
  const agents = [...new Set(w.steps.map((s) => s.captain).filter(Boolean) as string[])];
  const approvals = w.steps.filter((s) => s.approval === "manual");
  const example = p.examples[w.workflow_key];

  const saveName = async () => {
    const next = name.trim();
    if (!next || next === w.name) { setRenaming(false); setName(w.name); return; }
    try {
      await renameWorkflow(p.projectId, w.definition_id, { name: next });
      setRenaming(false);
      setNameError("");
      p.onRenamed();
    } catch (e) {
      setNameError(e instanceof NameInUseError ? "Name in use" : String((e as Error)?.message ?? e));
    }
  };

  const stop = (e: React.SyntheticEvent) => e.stopPropagation();

  return (
    <article className="wf-card" data-mine={mine || undefined} data-nested={nested || undefined}
             data-selected={p.selected.has(w.definition_id) || undefined}
             data-testid={`wf-card-${w.workflow_key}`}
             onPointerDownCapture={() => { pressDismisses.current = renaming || pop.open !== null; }}
             onClick={() => {
               const dismissing = pressDismisses.current;
               pressDismisses.current = false;
               if (!dismissing && !renaming && !pop.open) p.onOpen(w.workflow_key);
             }}>
      {mine ? (
        <label className="wf-card-check" title="Select" onClick={stop}>
          <input type="checkbox" checked={p.selected.has(w.definition_id)}
                 aria-label={`Select ${w.name}`} data-testid={`wf-card-select-${w.workflow_key}`}
                 onChange={() => p.onToggle(w.definition_id)} />
        </label>
      ) : null}

      <header className="wf-card-head">
        {renaming ? (
          <span className="wf-card-rename" onClick={stop}>
            <input autoFocus value={name} aria-label="Name" data-testid={`wf-card-name-input-${w.workflow_key}`}
                   onChange={(e) => { setName(e.target.value); setNameError(""); }}
                   onKeyDown={(e) => {
                     if (e.key === "Enter") void saveName();
                     if (e.key === "Escape") { setRenaming(false); setName(w.name); setNameError(""); }
                   }}
                   onBlur={() => { if (!nameError) void saveName(); }} />
            {nameError ? <em className="wf-card-name-error" role="alert">{nameError}</em> : null}
          </span>
        ) : (
          <h4 className="wf-card-name" title={w.name} data-user-text>{w.name}</h4>
        )}
        <span className="wf-card-chips">
          {mine ? <span className="wf-tag" data-kind="yours">Yours</span> : (
            <span className="wf-tag" data-kind="standard"
                  title="Shipped with BOSS. Saving makes your own copy.">Standard</span>
          )}
          {w.version ? (
            <span className="wf-tag" title="Each save is a new version. Running work keeps its version.">
              v{w.version}
            </span>
          ) : null}
          {w.forked_from_workflow_key && mine ? (
            <span className="wf-tag" data-kind="from" data-user-text
                  title={w.forked_from_name ?? w.forked_from_workflow_key}>
              from {w.forked_from_name ?? w.forked_from_workflow_key}
            </span>
          ) : null}
        </span>
      </header>

      <p className="wf-card-desc" title={w.description || undefined} data-user-text>
        {w.description || "\u00a0"}
      </p>

      <div className="wf-card-lane"><MiniLane steps={w.steps} /></div>

      <footer className="wf-card-meta">
        <span title={`${w.step_count} steps`}><ListNumbers size={14} aria-hidden /> {w.step_count}</span>
        {agents.length ? (
          <span className="wf-card-agents" title={agents.map((a) => a.replace(/_/g, " ")).join(", ")}>
            {agents.slice(0, 3).map((a) => {
              const m = agentMeta(a);
              return <AgentAvatar key={a} agent={a} size={20} aiBadge={false} className={`mr-avatar tone-${m.tone} wf-avatar ag-face`} />;
            })}
            {agents.length > 3 ? <em>+{agents.length - 3}</em> : null}
          </span>
        ) : null}
        {approvals.length ? (
          <span title={`Needs approval: ${approvals.map((s) => s.title).join(", ")}`}>
            <LockSimple size={14} aria-hidden /> {approvals.length}
          </span>
        ) : null}
        {schedules.length ? (
          <span title={schedules.map((s) => s.describes + (s.enabled ? "" : " (paused)")).join(", ")}
                data-testid={`wf-card-scheduled-${w.workflow_key}`}>
            <CalendarBlank size={14} aria-hidden />
          </span>
        ) : null}
        {w.last_run ? (
          <span title={`Last run ${w.last_run.status}, ${formatDateTime(w.last_run.at)}`}>
            <Clock size={14} aria-hidden /> {ageOf(w.last_run.at, p.now)}
          </span>
        ) : null}
        {p.liveRuns.has(w.definition_id) ? (
          <em className="wf-card-live" role="alert" data-testid={`wf-card-live-${w.workflow_key}`}>
            Has live runs
          </em>
        ) : null}
        {entry.copies.length ? (
          <button type="button" className="wf-link wf-card-copies" aria-expanded={showCopies}
                  data-testid={`wf-card-copies-${w.workflow_key}`}
                  onClick={(e) => { stop(e); setShowCopies((v) => !v); }}>
            +{entry.copies.length} copies
          </button>
        ) : null}
      </footer>

      <div className="wf-card-actions" onClick={stop} ref={pop.ref} data-open={pop.open ?? undefined}>
        <button type="button" className="wf-btn wf-btn-sm" data-testid={`wf-card-start-${w.workflow_key}`}
                disabled={!w.version} title={w.version ? "Start a run" : "Save a version first"}
                onClick={() => p.onStart(w.workflow_key)}>
          <Play size={12} aria-hidden /> Start
        </button>
        <button type="button" className="wf-btn wf-btn-sm" data-testid={`wf-card-open-${w.workflow_key}`}
                onClick={() => p.onOpen(w.workflow_key)}>Open</button>
        {p.onOpenFull ? (
          <button type="button" className="wf-btn wf-btn-sm wf-btn-icon" aria-label={`Open ${w.name} full page`}
                  title="Open full page: the whole window for this workflow"
                  data-testid={`open-full-page-${w.workflow_key}`}
                  onClick={() => p.onOpenFull?.(w.workflow_key)}>
            <CornersOut size={14} aria-hidden />
          </button>
        ) : null}
        <button type="button" className="wf-btn wf-btn-sm wf-btn-icon" aria-label="More"
                aria-expanded={pop.open === "menu"} data-testid={`wf-card-more-${w.workflow_key}`}
                onClick={() => pop.setOpen(pop.open ? null : "menu")}>
          <DotsThree size={16} aria-hidden />
        </button>

        {pop.open === "menu" ? (
          <div className="wf-pop wf-menu" role="menu" data-testid={`wf-card-menu-${w.workflow_key}`}>
            {mine ? (
              <>
                <button type="button" role="menuitem" onClick={() => { pop.setOpen(null); p.onDuplicate(w.workflow_key); }}>
                  Duplicate
                </button>
                <button type="button" role="menuitem" onClick={() => { pop.setOpen(null); setRenaming(true); }}>
                  Rename
                </button>
                <button type="button" role="menuitem" disabled={!w.version}
                        title={w.version ? undefined : "Save a version first"}
                        onClick={() => pop.setOpen("schedule")}>Schedule…</button>
                <button type="button" role="menuitem" onClick={() => pop.setOpen("usefor")}>Use for…</button>
                <button type="button" role="menuitem" data-tone="danger"
                        data-testid={`wf-card-delete-${w.workflow_key}`}
                        title={retire ? "It has runs, so it is retired: history is kept." : undefined}
                        onClick={() => pop.setOpen("delete")}>
                  {retire ? "Retire" : "Delete"}
                </button>
              </>
            ) : (
              <>
                <button type="button" role="menuitem" onClick={() => { pop.setOpen(null); p.onOpen(w.workflow_key); }}>Open</button>
                {p.onOpenFull ? (
                  <button type="button" role="menuitem" onClick={() => { pop.setOpen(null); p.onOpenFull?.(w.workflow_key); }}>
                    Open full page
                  </button>
                ) : null}
                <button type="button" role="menuitem" onClick={() => { pop.setOpen(null); p.onStart(w.workflow_key); }}>Start</button>
                {example ? (
                  <button type="button" role="menuitem" data-testid={`wf-card-example-${w.workflow_key}`}
                          onClick={() => { pop.setOpen(null); p.onStart(w.workflow_key, example); }}>
                    Try example
                  </button>
                ) : null}
                <button type="button" role="menuitem" onClick={() => pop.setOpen("usefor")}>Use for…</button>
                <button type="button" role="menuitem" disabled title="Standards can't be deleted.">Delete</button>
              </>
            )}
          </div>
        ) : null}

        {pop.open === "delete" ? (
          <div className="wf-pop wf-card-confirm" role="dialog" aria-label="Confirm delete"
               data-testid={`wf-card-confirm-${w.workflow_key}`}>
            <span>{retire ? "Retire" : "Delete"} “<span data-user-text>{w.name}</span>”?</span>
            <button type="button" className="wf-btn wf-btn-danger wf-btn-sm" disabled={deleting}
                    data-testid={`wf-card-confirm-delete-${w.workflow_key}`}
                    onClick={async () => {
                      setDeleting(true);
                      await p.onDelete(w);
                      setDeleting(false);
                      pop.setOpen(null);
                    }}>
              {retire ? "Retire" : "Delete"}
            </button>
          </div>
        ) : null}

        {pop.open === "usefor" ? (
          <div className="wf-pop wf-usefor" role="dialog" aria-label="Use for">
            <span className="wf-pop-label" title="New work of this class runs this workflow">Use for</span>
            {p.workClasses.length ? p.workClasses.map((c) => (
              <button key={c} type="button" className="wf-link" data-active={w.used_for?.includes(c) || undefined}
                      onClick={() => { pop.setOpen(null); p.onUseFor(w.workflow_key, c); }}>
                {w.used_for?.includes(c) ? <Check size={12} aria-hidden /> : null} {c}
              </button>
            )) : <span className="wf-lib-muted">No classes</span>}
          </div>
        ) : null}

        {pop.open === "schedule" ? (
          <SchedulePopover workflow={w} schedules={schedules} projectId={p.projectId} busy={p.busy}
                           inputSchema={p.inputSchemas[w.workflow_key]}
                           onSave={async (d) => { if (await p.onSaveSchedule(d)) pop.setOpen(null); }}
                           onToggle={p.onToggleSchedule} onDelete={p.onDeleteSchedule} />
        ) : null}
      </div>

      {showCopies && entry.copies.length ? (
        <div className="wf-card-copylist" onClick={stop} data-testid={`wf-card-copylist-${w.workflow_key}`}>
          {entry.copies.map((c) => (
            <LibraryCard key={c.definition_id} {...p} entry={{ item: c, copies: [] }} nested />
          ))}
        </div>
      ) : null}
    </article>
  );
}

/* ── Schedule popover ─────────────────────────────────────────────────────────────── */

function SchedulePopover({ workflow, schedules, projectId, busy, inputSchema, onSave, onToggle, onDelete }: {
  workflow: WorkflowSummary; schedules: Schedule[]; projectId: string; busy: string | null;
  inputSchema: unknown;
  onSave: (draft: ScheduleDraft) => Promise<void>;
  onToggle: (id: string, enabled: boolean) => void;
  onDelete: (id: string) => void;
}) {
  const [draft, setDraft] = useState<ScheduleDraft>(() => scheduleDraftFor(workflow, schedules[0]));
  const [shown, setShown] = useState<{ id: string; firings: Firing[] } | null>(null);
  const showFirings = async (id: string) => {
    if (shown?.id === id) { setShown(null); return; }
    try {
      const r = await fetch(`/api/delivery-workflows?view=schedule_firings&project_id=${encodeURIComponent(projectId)}`
        + `&schedule_id=${encodeURIComponent(id)}`, { cache: "no-store" });
      const body = r.ok ? await r.json() as { firings?: Firing[] } : {};
      setShown({ id, firings: Array.isArray(body.firings) ? body.firings : [] });
    } catch { setShown({ id, firings: [] }); }
  };
  const cronSaid = cronProblem(draft.cron);
  // WHAT ITS FIRING WILL REFUSE, ASKED WHILE SAVING. A schedule runs with nobody at the
  // keyboard, so the readiness checklist is drawn here and what is ticked is stored with it.
  const readiness = useWorkflowReadiness(projectId, draft.workflowKey, draft.values);
  const fields = workflowInputFields(inputSchema);
  const missing = missingRequired(fields, draft.values);
  return (
    <form className="wf-pop wf-schedule" data-testid="wf-schedule"
          onSubmit={(e) => { e.preventDefault(); void onSave(draft); }}>
      {schedules.length ? (
        <ul className="wf-schedule-list">
          {schedules.map((s) => (
            <li key={s.id} data-enabled={s.enabled}>
              <span title={s.last_missing?.length
                // NOT STARTED, AND WHY, each missing item with its fix, as the schedule kept it.
                ? `Not started last time. Missing: ${s.last_missing.map((m) =>
                    `${m.label}${m.fix ? ` (${m.fix})` : ""}`).join("; ")}`
                : s.last_error ? `Last run failed: ${s.last_error}`
                : s.next_run_at ? `Next ${formatDateTime(s.next_run_at)}` : undefined}>
                <span data-user-text>{s.describes}</span>{s.enabled ? "" : ", paused"}
                {s.last_missing?.length || s.last_error ? (
                  <Warning size={12} aria-label="Did not start" data-testid={`wf-sched-refused-${s.id}`} />
                ) : null}
              </span>
              <GatedButton permission="workflow_schedule:manage" type="button" className="wf-link" disabled={!!busy}
                      data-testid={`wf-schedule-toggle-${s.id}`}
                      onClick={() => onToggle(s.id, !s.enabled)}>{s.enabled ? "Pause" : "Resume"}</GatedButton>
              <button type="button" className="wf-link" disabled={!!busy}
                      onClick={() => setDraft(scheduleDraftFor(workflow, s))}>Edit</button>
              <button type="button" className="wf-link" data-testid={`wf-schedule-firings-${s.id}`}
                      onClick={() => void showFirings(s.id)}>Firings</button>
              <GatedButton permission="workflow_schedule:manage" type="button" className="wf-link wf-link-danger" disabled={!!busy}
                      aria-label="Remove schedule" onClick={() => onDelete(s.id)}>
                <Trash size={12} aria-hidden />
              </GatedButton>
            </li>
          ))}
        </ul>
      ) : null}
      {shown ? (
        <ol className="wf-schedule-list" aria-label="Firings" data-testid="wf-schedule-firing-list">
          {shown.firings.length ? shown.firings.slice(0, 12).map((f) => (
            <li key={f.id} data-outcome={f.outcome}>
              <span>{formatDateTime(f.scheduled_for)} · {f.outcome === "started" ? "started" : f.outcome}
                {f.reason ? ` — ${FIRING_REASON_WORDS[f.reason] ?? f.reason}` : ""}</span>
            </li>
          )) : <li>No firings yet.</li>}
        </ol>
      ) : null}
      <details className="wf-field" data-testid="wf-schedule-rules">
        <summary title="Cron, what happens while the last run is going, catching up after downtime, and skipping quiet periods">Rules</summary>
        <label className="wf-field">
          <span title="Five fields: minute hour day month weekday. Empty: every Monday 08:00.">Cron</span>
          <input value={draft.cron} placeholder="0 9 * * 1-5" data-testid="wf-schedule-cron"
                 onChange={(e) => setDraft({ ...draft, cron: e.target.value })} />
        </label>
        {cronSaid ? <p className="wf-lib-muted" role="alert" data-testid="wf-schedule-cron-said">{cronSaid}</p> : null}
        <label className="wf-field">
          <span title="When it is due while its last run is still going">If still running</span>
          <select value={draft.concurrency} data-testid="wf-schedule-concurrency"
                  onChange={(e) => setDraft({ ...draft, concurrency: e.target.value as Concurrency })}>
            {(Object.keys(CONCURRENCY_WORDS) as Concurrency[]).map((k) => <option key={k} value={k}>{CONCURRENCY_WORDS[k]}</option>)}
          </select>
        </label>
        <label className="wf-field">
          <span title="Firings missed while BOSS was off">Missed</span>
          <select value={draft.catchUp} data-testid="wf-schedule-catch-up"
                  onChange={(e) => setDraft({ ...draft, catchUp: e.target.value as CatchUp })}>
            {(Object.keys(CATCH_UP_WORDS) as CatchUp[]).map((k) => <option key={k} value={k}>{CATCH_UP_WORDS[k]}</option>)}
          </select>
        </label>
        <label className="wf-field">
          <input type="checkbox" checked={draft.gate} data-testid="wf-schedule-gate"
                 onChange={(e) => setDraft({ ...draft, gate: e.target.checked })} />
          <span title="Work items, git, builds or documents changed since the last run">Skip when nothing happened</span>
        </label>
      </details>
      <label className="wf-field">
        <span title="Runs every Monday at 08:00 in this zone">Time zone</span>
        <input value={draft.timezone} data-testid="wf-schedule-zone"
               onChange={(e) => setDraft({ ...draft, timezone: e.target.value })} />
      </label>
      {fields.map((field) => (
        <label className="wf-field" key={field.name}>
          <span>{field.label}{field.required ? <i className="wf-req" title="Required">•</i> : null}</span>
          <input value={draft.values[field.name] ?? ""} data-testid={`wf-schedule-input-${field.name}`}
                 onChange={(e) => setDraft({ ...draft, values: { ...draft.values, [field.name]: e.target.value } })} />
        </label>
      ))}
      <ReadinessPanel readiness={readiness} accepted={draft.startAnyway} projectId={projectId}
                      onAccept={(keys) => setDraft({ ...draft, startAnyway: keys })} />
      {unaccepted(readiness, draft.startAnyway).length ? (
        <p className="wf-lib-muted" title="Each run is refused until these are added, or you tick Start anyway">
          <Warning size={12} aria-hidden /> Will be refused
        </p>
      ) : null}
      <button type="submit" className="wf-btn wf-btn-primary wf-btn-sm" data-testid="wf-schedule-save"
              disabled={!!busy || !draft.timezone.trim() || missing.length > 0 || Boolean(cronSaid)}
              title={missing.length ? `Needs ${missing.join(", ")}` : undefined}>
        {draft.scheduleId ? "Save schedule" : draft.cron.trim() ? "Schedule" : "Schedule weekly"}
      </button>
    </form>
  );
}

/* ── Steps view ───────────────────────────────────────────────────────────────────── */

/** The modal's shape for a step being edited. */
function asStepDef(c: CustomStep): CustomStepDef {
  return {
    step_key: c.step_key, label: c.label, summary: c.summary,
    binding_kind: c.binding_kind, skill_id: c.skill_id ?? undefined,
    mcp_tool_name: c.mcp_tool_name ?? undefined,
    inputs: c.inputs.map((i) => ({ name: i.name, required: i.required })),
    produces: c.produces, risk_tier: c.risk_tier, usable: c.usable,
    unusable_reason: c.unusable_reason ?? undefined,
    approval_forced_manual: c.approval_forced_manual,
  };
}

function StepsTable({ projectId, customSteps, riskFloor, busy, onRemoveStep, onStepsChanged, usedIn }:
    LibraryProps & { usedIn: Map<string, number> }) {
  const [modal, setModal] = useState<{ edit?: CustomStep } | null>(null);
  return (
    <section className="wf-libsteps" data-testid="wf-lib-steps">
      <div className="wf-libsteps-head">
        <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-lib-new-step"
                onClick={() => setModal({})}>
          <Plus size={14} aria-hidden /> New step
        </button>
      </div>
      {customSteps.length === 0 ? (
        <p className="wf-lib-muted" data-testid="wf-lib-steps-empty">No steps of your own yet</p>
      ) : (
        <table className="wf-libsteps-table">
          <tbody>
            {customSteps.map((c) => {
              const risk = !c.usable ? "bad" : c.risk_tier >= riskFloor ? "high" : "low";
              const n = usedIn.get(c.step_key) ?? 0;
              return (
                <tr key={c.step_key} data-usable={c.usable} data-testid={`wf-step-row-${c.step_key}`}>
                  <td className="wf-libsteps-icon">
                    {hasStepIcon(c.step_key) ? <StepIcon capability={c.step_key} size={16} />
                      : <PuzzlePiece size={16} aria-hidden />}
                  </td>
                  <td className="wf-libsteps-label" title={c.summary || undefined} data-user-text>
                    {c.label}
                  </td>
                  <td>
                    <span className="wf-tag" title={c.binding_kind === "skill" ? "Skill" : "MCP tool"}>
                      {c.binding_kind === "skill" ? c.skill_id : c.mcp_tool_name}
                    </span>
                  </td>
                  <td>
                    <span className="wf-risk" data-risk={risk}
                          title={!c.usable ? (c.unusable_reason ?? "Cannot run")
                            : c.approval_forced_manual ? `Risk ${c.risk_tier}, always needs a person`
                            : `Risk ${c.risk_tier}`} />
                  </td>
                  <td className="wf-libsteps-used" title="Workflows that place this step">Used in {n}</td>
                  <td className="wf-libsteps-act">
                    <GatedButton permission="workflow:create" type="button" className="wf-btn wf-btn-sm wf-btn-icon" aria-label={`Edit ${c.label}`}
                            disabled={!!busy} data-testid={`wf-step-edit-${c.step_key}`}
                            onClick={() => setModal({ edit: c })}>
                      <PencilSimple size={14} aria-hidden />
                    </GatedButton>
                    <GatedButton permission="workflow:create" type="button" className="wf-btn wf-btn-sm wf-btn-icon" aria-label={`Remove ${c.label}`}
                            disabled={!!busy || n > 0}
                            title={n > 0 ? "In use, take it out of those workflows first" : undefined}
                            data-testid={`wf-step-remove-${c.step_key}`}
                            onClick={() => onRemoveStep(c.step_key)}>
                      <Trash size={14} aria-hidden />
                    </GatedButton>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {modal ? (
        <DefineStepModal key={modal.edit?.step_key ?? "new"} projectId={projectId} open
                         edit={modal.edit ? asStepDef(modal.edit) : undefined}
                         onClose={(created) => { setModal(null); if (created) onStepsChanged(); }} />
      ) : null}
    </section>
  );
}
