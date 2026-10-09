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
 * Deep links: `?area=setup&focus=repository|models|jobs|routing|budget|try` (routing = jobs), `&add=<provider>` opens Add a
 * model on that provider, `&source=<id>` opens that model's menu, legacy `step=github|model` maps
 * to focus. Rows whose last check is over a day old are re-checked in the background on open.
 */
import { GatedButton, useGate } from "../../components/gated";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowClockwise } from "@phosphor-icons/react";

import { fetchSetup, SetupFailure, setupWrite } from "../../lib/setup-client";
import {
  readParams, rowMode, SETUP_BADGE_EVENT, stepNumber, summarize, type SetupParams,
} from "../../lib/setup-stepper";
import type { RowKey, SetupView } from "../../lib/setup-types";
import { BudgetRow } from "./budget-row";
import { CapacityRow } from "./capacity-row";
import { ModelsRow } from "./models-row";
import { BoardContext, toast } from "./parts";
import { Dot, localView } from "./pill";
import { GitHubAccessRow } from "./github-access-row";
import { RepositoryRow } from "./repository-row";
import { RoutingRow } from "./routing-row";
import { TryRow } from "./try-row";
import { PublicUrlRow } from "./public-url-row";
import { ToolchainsRow } from "./toolchains-row";
import { SharingAsk } from "../improve/sharing-ask";

function staleTargets(view: SetupView): string[] {
  return [
    ...view.repository.repos.filter((r) => r.stale).map((r) => `repository:${r.binding_id ?? "install"}`),
    ...view.models.filter((m) => m.stale).map((m) => `model:${m.source_id}`),
  ];
}

function currentParams(): SetupParams {
  try { return readParams(window.location.search); } catch { return { focus: null, add: null, source: null }; }
}

export function SetupBoard({ projectId, actor, initialView = null }: {
  projectId: string; actor?: string; initialView?: SetupView | null;
}) {
  const [rawView, setView] = useState<SetupView | null>(initialView);
  const view = useMemo(() => (rawView ? localView(rawView) : null), [rawView]);
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
  // AND STAYS THERE while the rows above it finish loading (Capacity reads its sources after
  // the first paint): a deep link landed on Model routing, then was pushed off screen. Until
  // the reader scrolls, types or touches, a row that moves is followed for a few seconds.
  const landedOn = useRef<string | null>(null);
  useEffect(() => {
    if (!view || !params.focus || landedOn.current === params.focus) return;
    const el = refs.current[params.focus];
    if (!el || typeof el.scrollIntoView !== "function") return;
    landedOn.current = params.focus;
    const reduce = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    let followed = true;
    const stop = () => { followed = false; };
    const events = ["wheel", "touchstart", "keydown", "pointerdown"] as const;
    events.forEach((e) => window.addEventListener(e, stop, { once: true, passive: true }));
    const timers = [400, 900, 1600, 2600, 4000].map((ms) => window.setTimeout(() => {
      if (!followed) return;
      const top = el.getBoundingClientRect().top;
      if (Math.abs(top - 80) > 24) el.scrollIntoView({ behavior: "auto", block: "start" });
    }, ms));
    return () => {
      timers.forEach((t) => window.clearTimeout(t));
      events.forEach((e) => window.removeEventListener(e, stop));
    };
  }, [params.focus, view]);

  // STALE ROWS ARE RE-CHECKED IN THE BACKGROUND, once per open, so a token that expired or was
  // revoked overnight says so without anybody pressing Test.
  // A viewer who may not run checks (setup:manage) is not made to: the API would refuse it.
  const mayCheck = useGate("setup:manage").allowed;
  useEffect(() => {
    if (!view || autoChecked.current || !mayCheck) return;
    autoChecked.current = true;
    const stale = staleTargets(view);
    if (!stale.length) return;
    stale.forEach((key) => setChecking(key, true));
    setupWrite<SetupView>(projectId, "checks", { target: "stale" }, { label: "Test all", background: true })
      .then(setView)
      .catch(() => undefined)
      .finally(() => stale.forEach((key) => setChecking(key, false)));
  }, [view, projectId, setChecking, mayCheck]);

  // THE RAIL BADGE FOLLOWS THE BOARD: every new view tells the shell, so connecting the last
  // thing clears the badge without a reload.
  useEffect(() => {
    if (!view) return;
    try {
      window.dispatchEvent(new CustomEvent(SETUP_BADGE_EVENT, { detail: summarize(view).badge }));
    } catch { /* no window */ }
  }, [view]);

  // WHAT THE SERVER IS STILL LOOKING FOR (signed-in CLIs, the secret store) arrives on a
  // background probe; the page asks again shortly rather than waiting on it.
  const detecting = Boolean(view?.detecting);
  const asks = useRef(0);
  useEffect(() => {
    if (!detecting || asks.current >= 6) return;
    const timer = window.setTimeout(() => { asks.current += 1; void load(); }, 2500);
    return () => window.clearTimeout(timer);
  }, [detecting, view, load]);

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
        {error ? (
          <div className="su-line" role="alert" data-testid="su-load-error">
            <span className="su-pill su-tone-red" title={error} data-tip={error}>
              <span className="su-dot su-tone-red" aria-hidden="true" />Not loaded
            </span>
            <button type="button" className="su-btn su-btn-sm" data-testid="su-load-retry"
                    onClick={() => { setError(null); void load(); }}>Retry</button>
          </div>
        ) : <span className="su-faint">Loading…</span>}
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
            <Dot tone={view.verdict.tone} />{view.verdict.label}
          </span>
          {/* THE SEAM TO SKILLS AND MCP: once the repository and a model work, the next job is
              what the crew can do (the Kit opens on its Essentials). Board and stepper alike. */}
          {view.ready ? (
            <a className="su-link" href="?area=skills&setup=1" data-testid="su-next-skills"
               title="Choose what your agents can do: research, tickets, email."
               data-tip="Choose what your agents can do: research, tickets, email.">
              Next: give your crew skills
            </a>
          ) : null}
          <span className="su-spacer" />
          {/* Nothing connected, nothing to test: the button waits for something to check. */}
          <GatedButton permission="setup:manage" type="button" className="su-btn" data-testid="su-test-all"
                  disabled={checking.has("all")
                    || (!view.repository.repos.length && !view.models.length)}
                  onClick={testAll}
                  title="Re-checks GitHub access and asks each model one tiny question."
                  data-tip="Re-checks GitHub access and asks each model one tiny question.">
            <ArrowClockwise size={14} aria-hidden="true" className={checking.has("all") ? "su-spin" : undefined} />
            Test all
          </GatedButton>
        </header>
        <RepositoryRow ref={ref("repository")} mode={rowMode(view, "repository")}
                       step={stepNumber(view, "repository")} focused={focus === "repository"} />
        <GitHubAccessRow />
        <ModelsRow ref={ref("models")} mode={rowMode(view, "models")}
                   step={stepNumber(view, "models")} focused={focus === "models"}
                   add={params.add} source={params.source}
                   onAddHandled={() => setParams((p) => ({ ...p, add: null }))} />
        <CapacityRow ref={ref("capacity")} actor={actor} focused={focus === "capacity"} />
        {/* Model routing — where "Who does what" lived; its rows are "Pin a job" inside. */}
        <RoutingRow ref={ref("jobs")} mode={rowMode(view, "jobs")} focused={focus === "jobs"} />
        <BudgetRow ref={ref("budget")} mode={rowMode(view, "budget")} focused={focus === "budget"} />
        <TryRow ref={ref("try")} mode={rowMode(view, "try")} step={stepNumber(view, "try")}
                focused={focus === "try"} />
        <ToolchainsRow />
        <PublicUrlRow />
        {/* The end of setup: Improve BOSS, asked once, and the heartbeat with its opt-out. */}
        <SharingAsk projectId={projectId} actor={actor ?? ""} />
      </div>
    </BoardContext.Provider>
  );
}
