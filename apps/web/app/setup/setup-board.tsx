"use client";

/**
 * Repo and models, one board, one state.
 *
 * WHY THIS REPLACED setup-ui.tsx. The old page was forms that performed actions, so its state was
 * whatever the last form said: eight GETs on load, five verdicts that disagreed, a paragraph per
 * patch. This renders ONE server view (GET /v1/setup) and replaces it whole after every write; the
 * server decides every pill. While setup is unfinished the board runs as a stepper , 
 * 1 GitHub (the one "Sign in with GitHub"), 2 Repos, 3 Model, 4 Try it, and after the first pull
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
  ADVANCED_ROWS, defaultAdd, readParams, rowMode, SETUP_BADGE_EVENT, stepNumber, summarize, type SetupParams,
} from "../../lib/setup-stepper";
import type { RowKey, SetupView } from "../../lib/setup-types";
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
import { PublicUrlRow } from "./public-url-row";
import { ToolchainsRow } from "./toolchains-row";
import { SharingAsk } from "../improve/sharing-ask";
import { CiWorkflowsToggle } from "./ci-workflows-toggle";
import { GitHubSigninRow, useSignin } from "./github-signin-row";
import { Tip } from "./pill";
import { SectionLabel } from "../section-label";
import { useOnScreen } from "../../lib/area-shown";
import { SetupBlocks, WhoDoesWhat } from "./setup-blocks";
import { GET_STARTED_EVENT, getStarted, Welcome } from "./welcome";

/** One plain line per Advanced item (≤ 6 words), the detail in its tooltip. */
const ADVANCED: Record<string, { line: string; tip: string }> = {
  github_app: { line: "BOSS GitHub App and access",
                tip: "Each build gets a short-lived token for its own repo only." },
  repositories: { line: "Repos BOSS may use, org-wide",
                  tip: "The org's list of repositories BOSS may build in." },
  ci: { line: "Let builds edit CI workflows",
        tip: "Off by default. Asked for only when a build needs to change CI." },
  build_database: { line: "Postgres for builds that need it", tip: "Auto, Always or Never." },
  build_speed: { line: "How many agents write a build at once", tip: "Balanced or Fastest." },
  toolchains: { line: "What builds can compile and run", tip: "Python and Node always; others on the host." },
  jobs: { line: "Which model does which job", tip: "Pin a model to a job, or let BOSS route." },
  budget: { line: "Monthly spend cap", tip: "Set in Govern › Limits, with what happens at the cap." },
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

/** The rows the Setup blocks answer themselves (Add GitHub account, Add model), not Advanced. */
const BLOCK_FOCUS = new Set(["github", "repository", "models"]);

/** A link into Advanced settings: a row that lives there, or GitHub sending the person back from
 *  the org App's install. Repos, Models, a model's add/menu and the embedding model are on the
 *  board itself, so their links leave Advanced folded. */
function deepLinked(): boolean {
  try {
    const sp = new URLSearchParams(window.location.search);
    const f = readParams(window.location.search).focus;
    return Boolean(sp.get("github")) || (f !== null && ADVANCED_ROWS.includes(f));
  } catch { return false; }
}

/** An old link to the Welcome inside Setup (`view=welcome`, `focus=try`): the Welcome is its own area. */
function oldWelcomeLink(): boolean {
  try {
    const sp = new URLSearchParams(window.location.search);
    return sp.get("view") === "welcome" || sp.get("focus") === "try";
  } catch { return false; }
}

function navigate(area: "welcome" | "setup"): void {
  try { window.dispatchEvent(new CustomEvent("boss:navigate", { detail: area })); } catch { /* no window */ }
}

/**
 * Three layouts over one server view (BOSSAPP-808):
 *  - `"setup"` (area=setup, "Repo and models"): the 1 GitHub, 2 Repos, 3 Model stepper while a
 *    repository or a model is missing, then the board, GitHub accounts, Repositories, Models , 
 *    with every earlier row and setting under Advanced settings. Never the Welcome.
 *  - `"welcome"` (area=welcome, "Get started"): the 3-step Welcome. Its steps deep-link into Setup.
 *  - `"classic"` (the default): the board as it was, row for row.
 */
export function SetupBoard({ projectId, actor, initialView = null, layout = "classic" }: {
  projectId: string; actor?: string; initialView?: SetupView | null; layout?: "classic" | "setup" | "welcome";
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
  const [openAdd, setOpenAdd] = useState(false);
  useEffect(() => {
    const read = () => {
      setParams(currentParams());
      try { setOpenAdd(new URLSearchParams(window.location.search).get("open") === "add"); } catch { /* no window */ }
    };
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
  const mayCheck = useGate("setup:check").allowed;
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
  // ADVANCED SETTINGS (the whole earlier board) opens on a deep link into it.
  const [classicOpen, setClassicOpen] = useState<boolean>(() => deepLinked());
  const classicRef = useRef<HTMLDetailsElement | null>(null);
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
  useEffect(() => {
    if (params.focus && ADVANCED_ROWS.includes(params.focus)) setClassicOpen(true);
  }, [params.focus]);
  // Get started's "Add a model" opens the Models block's drawer once; closing it is final.
  const [modelAddDone, setModelAddDone] = useState(false);
  // BUDGET LIVES IN GOVERN: an old `focus=budget` link goes straight to Govern › Limits, never to a
  // row that only says "Set in Govern" (tester, 2026-11-05).
  useEffect(() => {
    if (layout !== "setup" || params.focus !== "budget") return;
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("area", "govern");
      url.searchParams.set("tab", "limits");
      url.searchParams.delete("focus");
      window.location.replace(url.toString());
    } catch { /* no window */ }
  }, [layout, params.focus]);
  // OLD LINKS KEEP WORKING: Setup's former Welcome (`view=welcome`, `focus=try`) opens Get started.
  useEffect(() => {
    if (layout === "setup" && oldWelcomeLink()) navigate("welcome");
  }, [layout]);
  // THE RAIL'S GET STARTED follows the board.
  useEffect(() => {
    if (!view || layout === "classic") return;
    try { window.dispatchEvent(new CustomEvent(GET_STARTED_EVENT, { detail: getStarted(view) })); } catch { /* no window */ }
  }, [view, layout]);
  // SHOWN AGAIN, READ AGAIN: Setup and Get started are two areas over one server view, so the one
  // coming back on screen asks for it (a connection made in the other shows at once).
  const wasOnScreen = useRef(onScreen);
  useEffect(() => {
    if (layout !== "classic" && onScreen && !wasOnScreen.current) void load();
    wasOnScreen.current = onScreen;
  }, [onScreen, layout, load]);
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
  // Not offered on this install: no ① row at all, ② Repos' "Connect GitHub" is the way in
  // (BOSSAPP-815), never a pill saying "This install uses the advanced path".
  const signinShown = signin?.configured ? signin : null;
  const shift = (n: number | null) => (n && signinShown ? n + 1 : n);

  // EVERY SETTING NOT SHOWN ON THE BOARD, one plain line each: the classic board folds them under
  // Advanced; Repo and models' board lists them under Advanced settings, and nothing else.
  const advancedItems = (
    <>
          <div className="su-adv-item" ref={ref("github_app")}><Why id="github_app" /><GitHubAccessRow /></div>
          {/* The org's registry: which repositories BOSS may use at all (multi-repo screen 1).
              `focus=repositories` lands on it; it hides itself on an API without it. */}
          <div className="su-adv-item" ref={ref("repositories")}><Why id="repositories" /><RepositoryRegistry projectId={projectId} /></div>
          <div className="su-adv-item" ref={ref("ci")}><Why id="ci" /><CiPermission /></div>
          <div className="su-adv-item" ref={ref("build_database")}><Why id="build_database" /><BuildDatabaseRow /></div>
          <div className="su-adv-item" ref={ref("build_speed")}><Why id="build_speed" /><BuildSpeedRow /></div>
          <div className="su-adv-item" ref={ref("toolchains")}><Why id="toolchains" /><ToolchainsRow /></div>
          {/* Model routing, where "Who does what" lived; its rows are "Pin a job" inside. */}
          <div className="su-adv-item"><Why id="jobs" /><RoutingRow ref={ref("jobs")} mode={rowMode(view, "jobs")} focused={focus === "jobs"} /></div>
          {/* BUDGET LIVES IN GOVERN (operator, 2026-11-05: "The Budget things should move to
              Governance"): one place to set it, Govern › Limits; here, a link to it. */}
          <div className="su-adv-item" ref={ref("budget")}><Why id="budget" />
            <section className="su-row" data-testid="su-row-budget" aria-label="Budget" data-focused={focus === "budget" || undefined}>
              <div className="su-row-label"><SectionLabel as="span" tip="Monthly, daily and per-project caps.">Budget</SectionLabel></div>
              <div className="su-row-body su-line">
                <a className="su-link" href="?area=govern&tab=limits" data-testid="su-budget-govern">Set in Govern › Limits ↗</a>
              </div>
            </section>
          </div>
          <div className="su-adv-item" ref={ref("public_url")}><Why id="public_url" /><PublicUrlRow /></div>
    </>
  );

  const classicBody = (nested: boolean) => (
    <>
        <header className="su-head">
          {!nested ? <h1>Repo and models</h1> : null}
          <span className={`su-pill su-verdict su-tone-${view.verdict.tone}`} data-testid="su-verdict"
                title={view.verdict.tip} data-tip={view.verdict.tip}>
            <Dot tone={view.verdict.tone} />{view.verdict.label}
          </span>
          {/* THE SEAM TO SKILLS AND MCP: once the repository and a model work, the next job is
              what the crew can do (the Kit opens on its Essentials). Board and stepper alike. */}
          {/* THE NEXT STEP FOLLOWS THE STEPPER: ready but no first PR yet is "open your first PR"
              (Try it, at the bottom), never a jump to Skills that skips the first build. */}
          {view.ready && view.next === "try" ? (
            <a className="su-link" href="?area=welcome" data-testid="su-next-try"
               onClick={(e) => {
                 if (layout === "classic") return;
                 e.preventDefault();
                 navigate("welcome");
               }}
               title="BOSS builds a small sample app so you can watch every step once."
               data-tip="BOSS builds a small sample app so you can watch every step once.">
              Next: try a sample project
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
          <GatedButton permission="setup:check" type="button" className="su-btn" data-testid="su-test-all"
                  disabled={checking.has("all")
                    || (!view.repository.repos.length && !view.models.length)}
                  onClick={testAll}
                  title="Re-checks GitHub access and asks each model one tiny question."
                  data-tip="Re-checks GitHub access and asks each model one tiny question.">
            <ArrowClockwise size={14} aria-hidden="true" className={checking.has("all") ? "su-spin" : undefined} />
            Test all
          </GatedButton>
        </header>
        {/* ① GitHub, the one sign-in (absent on an API without it: then Repos is step ①). */}
        {signinShown ? <GitHubSigninRow ref={ref("github")} status={signinShown} focused={focus === "github"}
                         step={stepNumber(view, "repository") ? 1 : null}
                         mode={view.next === "repository" && !view.repository.repos.length
                           && !(signin?.me?.status === "active") ? "current" : "open"}
                         onAdvanced={() => {
                           setAdvancedOpen(true);
                           window.setTimeout(() => refs.current.github_app?.scrollIntoView?.({ behavior: "smooth", block: "start" }), 50);
                         }} /> : null}
        <RepositoryRow ref={ref("repository")} mode={rowMode(view, "repository")}
                       step={shift(stepNumber(view, "repository"))} focused={focus === "repository"} />
        <ModelsRow ref={ref("models")} mode={rowMode(view, "models")}
                   step={shift(stepNumber(view, "models"))} focused={focus === "models" || focus === "capacity"}
                   add={params.add} source={params.source}
                   onAddHandled={() => setParams((p) => ({ ...p, add: null }))}>
          {/* rc9 (BOSS-434/437): the model sources sit inside Models, one heading and panel. */}
          <CapacityRow ref={ref("capacity")} actor={actor} focused={focus === "capacity"} />
          {/* The org's one embedding model (meaning search), set once for every project. */}
          <div ref={ref("embedding_model")} data-focused={focus === "embedding_model" || undefined}><EmbeddingModelRow startOpen={focus === "embedding_model"} /></div>
        </ModelsRow>
        {/* NO TRY IT HERE (operator, 2026-11-04: "Why trying out sample project is in Repos and
            Models"): Setup is connections only. The sample and the one-sentence PR live on the
            Welcome (step 3); `focus=try` opens it. */}
        <details className="su-advanced" data-testid="su-advanced" ref={ref("advanced")} open={advancedOpen}
                 onToggle={(e) => setAdvancedOpen((e.currentTarget as HTMLDetailsElement).open)}>
          <summary className="su-advanced-summary" data-testid="su-advanced-toggle"
                   title="The org's own GitHub App, CI, builds, models' routing and budget."
                   data-tip="The org's own GitHub App, CI, builds, models' routing and budget.">
            Advanced
          </summary>
          {advancedItems}
        </details>
    </>
  );

  if (layout === "welcome") {
    return (
      <BoardContext.Provider value={ctx}>
        <div className="su-board" data-testid="su-board" data-layout="welcome">
          <Welcome onSetup={() => navigate("setup")} />
        </div>
      </BoardContext.Provider>
    );
  }

  // THE STEPPER while a repository or a model is missing (1 GitHub, 2 Repos, 3 Model).
  const stepper = view.next === "repository" || view.next === "model";
  if (layout === "setup" && !stepper) {
    const openAdvanced = (row?: string) => {
      setClassicOpen(true);
      if (row) {
        landedOn.current = null;
        setParams((p) => ({ ...p, focus: row as RowKey }));
      } else {
        window.setTimeout(() => classicRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" }), 50);
      }
    };
    // Welcome's "Add another account / model" (`open=add`) land on the block's own Add; any
    // other link to a block only lands on it (tester, 2026-11-05: focus=repository opened "Add a
    // GitHub account", focus=models the Add a model drawer).
    const blockOpen = openAdd && params.focus && BLOCK_FOCUS.has(params.focus) && !params.add && !params.source
      ? (params.focus === "models" ? "model" as const : "account" as const) : null;
    const modelAdd = blockOpen === "model" && !modelAddDone ? defaultAdd(view).provider : null;
    return (
      <BoardContext.Provider value={ctx}>
        <div className="su-board" data-testid="su-board" data-layout="setup">
          <header className="su-head">
            <h1>Repo and models</h1>
            <span className={`su-pill su-verdict su-tone-${view.verdict.tone}`} data-testid="su-setup-verdict"
                  title={view.verdict.tip} data-tip={view.verdict.tip}>
              <Dot tone={view.verdict.tone} />{view.verdict.label}
            </span>
            {getStarted(view) ? (
              <a className="su-link" href="?area=welcome" data-testid="su-open-welcome"
                 onClick={(e) => { e.preventDefault(); navigate("welcome"); }}>Back to Get started</a>
            ) : null}
            <span className="su-spacer" />
            <GatedButton permission="setup:check" type="button" className="su-btn" data-testid="su-test-all"
                         disabled={checking.has("all") || (!view.repository.repos.length && !view.models.length)}
                         onClick={testAll}
                         title="Re-checks GitHub access and asks each model one tiny question."
                         data-tip="Re-checks GitHub access and asks each model one tiny question.">
              <ArrowClockwise size={14} aria-hidden="true" className={checking.has("all") ? "su-spin" : undefined} />
              Test all
            </GatedButton>
          </header>
          <div ref={ref("repository")} className="su-stack">
            <SetupBlocks onAdvanced={openAdvanced} initialOpen={blockOpen === "account" ? "account" : null}
                         key={blockOpen ?? "none"} />
          </div>
          {/* ONE MODELS SECTION (operator, 2026-11-05: "We have two areas to add model"): the
              full list, each model's status, its one fix, Details and ⋯, Claude's "pays with" , 
              then the org's embedding model and who does what. Nothing about models is repeated
              under Advanced settings. */}
          <ModelsRow block ref={ref("models")} mode="done" step={null}
                     focused={focus === "models" || focus === "capacity"}
                     add={params.add ?? modelAdd} source={params.source}
                     onAddHandled={() => { setModelAddDone(true); setParams((p) => ({ ...p, add: null })); }}>
            <CapacityRow ref={ref("capacity")} actor={actor} focused={focus === "capacity"} />
            <div ref={ref("embedding_model")} data-focused={focus === "embedding_model" || undefined}>
              <EmbeddingModelRow startOpen={focus === "embedding_model"} />
            </div>
            <WhoDoesWhat view={view} onChange={(job) => {
              openAdvanced("jobs");
              // A job's chip lands on THAT job's model menu, not the whole Routing block.
              if (job) {
                window.setTimeout(() => {
                  const pick = Array.from(document.querySelectorAll<HTMLSelectElement>("select"))
                    .find((el) => el.getAttribute("aria-label") === job);
                  pick?.focus?.();
                }, 700);
              }
            }} />
          </ModelsRow>
          <details className="su-advanced su-advanced-all" data-testid="su-advanced-all" ref={classicRef}
                   open={classicOpen}
                   onToggle={(e) => setClassicOpen((e.currentTarget as HTMLDetailsElement).open)}>
            <summary className="su-advanced-summary" data-testid="su-advanced-all-toggle"
                     title="The org's GitHub App, repo registry, CI, builds, routing, budget, public URL."
                     data-tip="The org's GitHub App, repo registry, CI, builds, routing, budget, public URL.">
              Advanced settings
            </summary>
            <div className="su-board su-board-inner su-adv-list">
              {signinShown ? (
                <GitHubSigninRow ref={ref("github")} status={signinShown} focused={focus === "github"}
                                 step={null} mode="open"
                                 onAdvanced={() => {
                                   window.setTimeout(() => refs.current.github_app?.scrollIntoView?.({ behavior: "smooth", block: "start" }), 50);
                                 }} />
              ) : null}
              {advancedItems}
            </div>
          </details>
          <SharingAsk projectId={projectId} actor={actor ?? ""} />
        </div>
      </BoardContext.Provider>
    );
  }

  return (
    <BoardContext.Provider value={ctx}>
      <div className="su-board" data-testid="su-board" data-first-run={view.next !== null || undefined}
           data-layout={layout === "setup" ? "setup-stepper" : undefined}>
        {layout === "setup" && getStarted(view) ? (
          <a className="su-link" href="?area=welcome" data-testid="su-open-welcome"
             onClick={(e) => { e.preventDefault(); navigate("welcome"); }}>Back to Get started</a>
        ) : null}
        {classicBody(false)}
        {/* The end of setup: Improve BOSS, asked once, and the heartbeat with its opt-out. */}
        <SharingAsk projectId={projectId} actor={actor ?? ""} />
      </div>
    </BoardContext.Provider>
  );
}
