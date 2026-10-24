"use client";

/**
 * Repo and models — one board, one state.
 *
 * WHY THIS REPLACED setup-ui.tsx. The old page was forms that performed actions, so its state was
 * whatever the last form said: eight GETs on load, five verdicts that disagreed, a paragraph per
 * patch. This renders ONE server view (GET /v1/setup) and replaces it whole after every write; the
 * server decides every pill. While setup is unfinished the board runs as a stepper —
 * 1 GitHub (the one "Sign in with GitHub"), 2 Repos, 3 Model, 4 Try it — and after the first pull
 * request it is a board. Everything else (the org's own GitHub App, the multi-repo registry, CI
 * permission, build database, toolchains, routing, budget, public URL) folds under ADVANCED,
 * each with one plain line; a deep link to any of them opens the fold on it. The model sources
 * (capacity) sit inside 3 Model since rc9, so `focus=capacity` lands there; so does the org's ONE
 * embedding model (2026-10-23, "so all projects can use it"), so `focus=embedding_model` lands there.
 *
 * Deep links: `?area=setup&focus=repository|repositories|models|embedding_model|jobs|routing|budget|try` (routing = jobs), `&add=<provider>` opens Add a
 * model on that provider, `&source=<id>` opens that model's menu, legacy `step=github|model` maps
 * to focus. Rows whose last check is over a day old are re-checked in the background on open.
 */
import { GatedButton, useGate } from "../../components/gated";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowClockwise } from "@phosphor-icons/react";

import { fetchSetup, SetupFailure, setupWrite } from "../../lib/setup-client";
import {
  ADVANCED_ROWS, readParams, rowMode, SETUP_BADGE_EVENT, stepNumber, summarize, type SetupParams,
} from "../../lib/setup-stepper";
import type { RowKey, SetupView } from "../../lib/setup-types";
import { BudgetRow } from "./budget-row";
import { BuildDatabaseRow } from "./build-database-row";
import { BuildSpeedRow } from "./build-speed-row";
import { EmbeddingModelRow } from "./embedding-model-row";
import { CapacityRow } from "./capacity-row";
import { ModelsRow } from "./models-row";
import { BoardContext, toast, useBoard } from "./parts";
import { Dot, localView } from "./pill";
import { GitHubAccessRow, SETUP_TESTED_EVENT } from "./github-access-row";
import { RepositoryRow } from "./repository-row";
import { RepositoryRegistry } from "../multirepo/registry";
import { RoutingRow } from "./routing-row";
import { TryRow } from "./try-row";
import { PublicUrlRow } from "./public-url-row";
import { ToolchainsRow } from "./toolchains-row";
import { SharingAsk } from "../improve/sharing-ask";
import { CiWorkflowsToggle } from "./ci-workflows-toggle";
import { GitHubSigninRow, useSignin } from "./github-signin-row";
import { Tip } from "./pill";
import { SectionLabel } from "../section-label";
import { useOnScreen } from "../../lib/area-shown";

/** One plain line per Advanced item (≤ 6 words), the detail in its tooltip. */
const ADVANCED: Record<string, { line: string; tip: string }> = {
  github_app: { line: "Org admin: per-build tokens, one repo",
                tip: "Your org's own GitHub App: each build gets a token for its repo only." },
  repositories: { line: "Which repos projects may use",
                  tip: "The org's list of repositories BOSS may build in." },
  ci: { line: "Let builds edit CI workflows",
        tip: "Off by default. Asked for only when a build needs to change CI." },
  build_database: { line: "Postgres for builds that need it", tip: "Auto, Always or Never." },
  build_speed: { line: "How many agents write a build at once", tip: "Balanced or Fastest." },
  toolchains: { line: "What builds can compile and run", tip: "Python and Node always; others on the host." },
  jobs: { line: "Which model does which job", tip: "Pin a model to a job, or let BOSS route." },
  budget: { line: "Monthly spend cap", tip: "BOSS pauses model spend at the cap." },
  public_url: { line: "Address used in emailed links", tip: "Sign-in, passkeys and emailed links use it." },
};

function Why({ id }: { id: keyof typeof ADVANCED }) {
  const a = ADVANCED[id];
  return <Tip tip={a.tip} className="su-adv-why su-faint" testid={`su-adv-why-${id}`}>{a.line}</Tip>;
}

/** Advanced › CI permission: the per-repository opt-in, one line per connected repository. */
function CiPermission() {
  const { view } = useBoard();
  const bound = view.repository.repos.filter((r) => r.binding_id);
  return (
    <section className="su-row" data-testid="su-row-ci" aria-label="CI permission" id="su-ci">
      <div className="su-row-label"><SectionLabel as="span" tip="Builds may change .github/workflows (per repo).">CI permission</SectionLabel></div>
      <div className="su-row-body su-stack">
        {bound.length ? bound.map((r) => (
          <div key={r.binding_id} className="su-stack" data-testid={`su-ci-${r.full_name}`}>
            <span className="su-mono" data-user-text>{r.full_name}</span>
            <CiWorkflowsToggle bindingId={r.binding_id as string} />
          </div>
        )) : <span className="su-faint">Connect a repo first</span>}
      </div>
    </section>
  );
}

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
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    if (!running || !onScreen) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [running, load, onScreen]);

  // LEAVING MID-CHECK is asked about: the check finishes on the server, but its answer is lost.
  useEffect(() => {
    if (!checking.size) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [checking]);

  const { status: signin } = useSignin();
  // ADVANCED IS FOLDED unless a link lands inside it, or GitHub just sent the person back from
  // the org App's install (that row reads `?github=` itself).
  const [advancedOpen, setAdvancedOpen] = useState<boolean>(() => {
    try {
      const sp = new URLSearchParams(window.location.search);
      const f = readParams(window.location.search).focus;
      return Boolean(sp.get("github")) || (f !== null && ADVANCED_ROWS.includes(f));
    } catch { return false; }
  });
  useEffect(() => {
    if (params.focus && ADVANCED_ROWS.includes(params.focus)) setAdvancedOpen(true);
  }, [params.focus]);
  const ctx = useMemo(() => view ? {
    projectId, view, apply: setView, checking, setChecking, signin,
  } : null, [projectId, view, checking, setChecking, signin]);

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
      // GitHub access asks GitHub again what the App was granted (github-access-row.tsx).
      try { window.dispatchEvent(new Event(SETUP_TESTED_EVENT)); } catch { /* no window */ }
    } catch (e) {
      toast((e as SetupFailure).refusal?.headline ?? "Check failed", "error");
    } finally { setChecking("all", false); }
  };
  const focus = params.focus;
  const ref = (row: RowKey) => (el: HTMLElement | null) => { refs.current[row] = el; };
  // With "Sign in with GitHub" shown as ①, the stepper's rows move down one: ② Repos ③ Model ④ Try it.
  const shift = (n: number | null) => (n && signin ? n + 1 : n);

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
          {/* THE NEXT STEP FOLLOWS THE STEPPER: ready but no first PR yet is "open your first PR"
              (Try it, at the bottom), never a jump to Skills that skips the first build. */}
          {view.ready && view.next === "try" ? (
            <a className="su-link" href="?area=setup&focus=try" data-testid="su-next-try"
               onClick={(e) => {
                 const el = refs.current.try;
                 if (el && typeof el.scrollIntoView === "function") { e.preventDefault(); el.scrollIntoView({ behavior: "smooth", block: "start" }); }
               }}
               title="Describe one small change; BOSS opens a pull request on your repository."
               data-tip="Describe one small change; BOSS opens a pull request on your repository.">
              Next: open a PR
            </a>
          ) : view.ready ? (
            <a className="su-link" href="?area=skills&setup=1" data-testid="su-next-skills"
               title="Choose what your agents can do: research, tickets, email."
               data-tip="Choose what your agents can do: research, tickets, email.">
              Next: add skills
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
        {/* ① GitHub — the one sign-in (absent on an API without it: then Repos is step ①). */}
        <GitHubSigninRow ref={ref("github")} status={signin} focused={focus === "github"}
                         step={signin && stepNumber(view, "repository") ? 1 : null}
                         mode={view.next === "repository" && !view.repository.repos.length
                           && !(signin?.me?.status === "active") ? "current" : "open"}
                         onAdvanced={() => {
                           setAdvancedOpen(true);
                           window.setTimeout(() => refs.current.github_app?.scrollIntoView?.({ behavior: "smooth", block: "start" }), 50);
                         }} />
        <RepositoryRow ref={ref("repository")} mode={rowMode(view, "repository")}
                       step={shift(stepNumber(view, "repository"))} focused={focus === "repository"} />
        <ModelsRow ref={ref("models")} mode={rowMode(view, "models")}
                   step={shift(stepNumber(view, "models"))} focused={focus === "models" || focus === "capacity"}
                   add={params.add} source={params.source}
                   onAddHandled={() => setParams((p) => ({ ...p, add: null }))}>
          {/* rc9 (BOSS-434/437): the model sources sit inside Models, one heading and panel. */}
          <CapacityRow ref={ref("capacity")} actor={actor} focused={focus === "capacity"} />
          {/* The org's one embedding model (meaning search), set once for every project. */}
          <div ref={ref("embedding_model")} data-focused={focus === "embedding_model" || undefined}><EmbeddingModelRow /></div>
        </ModelsRow>
        <TryRow ref={ref("try")} mode={rowMode(view, "try")} step={shift(stepNumber(view, "try"))}
                focused={focus === "try"} />
        <details className="su-advanced" data-testid="su-advanced" ref={ref("advanced")} open={advancedOpen}
                 onToggle={(e) => setAdvancedOpen((e.currentTarget as HTMLDetailsElement).open)}>
          <summary className="su-advanced-summary" data-testid="su-advanced-toggle"
                   title="The org's own GitHub App, CI, builds, models' routing and budget."
                   data-tip="The org's own GitHub App, CI, builds, models' routing and budget.">
            Advanced
          </summary>
          <div className="su-adv-item" ref={ref("github_app")}><Why id="github_app" /><GitHubAccessRow /></div>
          {/* The org's registry: which repositories BOSS may use at all (multi-repo screen 1).
              `focus=repositories` lands on it; it hides itself on an API without it. */}
          <div className="su-adv-item" ref={ref("repositories")}><Why id="repositories" /><RepositoryRegistry projectId={projectId} /></div>
          <div className="su-adv-item" ref={ref("ci")}><Why id="ci" /><CiPermission /></div>
          <div className="su-adv-item" ref={ref("build_database")}><Why id="build_database" /><BuildDatabaseRow /></div>
          <div className="su-adv-item" ref={ref("build_speed")}><Why id="build_speed" /><BuildSpeedRow /></div>
          <div className="su-adv-item" ref={ref("toolchains")}><Why id="toolchains" /><ToolchainsRow /></div>
          {/* Model routing — where "Who does what" lived; its rows are "Pin a job" inside. */}
          <div className="su-adv-item"><Why id="jobs" /><RoutingRow ref={ref("jobs")} mode={rowMode(view, "jobs")} focused={focus === "jobs"} /></div>
          <div className="su-adv-item"><Why id="budget" /><BudgetRow ref={ref("budget")} mode={rowMode(view, "budget")} focused={focus === "budget"} /></div>
          <div className="su-adv-item" ref={ref("public_url")}><Why id="public_url" /><PublicUrlRow /></div>
        </details>
        {/* The end of setup: Improve BOSS, asked once, and the heartbeat with its opt-out. */}
        <SharingAsk projectId={projectId} actor={actor ?? ""} />
      </div>
    </BoardContext.Provider>
  );
}
