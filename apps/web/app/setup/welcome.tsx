"use client";

/**
 * Get BOSS working in 3 steps (BOSSAPP-808 screen 1): Connect GitHub, Add a model, Try a sample
 * project. Shown to a new install until the three are done (Setup › Get started reopens it).
 * Done-ness is the server's: a green account or working repo, a green model, a first PR (`try.done`).
 * The project buttons only link to the New project flow (`/?new_project=sample|import|idea`).
 */
import { changeHref, linkTarget } from "../../lib/repo-links";
import React, { useState } from "react";
import { Check, GitBranch, Lightbulb, Plugs } from "@phosphor-icons/react";
import { addHref } from "../skills/kit-url";

import type { SetupView } from "../../lib/setup-types";
import type { SigninStatus } from "../../lib/identity/github-signin";
import { useBoard } from "./parts";
import { Pill } from "./pill";
import { TryRow } from "./try-row";
import { accountsOf, reposOf } from "./setup-blocks";
import { switchAreaWith } from "../url-intent";

export const NEW_PROJECT_HREF = {
  sample: "/?new_project=sample",
  import: "/?new_project=import",
  idea: "/?new_project=idea",
} as const;

const FLOW = ["Idea", "Plan (you approve)", "Build", "Review", "Pull request", "Live preview"];

export type WelcomeState = {
  /** `local`: the org keeps code on this machine (no GitHub needed to build; GitHub is for PRs on GitHub). */
  github: { done: boolean; login: string | null; repos: number; local: { full_name: string; project_id?: string | null; project_name: string } | null };
  model: { done: boolean; label: string | null; kind: string | null };
  /** `project`: the org's live sample project, once it exists (Open the sample). */
  sample: { done: boolean; project: { project_id: string; name: string } | null };
};

/** The sample project's page. Only that project and area: no one-shot params ride along. */
export function sampleHref(projectId: string): string {
  return `/?${new URLSearchParams({ project_id: projectId, area: "project" }).toString()}`;
}

/** What the three steps say, from the server's view alone. */
export function welcomeState(view: SetupView, signin?: SigninStatus | null): WelcomeState {
  const accounts = accountsOf(view, signin);
  const green = accounts.find((a) => a.pill.tone === "green");
  const repoWorks = view.repository.repos.some((r) => r.pill.tone === "green");
  const model = view.models.find((m) => m.pill.tone === "green");
  const local = view.repository.local ?? null;
  return {
    github: { done: Boolean(green) || repoWorks || Boolean(local), login: green?.login ?? green?.name ?? null,
              repos: reposOf(view).length,
              local: local && !green && !repoWorks ? { full_name: local.full_name, project_id: local.project_id ?? null, project_name: local.project_name } : null },
    model: { done: Boolean(model), label: model?.label ?? null, kind: model?.kind ?? null },
    // TICKED ONLY FOR A REAL FIRST PR from the sample / Try it run (walk 2026-11-04: a green tick
    // with no sample run). Other PRs in the project make the install established, not this step.
    sample: { done: Boolean(view.try.last_run?.pr_url), project: view.try.sample ?? null },
  };
}

/** WHAT THE SAMPLE COSTS, only as true as what is connected (stranger test: "included in your
 *  subscription" with no subscription connected). */
export function sampleCostLine(model: WelcomeState["model"]): string {
  if (!model.done) return "About 8 minutes, once a model is connected";
  if (model.kind === "subscription") return "About 8 minutes, included in your subscription";
  if (model.kind === "local") return "About 8 minutes on your local model, no API cost";
  return "About 8 minutes, billed to your API key";
}

export function welcomeDone(state: WelcomeState): boolean {
  return state.github.done && state.model.done && state.sample.done;
}

export { getStarted } from "../../lib/setup-stepper";

export { GET_STARTED_EVENT } from "../../lib/setup-stepper";

function Circle({ n, done, current }: { n: number; done: boolean; current: boolean }) {
  return (
    <span className="su-wl-circle" data-done={done || undefined} data-current={current || undefined}
          aria-label={done ? "done" : `step ${n}`}>
      {done ? <Check size={14} weight="bold" aria-hidden="true" /> : n}
    </span>
  );
}

/** The last sample / Try it run: its pill, the PR when open, the run when it failed. */
function LastRun({ run }: { run: NonNullable<SetupView["try"]["last_run"]> }) {
  const runHref = `?area=delivery_workflows&wfr=${encodeURIComponent(run.run_id)}`;
  return (
    <span className="su-line" data-testid="su-wl-last-run">
      {run.pr_url ? (
        <a className="su-pill su-tone-green" href={changeHref(run.pr_url)} {...linkTarget(run.pr_url)}
           title={run.pill.tip} data-tip={run.pill.tip} data-testid="su-wl-last-pr">{run.pill.label}</a>
      ) : <a href={runHref} className="su-plain"><Pill pill={run.pill} testid="su-wl-last-pill" /></a>}
      {run.pill.tone === "red" ? <a className="su-link" href={runHref} data-testid="su-wl-last-open">Open run ↗</a> : null}
    </span>
  );
}

/** Into Setup on one row (`focus=`), in place: Get started → Setup → back. */
function SetupLink({ focus, className, testid, children }: {
  focus: "repository" | "models"; className: string; testid: string; children: React.ReactNode;
}) {
  // `open=add`: Get started's "Add another …" opens that block's Add. A plain `focus=` link (a
  // fix elsewhere in the console) only lands on the section.
  const href = `/?area=setup&focus=${focus}&open=add`;
  return (
    <a className={className} href={href} data-testid={testid}
       onClick={(e) => {
         if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
         e.preventDefault();
         try {
           switchAreaWith("setup", { focus, open: "add", view: null });
         } catch { window.location.assign(href); }
       }}>
      {children}
    </a>
  );
}

export function Welcome({ onSetup }: { onSetup: () => void }) {
  const { view, signin, projectId } = useBoard();
  const [quickOpen, setQuickOpen] = useState<boolean>(() => {
    try { return new URLSearchParams(window.location.search).get("focus") === "try"; } catch { return false; }
  });

  const state = welcomeState(view, signin);
  const current = !state.github.done ? 1 : !state.model.done ? 2 : !state.sample.done ? 3 : 0;
  const s = (n: number) => (n === 1 ? "" : "s");

  return (
    <div className="su-welcome" data-testid="su-welcome">
      <header className="su-head">
        <div className="su-stack su-tight">
          <h1>Get BOSS working in 3 steps</h1>
          <span className="su-faint">About 10 minutes. You can change everything later in Setup.</span>
        </div>
        <span className="su-spacer" />
        <a className="su-link" href="/?area=setup" data-testid="su-welcome-setup"
           onClick={(e) => { e.preventDefault(); onSetup(); }}>Setup</a>
      </header>

      <ol className="su-wl-steps">
        <li className="su-wl-step" data-current={current === 1 || undefined} data-testid="su-wl-github">
          <Circle n={1} done={state.github.done} current={current === 1} />
          <div className="su-stack su-tight">
            <span className="su-strong">{state.github.local ? "Code on this machine" : "Connect GitHub"}</span>
            {state.github.local ? (
              <span className="su-faint" data-testid="su-wl-github-local">
                {state.github.local.project_id && state.github.local.project_id === projectId
                  ? `${state.github.local.project_name} keeps its code here.`
                  : "Projects keep their code on this machine."} GitHub is optional: connect it for PRs on GitHub.
              </span>
            ) : state.github.done ? (
              <span className="su-faint" data-testid="su-wl-github-done">
                Connected{state.github.login ? ` as ${state.github.login}` : ""}. {state.github.repos} repo{s(state.github.repos)} BOSS can use
              </span>
            ) : <span className="su-faint">Pick the repos BOSS may work on.</span>}
          </div>
          <span className="su-spacer" />
          {state.github.local ? (
            <SetupLink focus="repository" className="su-link" testid="su-wl-github-optional">Connect GitHub</SetupLink>
          ) : state.github.done ? (
            <SetupLink focus="repository" className="su-link" testid="su-wl-github-more">Add another account</SetupLink>
          ) : (
            <SetupLink focus="repository" className={`su-btn${current === 1 ? " su-btn-primary" : ""}`}
                       testid="su-wl-github-go">Connect GitHub</SetupLink>
          )}
        </li>

        <li className="su-wl-step" data-current={current === 2 || undefined} data-testid="su-wl-model">
          <Circle n={2} done={state.model.done} current={current === 2} />
          <div className="su-stack su-tight">
            <span className="su-strong">Add a model</span>
            {state.model.done ? (
              <span className="su-faint" data-testid="su-wl-model-done">
                {state.model.label} {state.model.kind === "subscription" ? "signed in" : "connected"}. Builds and reviews use it.
              </span>
            ) : <span className="su-faint">A subscription, an API key or a local model.</span>}
          </div>
          <span className="su-spacer" />
          {state.model.done ? (
            <SetupLink focus="models" className="su-link" testid="su-wl-model-more">Add another model</SetupLink>
          ) : (
            <SetupLink focus="models" className={`su-btn${current === 2 ? " su-btn-primary" : ""}`}
                       testid="su-wl-model-go">Add a model</SetupLink>
          )}
        </li>

        <li className="su-wl-step su-wl-sample" data-current={current === 3 || undefined} data-testid="su-wl-sample">
          <Circle n={3} done={state.sample.done} current={current === 3} />
          <div className="su-stack su-tight">
            <span className="su-strong">Try a sample project</span>
            <span className="su-faint">BOSS builds a small Hello World web app so you can watch every step once.</span>
            <ol className="su-wl-flow" aria-label="What happens">
              {FLOW.map((f, i) => (
                <React.Fragment key={f}>
                  {i ? <li className="su-wl-arrow" aria-hidden="true">→</li> : null}
                  <li className="su-chip">{f}</li>
                </React.Fragment>
              ))}
            </ol>
            <span className="su-faint" data-testid="su-wl-sample-cost">{sampleCostLine(state.model)}</span>
            {view.try.last_run ? <LastRun run={view.try.last_run} /> : null}
          </div>
          <span className="su-spacer" />
          {state.sample.project ? (
            // THE SAMPLE EXISTS: open it, never offer to start a second one (stranger test r2).
            <a className={`su-btn${current === 3 ? " su-btn-primary" : ""}`} href={sampleHref(state.sample.project.project_id)}
               data-testid="su-wl-sample-open">Open the sample</a>
          ) : (
            <a className={`su-btn${current === 3 ? " su-btn-primary" : ""}`} href={NEW_PROJECT_HREF.sample}
               data-testid="su-wl-sample-go">Start the sample</a>
          )}
        </li>
      </ol>

      {/* QUICK PR (moved from Setup's Try it): one sentence becomes a pull request you review. */}
      <details className="su-advanced" data-testid="su-wl-quick-pr" open={quickOpen}
               onToggle={(e) => setQuickOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary className="su-advanced-summary">Or open one small PR</summary>
        <TryRow mode={view.ready ? "open" : "waiting"} step={null} focused={quickOpen} />
      </details>

      <section className="su-stack" aria-label="Your own work">
        <span className="su-strong">Or go straight to your own work</span>
        <div className="su-wl-cards">
          <a className="su-card su-wl-card" href={NEW_PROJECT_HREF.import} data-testid="su-wl-import">
            <GitBranch size={18} aria-hidden="true" />
            <span className="su-strong">Import an existing repo</span>
          </a>
          <a className="su-card su-wl-card" href={NEW_PROJECT_HREF.idea} data-testid="su-wl-idea">
            <Lightbulb size={18} aria-hidden="true" />
            <span className="su-strong">Start a new app from an idea</span>
          </a>
          {/* CONNECT YOUR TOOLS (BOSSAPP-1478): one press to Add MCP server on Skills & MCP. */}
          <a className="su-card su-wl-card" href={`/${addHref("mcp")}`} data-testid="su-wl-mcp"
             title="GitHub, Jira, Slack or your own tools: anything that speaks MCP"
             data-tip="GitHub, Jira, Slack or your own tools: anything that speaks MCP">
            <Plugs size={18} aria-hidden="true" />
            <span className="su-strong">Connect your tools (MCP)</span>
          </a>
        </div>
      </section>

    </div>
  );
}
