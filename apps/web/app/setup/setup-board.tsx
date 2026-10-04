"use client";

/**
 * Repo and models — one board, one state.
 *
 * WHY THIS REPLACED setup-ui.tsx. The old page was forms that performed actions, so its state was
 * whatever the last form said: eight GETs on load, five verdicts that disagreed, a paragraph per
 * patch. This renders ONE server view (GET /v1/setup) and replaces it whole after every write; the
 * server decides every pill. While setup is unfinished the board runs as a stepper —
 * 1 Repository, 2 Model, 3 Try it — and after the first pull request it is a board.
 *
 * Deep links: `?area=setup&focus=repository|models|jobs|budget|try`, `&add=<provider>` opens Add a
 * model on that provider, `&source=<id>` opens that model's menu, legacy `step=github|model` maps
 * to focus. Rows whose last check is over a day old are re-checked in the background on open.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowClockwise } from "@phosphor-icons/react";

import { fetchSetup, SetupFailure, setupWrite } from "../../lib/setup-client";
import { readParams, rowMode, stepNumber, type SetupParams } from "../../lib/setup-stepper";
import type { RowKey, SetupView } from "../../lib/setup-types";
import { BudgetRow } from "./budget-row";
import { JobsRow } from "./jobs-row";
import { ModelsRow } from "./models-row";
import { BoardContext, toast } from "./parts";
import { RepositoryRow } from "./repository-row";
import { TryRow } from "./try-row";

function staleTargets(view: SetupView): string[] {
  return [
    ...view.repository.repos.filter((r) => r.stale).map((r) => `repository:${r.binding_id ?? "install"}`),
    ...view.models.filter((m) => m.stale).map((m) => `model:${m.source_id}`),
  ];
}

function currentParams(): SetupParams {
  try { return readParams(window.location.search); } catch { return { focus: null, add: null, source: null }; }
}

export function SetupBoard({ projectId, initialView = null }: {
  projectId: string; actor?: string; initialView?: SetupView | null;
}) {
  const [view, setView] = useState<SetupView | null>(initialView);
  const [error, setError] = useState<string | null>(null);
  const [checking, setCheckingState] = useState<Set<string>>(new Set());
  const [params, setParams] = useState<SetupParams>({ focus: null, add: null, source: null });
  const refs = useRef<Partial<Record<RowKey, HTMLElement | null>>>({});
  const autoChecked = useRef(false);

  const setChecking = useCallback((key: string, on: boolean) => {
    setCheckingState((prev) => {
      const next = new Set(prev);
      if (on) next.add(key); else next.delete(key);
      return next;
    });
  }, []);

  const load = useCallback(async () => {
    try {
      setView(await fetchSetup(projectId));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [projectId]);

  useEffect(() => { if (!initialView) void load(); }, [load, initialView]);

  // THE URL SAYS WHERE TO LAND, on first paint and on every in-console link (popstate).
  useEffect(() => {
    const read = () => setParams(currentParams());
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);
  useEffect(() => {
    if (!view || !params.focus) return;
    const el = refs.current[params.focus];
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [params.focus, view]);

  // STALE ROWS ARE RE-CHECKED IN THE BACKGROUND, once per open, so a token that expired or was
  // revoked overnight says so without anybody pressing Test.
  useEffect(() => {
    if (!view || autoChecked.current) return;
    autoChecked.current = true;
    const stale = staleTargets(view);
    if (!stale.length) return;
    stale.forEach((key) => setChecking(key, true));
    setupWrite<SetupView>(projectId, "checks", { target: "stale" }, { label: "Test all", background: true })
      .then(setView)
      .catch(() => undefined)
      .finally(() => stale.forEach((key) => setChecking(key, false)));
  }, [view, projectId, setChecking]);

  // A RUN IN FLIGHT IS WATCHED: every 5s while the page is visible.
  const running = view?.try.last_run?.pill.tone === "busy";
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [running, load]);

  // LEAVING MID-CHECK is asked about: the check finishes on the server, but its answer is lost.
  useEffect(() => {
    if (!checking.size) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [checking]);

  const ctx = useMemo(() => view ? {
    projectId, view, apply: setView, checking, setChecking,
  } : null, [projectId, view, checking, setChecking]);

  if (!view || !ctx) {
    return (
      <div className="su-board" data-testid="su-board">
        <header className="su-head"><h1>Repo and models</h1></header>
        {error ? <span className="su-pill su-tone-red" role="alert">{error}</span>
               : <span className="su-faint">Loading…</span>}
      </div>
    );
  }

  const testAll = async () => {
    setChecking("all", true);
    try {
      setView(await setupWrite<SetupView>(projectId, "checks", { target: "all" }, { label: "Test all" }));
    } catch (e) {
      toast((e as SetupFailure).refusal?.headline ?? "Check failed", "error");
    } finally { setChecking("all", false); }
  };
  const focus = params.focus;
  const ref = (row: RowKey) => (el: HTMLElement | null) => { refs.current[row] = el; };

  return (
    <BoardContext.Provider value={ctx}>
      <div className="su-board" data-testid="su-board" data-first-run={view.next !== null || undefined}>
        <header className="su-head">
          <h1>Repo and models</h1>
          <span className={`su-pill su-verdict su-tone-${view.verdict.tone}`} data-testid="su-verdict"
                title={view.verdict.tip} data-tip={view.verdict.tip}>
            {view.verdict.label}
          </span>
          <span className="su-spacer" />
          <button type="button" className="su-btn" data-testid="su-test-all"
                  disabled={checking.has("all")} onClick={testAll}
                  title="Re-checks GitHub access and asks each model one tiny question."
                  data-tip="Re-checks GitHub access and asks each model one tiny question.">
            <ArrowClockwise size={14} aria-hidden="true" className={checking.has("all") ? "su-spin" : undefined} />
            Test all
          </button>
        </header>
        <RepositoryRow ref={ref("repository")} mode={rowMode(view, "repository")}
                       step={stepNumber(view, "repository")} focused={focus === "repository"} />
        <ModelsRow ref={ref("models")} mode={rowMode(view, "models")}
                   step={stepNumber(view, "models")} focused={focus === "models"}
                   add={params.add} source={params.source}
                   onAddHandled={() => setParams((p) => ({ ...p, add: null }))} />
        <JobsRow ref={ref("jobs")} mode={rowMode(view, "jobs")} focused={focus === "jobs"} />
        <BudgetRow ref={ref("budget")} mode={rowMode(view, "budget")} focused={focus === "budget"} />
        <TryRow ref={ref("try")} mode={rowMode(view, "try")} step={stepNumber(view, "try")}
                focused={focus === "try"} />
      </div>
    </BoardContext.Provider>
  );
}
