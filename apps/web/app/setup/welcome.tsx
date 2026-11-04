"use client";

/**
 * Get BOSS working in 3 steps (BOSSAPP-808 screen 1): Connect GitHub · Add a model · Try a sample
 * project. Shown to a new install until the three are done (Setup › Get started reopens it).
 * Done-ness is the server's: a green account or working repo, a green model, a first PR (`try.done`).
 * The project buttons only link to the New project flow (`/?new_project=sample|import|idea`).
 */
import React, { useState } from "react";
import { Check, GitBranch, Lightbulb } from "@phosphor-icons/react";

import type { SetupView } from "../../lib/setup-types";
import type { SigninStatus } from "../../lib/identity/github-signin";
import { AddAccountPanel } from "./add-account-panel";
import { AddModelPanel } from "./add-model-drawer";
import { Drawer } from "./drawer";
import { useBoard } from "./parts";
import { accountsOf, reposOf } from "./setup-blocks";
import { fetchSetup } from "../../lib/setup-client";
import { defaultAdd } from "../../lib/setup-stepper";

export const NEW_PROJECT_HREF = {
  sample: "/?new_project=sample",
  import: "/?new_project=import",
  idea: "/?new_project=idea",
} as const;

const FLOW = ["Idea", "Plan (you approve)", "Build", "Review", "Pull request", "Live preview"];

export type WelcomeState = {
  github: { done: boolean; login: string | null; repos: number };
  model: { done: boolean; label: string | null; kind: string | null };
  sample: { done: boolean };
};

/** What the three steps say, from the server's view alone. */
export function welcomeState(view: SetupView, signin?: SigninStatus | null): WelcomeState {
  const accounts = accountsOf(view, signin);
  const green = accounts.find((a) => a.pill.tone === "green");
  const repoWorks = view.repository.repos.some((r) => r.pill.tone === "green");
  const model = view.models.find((m) => m.pill.tone === "green");
  return {
    github: { done: Boolean(green) || repoWorks, login: green?.login ?? green?.name ?? null,
              repos: reposOf(view).length },
    model: { done: Boolean(model), label: model?.label ?? null, kind: model?.kind ?? null },
    sample: { done: Boolean(view.try.done) },
  };
}

export function welcomeDone(state: WelcomeState): boolean {
  return state.github.done && state.model.done && state.sample.done;
}

function Circle({ n, done, current }: { n: number; done: boolean; current: boolean }) {
  return (
    <span className="su-wl-circle" data-done={done || undefined} data-current={current || undefined}
          aria-label={done ? "done" : `step ${n}`}>
      {done ? <Check size={14} weight="bold" aria-hidden="true" /> : n}
    </span>
  );
}

export function Welcome({ onSetup }: { onSetup: () => void }) {
  const { view, signin, projectId, apply } = useBoard();
  const [open, setOpenState] = useState<null | "account" | "model">(null);
  const setOpen = (next: null | "account" | "model") => {
    setOpenState(next);
    if (next === null) void fetchSetup(projectId).then(apply).catch(() => undefined);
  };
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
        <button type="button" className="su-link" onClick={onSetup} data-testid="su-welcome-setup">Setup</button>
      </header>

      <ol className="su-wl-steps">
        <li className="su-wl-step" data-current={current === 1 || undefined} data-testid="su-wl-github">
          <Circle n={1} done={state.github.done} current={current === 1} />
          <div className="su-stack su-tight">
            <span className="su-strong">Connect GitHub</span>
            {state.github.done ? (
              <span className="su-faint" data-testid="su-wl-github-done">
                Connected{state.github.login ? ` as ${state.github.login}` : ""} · {state.github.repos} repo{s(state.github.repos)} BOSS can use
              </span>
            ) : <span className="su-faint">Pick the repos BOSS may work on.</span>}
          </div>
          <span className="su-spacer" />
          {state.github.done ? (
            <button type="button" className="su-link" onClick={() => setOpen("account")} data-testid="su-wl-github-more">
              Add another account
            </button>
          ) : (
            <button type="button" className={`su-btn${current === 1 ? " su-btn-primary" : ""}`}
                    onClick={() => setOpen("account")} data-testid="su-wl-github-go">Connect GitHub</button>
          )}
        </li>

        <li className="su-wl-step" data-current={current === 2 || undefined} data-testid="su-wl-model">
          <Circle n={2} done={state.model.done} current={current === 2} />
          <div className="su-stack su-tight">
            <span className="su-strong">Add a model</span>
            {state.model.done ? (
              <span className="su-faint" data-testid="su-wl-model-done">
                {state.model.label} {state.model.kind === "subscription" ? "signed in" : "connected"} · builds and reviews will use it
              </span>
            ) : <span className="su-faint">A subscription, an API key or a local model.</span>}
          </div>
          <span className="su-spacer" />
          {state.model.done ? (
            <button type="button" className="su-link" onClick={() => setOpen("model")} data-testid="su-wl-model-more">
              Add another model
            </button>
          ) : (
            <button type="button" className={`su-btn${current === 2 ? " su-btn-primary" : ""}`}
                    onClick={() => setOpen("model")} data-testid="su-wl-model-go">Add a model</button>
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
            <span className="su-faint">~8 min · included in your subscription</span>
          </div>
          <span className="su-spacer" />
          <a className={`su-btn${current === 3 ? " su-btn-primary" : ""}`} href={NEW_PROJECT_HREF.sample}
             data-testid="su-wl-sample-go">Start the sample</a>
        </li>
      </ol>

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
        </div>
      </section>

      {open === "account" ? (
        <AddAccountPanel onClose={() => setOpen(null)} onAdvanced={() => { setOpen(null); onSetup(); }} />
      ) : null}
      {open === "model" ? (
        <Drawer title="Add a model" onClose={() => setOpen(null)} testid="su-wl-model-drawer">
          <AddModelPanel initialTile={defaultAdd(view).tile} initialProvider={defaultAdd(view).provider}
                         onDone={() => setOpen(null)} />
        </Drawer>
      ) : null}
    </div>
  );
}
