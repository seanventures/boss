"use client";

/**
 * Factory (factory spec §4.1): the area shell — the tabs, the header, the URL.
 *
 *   Factory  [● Floor] [Apps 12]  ·  3 building · 2 need you  ·  [Find a build] [+ New build ▾]
 *
 * `?area=code_tasks` opens on the Floor; `view=apps` is the Apps tab; `build=<run>` opens that
 * build's cockpit full width over either tab (`run=` from older links is rewritten to it).
 * Esc or `← Floor` returns to the row that opened it. The object is a build; an app is what
 * builds make. The Floor polls `/v1/factory/floor` (8s, ETag); nothing here polls on its own.
 */
import { CaretDown, MagnifyingGlass } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useRef, useState } from "react";

import type { CodeTaskRun, PreviewRec, QaBenchmarkApp } from "@boss/api-client";
import { callAction } from "../lib/call-action";
import { useConsoleLive } from "../lib/console-live";
import { openPlan } from "../lib/converse-doors";
import { useFactoryFloor } from "../lib/factory-floor";
import { navigate } from "./agents/live-util";
import { notifyChanged } from "./changed";
import { showToast } from "./client-forms";
import { AppsTab } from "./factory/apps-tab";
import { Cockpit } from "./factory/cockpit";
import { startDirectChangeAction } from "./factory/floor-actions";
import { FloorView } from "./factory/floor";
import { isInternalRun } from "./factory/legacy-build-parts";
import { BenchmarkSheetBody } from "./factory/benchmark-sheet";

// Kept importable from here: tests and older callers read these from the area module.
export {
  DeploymentLine, appOrigin, failingGatesOf, fromInteractiveControl, isRetryable, latestPreviewFor,
  leadLine, pickBuiltRun,
} from "./factory/legacy-build-parts";

type View = "floor" | "apps";
type UrlState = { view: View; build: string | null };

/** Where the URL says the Factory is. `run=` (older links) reads as `build=`. */
export function readFactoryUrl(search: string): UrlState & { legacyRun: boolean; wantsNew: boolean } {
  const q = new URLSearchParams(search);
  const build = q.get("build") || q.get("run") || null;
  return {
    view: q.get("view") === "apps" ? "apps" : "floor",
    build,
    legacyRun: !q.get("build") && !!q.get("run"),
    wantsNew: q.get("new") === "1",
  };
}

function writeUrl(patch: Record<string, string | null>, mode: "push" | "replace"): void {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    if (mode === "push") window.history.pushState(null, "", url.toString());
    else window.history.replaceState(null, "", url.toString());
  } catch { /* not a browser */ }
}

function Sheet({ title, onClose, children, testId }: { title: string; onClose: () => void; children: React.ReactNode; testId: string }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); onClose(); } };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fx-sheet-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="fx-sheet" role="dialog" aria-modal="true" aria-label={title} data-testid={testId}>
        <header className="fx-sheet-head">
          <h2>{title}</h2>
          <button type="button" className="ag-act-close" aria-label="Close" onClick={onClose}>×</button>
        </header>
        {children}
      </section>
    </div>
  );
}

/** Direct change: an already-specified change goes straight to a build, no PRD first. */
function DirectChangeSheet({ projectId, actor, owner, repo, onClose, onStarted }: {
  projectId: string; actor: string; owner?: string; repo?: string;
  onClose: () => void; onStarted: (runId: string | null) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const first = useRef<HTMLInputElement>(null);
  useEffect(() => { first.current?.focus(); }, []);
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (busy) return;
    const form = new FormData(e.currentTarget);
    form.set("project_id", projectId);
    form.set("actor", actor);
    const [o, r] = String(form.get("repo_full") ?? "").split("/").map((x) => x.trim());
    if (o && r) { form.set("owner", o); form.set("repo", r); }
    setBusy(true);
    setError("");
    try {
      const res = await callAction(startDirectChangeAction)(form);
      if (res.ok) {
        showToast("success", "Build started");
        notifyChanged("code_tasks");
        onStarted(res.runId && res.runId.startsWith("crun_") ? res.runId : null);
        onClose();
      } else setError(res.error ?? "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title="Direct change" onClose={onClose} testId="fx-direct-sheet">
      <form className="fx-form" onSubmit={(e) => void submit(e)}>
        <label className="fx-field"><span>Name</span>
          <input ref={first} name="title" required className="cap-input" data-testid="fx-direct-title" />
        </label>
        <label className="fx-field"><span>What to change</span>
          <textarea name="instructions" required rows={4} className="cap-input" data-testid="fx-direct-what" />
        </label>
        <label className="fx-field"><span title="One check per line">Done when</span>
          <textarea name="acceptance_criteria" rows={2} className="cap-input" />
        </label>
        <label className="fx-field"><span title="One command per line, run after the build">Test commands</span>
          <textarea name="test_commands" rows={2} className="cap-input" data-testid="fx-direct-tests"
                    placeholder="npm test" />
        </label>
        <label className="fx-field"><span title="Reviewers on the code review. Each signs its own work.">Reviewers</span>
          <select name="review_panel_size" className="cap-input" defaultValue="" data-testid="fx-direct-panel">
            <option value="">Default</option>
            <option value="1">1</option>
            <option value="2">2</option>
            <option value="3">3</option>
          </select>
        </label>
        <label className="fx-field"><span title="owner/repo">Repo</span>
          <input name="repo_full" className="cap-input" defaultValue={owner && repo ? `${owner}/${repo}` : ""} />
        </label>
        {error ? <span className="ag-att-error" role="alert" data-user-text>{error}</span> : null}
        <div className="fx-sheet-actions">
          <button type="submit" className="ag-btn ag-btn-primary" disabled={busy} aria-busy={busy || undefined}
                  data-testid="fx-direct-start">Start build</button>
          <button type="button" className="ag-link" onClick={onClose}>Cancel</button>
        </div>
      </form>
    </Sheet>
  );
}

function NewBuildButton({ open, setOpen, onPlan, onDirect, onBenchmark, hasBenchmarks }: {
  open: boolean; setOpen: (v: boolean) => void;
  onPlan: () => void; onDirect: () => void; onBenchmark: () => void; hasBenchmarks: boolean;
}) {
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); setOpen(false); } };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    box.current?.querySelector<HTMLElement>("[role=menuitem]")?.focus();
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open, setOpen]);
  function onMenuKey(e: React.KeyboardEvent) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = [...(box.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? [])];
    const at = items.indexOf(document.activeElement as HTMLElement);
    items[(at + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
  }
  const pick = (fn: () => void) => () => { setOpen(false); fn(); };
  return (
    <span className="fx-split" ref={box}>
      <button type="button" className="ag-btn ag-btn-primary fx-split-main" data-testid="fx-new-plan"
              title="Plan it with Juno: a PRD first, then the build" onClick={onPlan}>+ New build</button>
      <button type="button" className="ag-btn ag-btn-primary fx-split-more" data-testid="fx-new-build"
              aria-haspopup="menu" aria-expanded={open} aria-label="More ways to start a build"
              onClick={() => setOpen(!open)}>
        <CaretDown size={12} weight="bold" aria-hidden />
      </button>
      {open ? (
        <span className="fx-menu" role="menu" data-testid="fx-new-menu" onKeyDown={onMenuKey}>
          <button type="button" role="menuitem" onClick={pick(onPlan)} title="A PRD first, then the build">Plan it with Juno</button>
          <button type="button" role="menuitem" title="Pick a workflow to run"
                  onClick={pick(() => navigate({ area: "delivery_workflows", new: "1", build: null, run: null, view: null }))}>
            From a workflow
          </button>
          <button type="button" role="menuitem" onClick={pick(onDirect)} title="An already-specified change, no PRD"
                  data-testid="fx-new-direct">Direct change</button>
          {hasBenchmarks ? (
            <button type="button" role="menuitem" onClick={pick(onBenchmark)} title="Build a pinned example app">From a benchmark</button>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}

export function CodeTasksHome({ projectId, orgId, codeTasks, apiReachable, previews = [], benchmarks = [], actor = "executive_console" }: {
  projectId: string;
  orgId?: string;
  codeTasks: CodeTaskRun[];
  codeTaskTotal?: number;
  meetings?: unknown[];
  apiReachable: boolean;
  previews?: PreviewRec[];
  previewBlockedReason?: string;
  benchmarks?: QaBenchmarkApp[];
  actor?: string;
  githubPlane?: string | null;
  githubPlaneLabel?: string | null;
  githubFullName?: string | null;
}) {
  const [url, setUrl] = useState<UrlState>({ view: "floor", build: null });
  const [query, setQuery] = useState("");
  const [menu, setMenu] = useState(false);
  const [sheet, setSheet] = useState<null | "direct" | "benchmark">(null);
  const [launched, setLaunched] = useState<Record<string, { run_id?: string; error?: string; kind?: "meeting" | "code" | "existing" }>>({});
  const opener = useRef<{ runId: string; el: HTMLElement | null } | null>(null);
  const find = useRef<HTMLInputElement>(null);
  const live = useConsoleLive();
  const floor = useFactoryFloor(projectId, apiReachable, actor);
  const reloadFloor = floor.refresh;

  // THE URL IS THE STATE: read on mount and on every back/forward. `run=` becomes `build=`.
  useEffect(() => {
    const read = () => {
      const now = readFactoryUrl(window.location.search);
      if (now.legacyRun) writeUrl({ build: now.build, run: null }, "replace");
      if (now.wantsNew) { writeUrl({ new: null }, "replace"); setMenu(true); }
      setUrl({ view: now.view, build: now.build });
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  const openBuild = useCallback((runId: string, from: HTMLElement | null) => {
    opener.current = { runId, el: from };
    writeUrl({ build: runId, run: null }, "push");
    setUrl((u) => ({ ...u, build: runId }));
  }, []);

  const closeBuild = useCallback(() => {
    writeUrl({ build: null, run: null }, "push");
    setUrl((u) => ({ ...u, build: null }));
    const back = opener.current ?? (url.build ? { runId: url.build, el: null } : null);
    // FOCUS GOES BACK TO WHAT OPENED IT (walk D17) — the same element if it survived, else the
    // row now drawing that build (a Building row, a Stopped row, its card), once the Floor has
    // painted again. Tried over a few frames: the Floor re-renders after the URL changes.
    const target = (): HTMLElement | null => {
      if (back?.el?.isConnected) return back.el;
      if (!back) return null;
      const id = back.runId;
      return document.querySelector<HTMLElement>(
        [`[data-testid="fx-build-row-${id}"]`, `[data-testid="fx-stopped-${id}"]`, `[data-testid="fx-app-build-${id}"]`,
         `[data-att-id="build:${id}:gates"]`, `[data-att-id="build:${id}:budget"]`].join(", "));
    };
    const tryFocus = (left: number) => {
      const el = target();
      const focusable = el?.matches("button, a, [tabindex]") ? el : el?.querySelector<HTMLElement>(".fx-row-open, button, a") ?? el;
      if (focusable) { focusable.focus(); return; }
      if (left > 0) window.setTimeout(() => tryFocus(left - 1), 50);
    };
    window.setTimeout(() => tryFocus(6), 0);
  }, [url.build]);

  const setView = (view: View) => {
    writeUrl({ view: view === "apps" ? "apps" : null }, "replace");
    setUrl((u) => ({ ...u, view }));
  };

  // `/` finds a build.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = !!t?.closest?.("input, textarea, select, [contenteditable=true]");
      if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !url.build) {
        e.preventDefault();
        find.current?.focus();
      }
      // Esc inside the cockpit is the cockpit's own chain (drawer → filter → Floor): it calls
      // onClose last. Handling it here too raced the cockpit's listener and skipped the chain.
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [url.build, closeBuild]);

  const plan = useCallback(() => openPlan("pm", { from: "roster" }), []);
  const runs = codeTasks.filter((c) => !isInternalRun(c));
  const lastSpec = runs[0]?.spec;
  const counts = floor.data?.counts;
  const needs = counts?.needsYou ?? 0;

  if (!apiReachable) {
    return (
      <div className="fx" data-testid="fx">
        <div className="ag-live-alert" role="alert"><span>Can&apos;t reach BOSS</span></div>
      </div>
    );
  }

  return (
    <div className="fx" data-testid="fx">
      <header className="fx-head" hidden={!!url.build}>
        {/* The console title already says Factory (walk D19): this heading is for screen readers. */}
        <h2 className="ag-sr">Factory</h2>
        <span className="fx-tabs" role="tablist" aria-label="Factory views">
          <button type="button" role="tab" aria-selected={url.view === "floor"} className="fx-tab" data-testid="fx-view-floor"
                  title="What needs you and what's building" onClick={() => setView("floor")}>Floor</button>
          <button type="button" role="tab" aria-selected={url.view === "apps"} className="fx-tab" data-testid="fx-view-apps"
                  title="Every app, where it runs, how it got there" onClick={() => setView("apps")}>
            Apps{counts?.apps ? <span className="fx-tab-count">{counts.apps}</span> : null}
          </button>
        </span>
        {counts ? (
          <span className="fx-head-counts" data-testid="fx-head-counts">
            <span>{counts.building} building</span>
            {needs ? <><span aria-hidden> · </span><span className="fx-needs">{needs === 1 ? "1 needs you" : `${needs} need you`}</span></> : null}
          </span>
        ) : null}
        <span className="ag-spacer" />
        <label className="fx-find">
          <MagnifyingGlass size={12} weight="bold" aria-hidden />
          <span className="ag-sr">Find a build</span>
          <input ref={find} type="search" placeholder="Find a build" value={query} data-testid="fx-find"
                 title="Press / to search" onChange={(e) => setQuery(e.target.value)} />
        </label>
        <NewBuildButton open={menu} setOpen={setMenu} onPlan={plan} onDirect={() => setSheet("direct")}
                        onBenchmark={() => setSheet("benchmark")} hasBenchmarks={benchmarks.length > 0} />
      </header>

      {url.build ? (
        <Cockpit projectId={projectId} runId={url.build} actor={actor} orgId={orgId ?? null}
                 onClose={closeBuild} onOpenBuild={(id) => openBuild(id, opener.current?.el ?? null)} />
      ) : url.view === "apps" ? (
        <div role="tabpanel" aria-label="Apps">
          <AppsTab projectId={projectId} runs={runs} previews={previews} query={query}
                   onOpen={openBuild} onNew={plan} />
        </div>
      ) : (
        <div role="tabpanel" aria-label="Floor">
          <FloorView projectId={projectId} actor={actor} floor={floor.data} error={floor.error} loading={floor.loading}
                     refresh={reloadFloor} paused={!live} query={query} orgId={orgId ?? null}
                     onOpen={openBuild} onPlan={plan} onDirect={() => setSheet("direct")} onApps={() => setView("apps")} />
        </div>
      )}

      {sheet === "direct" ? (
        <DirectChangeSheet projectId={projectId} actor={actor} owner={lastSpec?.owner ?? undefined} repo={lastSpec?.repo ?? undefined}
                           onClose={() => setSheet(null)}
                           onStarted={(id) => { reloadFloor(); if (id) openBuild(id, null); }} />
      ) : null}
      {sheet === "benchmark" ? (
        <Sheet title="From a benchmark" onClose={() => setSheet(null)} testId="fx-benchmark-sheet">
          <BenchmarkSheetBody benchmarks={benchmarks} projectId={projectId} actor={actor}
                              builtIds={new Set(codeTasks
                                .map((c) => ((c.finalize_ctx ?? {}) as Record<string, unknown>).qa_benchmark as { id?: string } | undefined)
                                .map((b) => b?.id).filter((id): id is string => Boolean(id)))}
                              launched={launched}
                              onLaunched={(key, runId, error, kind) => { setLaunched((m) => ({ ...m, [key]: { run_id: runId, error, kind } })); reloadFloor(); }}
                              onOpen={(id) => { setSheet(null); openBuild(id, null); }}
                              owner={lastSpec?.owner ?? undefined} repo={lastSpec?.repo ?? undefined} />
        </Sheet>
      ) : null}
    </div>
  );
}
