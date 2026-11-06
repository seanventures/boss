"use client";

/**
 * FIRST RUN (BOSSAPP-914 screens 0a / 0b): what a fresh install opens on. It absorbs Get started
 * (app/setup/welcome.tsx) — every item there keeps a place here (README parity table):
 *
 *   Get started today                      here
 *   Connect GitHub (+ "Connected as X")    step-1 door; guide step 1 shows login · repos
 *   Add another account                    "+ another account" (guide footer, step-1 door when done)
 *   Add a model (+ add another)            step-2 door; "+ another model"
 *   Try a sample / Start the sample        step-3 "Try a sample" → the sample door of Add a project
 *   Last sample run pill, PR, Open run     the sample's run pill under step 3
 *   Or open one small PR                   step-3 "One small PR" → Get started's quick-PR row
 *   Import a repo / New app from an idea   step-3 Existing repo / From an idea
 *   Flow Idea → … → Live preview           "What happens to each piece of work"
 *   Link to Repo and models                "change anything later in Repo and models"
 *
 * 0a while a step is open; 0b (the guide beside Helm's live onboarding) once a project with a
 * repo exists and the URL asks for the first run (`&first_run=1`).
 */
import React, { useEffect, useState } from "react";
import {
  Brain, Check, Compass, FolderPlus, GitBranch, GithubLogo, GitPullRequest, Lightbulb, Lightning, Path, Plus, Signpost, Sparkle, Flask, HandWaving, Play,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { fetchSetup } from "../../lib/setup-client";
import type { SetupView } from "../../lib/setup-types";
import {
  PX_PATHS, navigateTo, usePageSearch, parseOnboarding, projectHref, pxHref, readPxUrl, startWork, usePxRead, type Onboarding, type ProjectCard, type StepState,
} from "../../lib/project-experience";
import { FX_ONBOARDING } from "../../lib/project-experience-fixtures";
import { useSignin } from "../setup/github-signin-row";
import { welcomeState, type WelcomeState } from "../setup/welcome";
import { Face, Flow, Ico, Journey, Said, openAddProject, usePxUrl } from "./kit";
import { DiscoverDiagram } from "./onboarding";

const setupHref = (focus: "repository" | "models") => pxHref(`?area=setup&focus=${focus}&open=add`);
const QUICK_PR = "?area=welcome&focus=try";
const FLOW = [
  { label: "Idea", icon: <Lightbulb size={12} aria-hidden />, type: "todo" },
  { label: "Plan", icon: <Face agent={{ name: "Juno", role_key: "pm" }} size={16} />, type: "agent" },
  { label: "You approve", type: "waiting" },
  { label: "Build", icon: <Face agent={{ name: "Forge", role_key: "software_engineer" }} size={16} />, type: "agent" },
  { label: "Review", icon: <Face agent={{ name: "Vera", role_key: "qa" }} size={16} />, type: "agent" },
  { label: "Pull request", icon: <GitPullRequest size={12} aria-hidden />, type: "todo" },
  { label: "Live preview", icon: <Face agent={{ name: "Beacon", role_key: "sre" }} size={16} />, type: "agent" },
];

export function useWelcome(projectId: string): { state: WelcomeState | null; view: SetupView | null; failed: boolean } {
  const { status: signin } = useSignin();
  const [view, setView] = useState<SetupView | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    fetchSetup(projectId).then((v) => { if (alive) setView(v); }).catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [projectId]);
  return { state: view ? welcomeState(view, signin) : null, view, failed };
}

export function FirstRun({ projectId, cards }: { projectId: string; actor: string; cards: ProjectCard[] }) {
  const { state, view, failed } = useWelcome(projectId);
  const url = usePxUrl();
  const withRepo = cards.filter((c) => c.repo);
  const askedId = usePageSearch().get("project_id");
  const guided = url.firstRun && withRepo.length > 0 ? (withRepo.find((c) => c.id === askedId) ?? withRepo[0]) : null;
  // Unknown setup (an older API, or it failed): every door is open; nothing is ticked that we don't know.
  const s: WelcomeState = state ?? { github: { done: false, login: null, repos: 0 }, model: { done: false, label: null, kind: null }, sample: { done: false } };
  const hasProject = cards.length > 0;
  if (guided) return <Guided card={guided} s={s} />;
  const current = !s.github.done ? 1 : !s.model.done ? 2 : 3;
  const strip: { key: string; label: string; state: StepState; sub: string; href: string }[] = [
    { key: "github", label: "Connect GitHub", state: s.github.done ? "done" : "now", sub: s.github.done ? (s.github.login ?? "") : "", href: setupHref("repository") },
    { key: "model", label: "Add a model", state: s.model.done ? "done" : current === 2 ? "now" : "todo", sub: s.model.label ?? "", href: setupHref("models") },
    { key: "project", label: "Add project", state: hasProject ? "done" : current === 3 ? "now" : "todo", sub: "", href: "" },
    { key: "onboard", label: "Helm onboards", state: "todo", sub: "", href: "" },
    { key: "pr", label: "First PR", state: s.sample.done ? "done" : "todo", sub: "", href: "" },
  ];
  return (
    <div className="px-stack" data-testid="px-first-run" data-step={current}>
      <nav className="px-crumbs" aria-label="Breadcrumb"><span aria-current="page">Projects</span></nav>
      <section className="px-hero" aria-labelledby="px-fr-title">
        <Orbit />
        <h1 id="px-fr-title">Add your first project</h1>
        <p>Point the crew at a repo or an idea. Helm, your captain, reads it, gets it ready, and suggests the first piece of work.</p>
        <Journey nodes={strip} testid="px-fr-strip" onOpen={(n) => { if (n.href) navigateTo(n.href); else if (n.key === "project") openAddProject("import"); }} />
        <small className="px-faint">About 10 minutes · change anything later in <a href={pxHref("?area=setup")}>Repo and models</a></small>
        {failed ? <Said text="Couldn't read setup just now — every step stays open." /> : null}
      </section>

      <div className="px-grid px-g3">
        <div className="px-door" data-current={current === 1 || undefined} data-done={s.github.done || undefined} data-testid="px-fr-github">
          <div className="px-row"><Ico icon={GithubLogo} c="teal" size="l" /><span className="px-tag" data-tone={s.github.done ? "green" : "blue"}>{s.github.done ? <><Check size={10} /> Step 1</> : "Step 1"}</span></div>
          <h3>Connect GitHub</h3>
          {s.github.done ? (
            <p data-testid="px-fr-github-done">Connected{s.github.login ? ` as ${s.github.login}` : ""} · {s.github.repos} repo{s.github.repos === 1 ? "" : "s"}</p>
          ) : <p>Pick the repos the crew may work on.</p>}
          <div className="px-row px-wrap">
            {s.github.done
              ? <a className="px-btn" href={setupHref("repository")} data-testid="px-fr-github-more"><Plus size={12} /> Another account</a>
              : <a className="px-btn" data-kind={current === 1 ? "primary" : undefined} href={setupHref("repository")} data-testid="px-fr-github-go"><GithubLogo size={14} /> Connect</a>}
          </div>
        </div>
        <div className="px-door" data-current={current === 2 || undefined} data-done={s.model.done || undefined} data-testid="px-fr-model">
          <div className="px-row"><Ico icon={Brain} c="violet" size="l" /><span className="px-tag" data-tone={s.model.done ? "green" : "blue"}>{s.model.done ? <><Check size={10} /> Step 2</> : "Step 2"}</span></div>
          <h3>Add a model</h3>
          {s.model.done ? <p data-testid="px-fr-model-done">{s.model.label} {s.model.kind === "subscription" ? "signed in" : "connected"}</p>
            : <p>A subscription, an API key, or a local model.</p>}
          <div className="px-row px-wrap">
            {s.model.done
              ? <a className="px-btn" href={setupHref("models")} data-testid="px-fr-model-more"><Plus size={12} /> Another model</a>
              : <a className="px-btn" data-kind={current === 2 ? "primary" : undefined} href={setupHref("models")} data-testid="px-fr-model-go"><Plus size={14} /> Add model</a>}
          </div>
        </div>
        <div className="px-door" data-current={current === 3 || undefined} data-testid="px-fr-project">
          <div className="px-row"><Ico icon={FolderPlus} c="green" size="l" /><span className="px-tag" data-tone="blue">Step 3</span></div>
          <h3>Add a project</h3>
          <div className="px-grid px-g2" style={{ gap: 8 }}>
            <GatedButton permission="project:create" className="px-btn" data-kind={current === 3 ? "primary" : undefined} data-testid="px-fr-existing"
                         onClick={() => openAddProject("import")}><GitBranch size={14} /> Existing repo</GatedButton>
            <GatedButton permission="project:create" className="px-btn" data-testid="px-fr-idea" onClick={() => openAddProject("idea")}>
              <Lightbulb size={14} /> From an idea</GatedButton>
          </div>
          <div className="px-row px-wrap" style={{ fontSize: ".6rem" }}>
            <button type="button" className="px-btn" data-kind="ghost" data-size="s" data-testid="px-fr-sample" onClick={() => openAddProject("sample")}><Flask size={12} /> Try a sample</button>
            <a className="px-btn" data-kind="ghost" data-size="s" href={pxHref(QUICK_PR)} data-testid="px-fr-quick-pr"><Lightning size={12} /> One small PR</a>
          </div>
          {view?.try.last_run ? <LastRun run={view.try.last_run} /> : null}
        </div>
      </div>

      <section className="px-card" aria-labelledby="px-fr-flow">
        <div className="px-card-h"><Ico icon={Path} c="blue" /><h2 id="px-fr-flow">What happens to each piece of work</h2></div>
        <Flow nodes={FLOW} testid="px-fr-flow" />
      </section>
    </div>
  );
}

function LastRun({ run }: { run: NonNullable<SetupView["try"]["last_run"]> }) {
  const runHref = pxHref(`?area=delivery_workflows&wfr=${encodeURIComponent(run.run_id)}`);
  return (
    <div className="px-row px-wrap" style={{ fontSize: ".6rem" }} data-testid="px-fr-last-run">
      <span className="px-faint">Last sample:</span>
      {run.pr_url ? <a className="px-chip" href={run.pr_url} target="_blank" rel="noreferrer noopener" data-testid="px-fr-last-pr">{run.pill.label} ↗</a>
        : <a className="px-chip" href={runHref}>{run.pill.label}</a>}
      {run.pill.tone === "red" ? <a className="px-chip" href={runHref} data-testid="px-fr-last-open">Open run</a> : null}
    </div>
  );
}

/** The illustrated hero: a folder with the crew in orbit around it. */
function Orbit() {
  const crew = [
    { name: "Helm", role_key: "engineering_captain", x: 20, y: 52 }, { name: "Juno", role_key: "pm", x: 78, y: 10 },
    { name: "Forge", role_key: "software_engineer", x: 186, y: 4 }, { name: "Vera", role_key: "qa", x: 246, y: 52 },
    { name: "Beacon", role_key: "sre", x: 200, y: 108 },
  ];
  return (
    <div style={{ position: "relative", width: 300, height: 150, maxWidth: "100%" }} aria-hidden>
      <svg className="px-orbit" viewBox="0 0 300 150">
        <ellipse cx="150" cy="75" rx="128" ry="58" fill="none" stroke="var(--border-strong)" strokeDasharray="3 6" />
        <rect x="104" y="48" width="92" height="62" rx="9" fill="color-mix(in srgb, var(--accent) 22%, transparent)" stroke="var(--accent)" strokeWidth="2" />
        <path d="M104 56 v-10 a6 6 0 0 1 6 -6 h26 l8 8 h46" fill="none" stroke="var(--accent)" strokeWidth="2" />
        <line x1="128" y1="80" x2="172" y2="80" stroke="var(--teal)" strokeWidth="3" strokeLinecap="round" />
        <line x1="134" y1="90" x2="166" y2="90" stroke="var(--teal)" strokeWidth="3" strokeLinecap="round" opacity=".6" />
        <circle cx="150" cy="18" r="3" fill="var(--teal)" /><circle cx="276" cy="40" r="3" fill="#8b7cf6" /><circle cx="96" cy="128" r="3" fill="var(--teal)" />
      </svg>
      {crew.map((c) => (
        <span key={c.name} style={{ position: "absolute", left: c.x, top: c.y }}><Face agent={c} size={34} /></span>
      ))}
    </div>
  );
}

/** 0b: the 6-step guide beside Helm's live onboarding diagram, ending at suggested first work. */
function Guided({ card, s }: { card: ProjectCard; s: WelcomeState }) {
  const ob = usePxRead<Onboarding>(PX_PATHS.onboarding(card.id), parseOnboarding, { everyMs: 5_000, demo: () => parseOnboarding(FX_ONBOARDING) });
  const o = ob.data;
  const discover = o?.stages.find((x) => x.key === "discover");
  const ready = o?.stages.find((x) => x.key === "ready");
  const steps: { label: string; sub: string; state: StepState; icon: React.ReactNode }[] = [
    { label: "Connect GitHub", sub: s.github.done ? `${s.github.login ?? "connected"} · ${s.github.repos} repos` : "not yet", state: s.github.done ? "done" : "now", icon: <GithubLogo size={13} /> },
    { label: "Add a model", sub: s.model.label ?? "not yet", state: s.model.done ? "done" : "now", icon: <Brain size={13} /> },
    { label: "Add project", sub: card.repo || card.name, state: "done", icon: <FolderPlus size={13} /> },
    { label: "Helm onboards", sub: discover ? `Discover · ${discover.sub || discover.state}` : "starting", state: !o ? "now" : discover?.state === "done" ? "done" : "now", icon: <Compass size={13} /> },
    { label: "Ready for work", sub: ready?.sub || "checks the agent runs", state: ready?.state === "done" ? "done" : ready?.state === "now" ? "now" : "todo", icon: <Signpost size={13} /> },
    { label: "First work", sub: "suggested → your PR", state: o?.first_work ? "now" : "todo", icon: <GitPullRequest size={13} /> },
  ];
  const done = steps.filter((x) => x.state === "done").length;
  const [said, setSaid] = useState("");
  const startFirst = async () => {
    if (!o?.first_work) return;
    const r = await startWork(card.id, o.first_work.source === "bug" ? "bug" : "ticket", o.first_work.label);
    if (r.ok) { setSaid(r.demo ? "Sample mode — nothing was sent." : "Started."); if (!r.demo) navigateTo(projectHref(card.id, { tab: "work" })); }
    else setSaid(r.error ?? "It didn't start.");
  };
  return (
    <div className="px-stack" data-testid="px-first-run-guided">
      <nav className="px-crumbs" aria-label="Breadcrumb">
        <a href={pxHref("?area=projects")}>Projects</a><span aria-hidden>/</span><span aria-current="page">First project</span>
      </nav>
      <div className="px-guide">
        <section className="px-card" aria-labelledby="px-g-title">
          <div className="px-card-h"><Ico icon={Signpost} c="blue" /><h2 id="px-g-title">Your first project</h2><span className="px-more px-mono px-faint">{done} / 6</span></div>
          <ol style={{ listStyle: "none", margin: 0, padding: 0 }} data-testid="px-guide-steps">
            {steps.map((st) => (
              <li key={st.label} className="px-gstep" data-s={st.state}>
                <span className="px-ck-b" aria-hidden>{st.state === "done" ? <Check size={13} weight="bold" /> : st.icon}</span>
                <span><b>{st.label}</b><small>{st.sub}</small></span>
              </li>
            ))}
          </ol>
          <div className="px-row px-wrap" style={{ borderTop: "1px solid var(--border)", paddingTop: 8, marginTop: 6, fontSize: ".6rem" }}>
            <a href={setupHref("repository")} data-testid="px-guide-account"><Plus size={10} /> another account</a>
            <a href={setupHref("models")} data-testid="px-guide-model"><Plus size={10} /> another model</a>
          </div>
        </section>
        <div className="px-stack">
          <section className="px-card" aria-labelledby="px-g-live">
            <div className="px-card-h"><Ico icon={Compass} c="teal" /><h2 id="px-g-live">Helm is reading your repo</h2><span className="px-tag" data-tone="blue"><span className="px-dot" data-s="now" aria-hidden /> live</span>
              {o ? <span className="px-more px-mono px-faint">{Math.floor(o.elapsed_s / 60)}m {o.elapsed_s % 60}s</span> : null}</div>
            {o ? <DiscoverDiagram nodes={o.discover.nodes} /> : (
              <p className="px-faint" data-testid="px-guide-waiting">{ob.state === "missing" ? "Helm's live view arrives with the next BOSS update — the project page shows its progress meanwhile." : "Starting…"}</p>
            )}
            <a className="px-btn" data-kind="ghost" href={projectHref(card.id)} data-testid="px-guide-open-project">Open {card.name} →</a>
          </section>
          <div className="px-grid px-g2">
            <section className="px-card" aria-labelledby="px-g-you">
              <div className="px-card-h"><Ico icon={HandWaving} c="amber" /><h2 id="px-g-you">Only you can</h2></div>
              {o && o.only_you.length ? o.only_you.map((it) => (
                <div key={it.key} className="px-ny" data-testid={`px-guide-you-${it.key}`}>
                  <span className="px-ny-ico" aria-hidden>!</span>
                  <div style={{ flex: 1, minWidth: 0 }}><div className="px-ny-t">{it.label}</div><small className="px-faint">{it.detail}</small></div>
                  {it.action ? <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(it.action.href)}>{it.action.label}</a> : null}
                </div>
              )) : <p className="px-faint">Nothing yet — Helm asks only for what it can't do itself.</p>}
              <small className="px-faint">Everything else Helm does itself.</small>
            </section>
            <section className="px-card" aria-labelledby="px-g-first">
              <div className="px-card-h"><Ico icon={Sparkle} c="violet" /><h2 id="px-g-first">First work, suggested</h2><Face agent={{ name: "Helm", role_key: "engineering_captain" }} size={20} /></div>
              {o?.first_work ? (
                <>
                  <b style={{ fontSize: ".72rem" }}>{o.first_work.label}</b>
                  <div className="px-row px-wrap" style={{ margin: "6px 0 10px" }}>
                    {o.first_work.source ? <span className="px-tag" data-tone="red">{o.first_work.source.toUpperCase()}</span> : null}
                    {o.first_work.estimate_min !== null ? <span className="px-tag" data-tone="cost">≈ {o.first_work.estimate_min} min</span> : null}
                    {o.first_work.estimate_usd !== null ? <span className="px-tag" data-tone="cost">≈ ${o.first_work.estimate_usd.toFixed(2)}</span> : null}
                  </div>
                  <div className="px-row px-wrap">
                    <GatedButton permission="workflow_run:start" projectId={card.id} className="px-btn" data-kind="primary" data-testid="px-guide-start"
                                 onClick={() => void startFirst()}><Play size={12} weight="fill" /> Start when ready</GatedButton>
                    {o.first_work.others_href ? <a className="px-btn" data-kind="ghost" href={pxHref(o.first_work.others_href)}>Other ideas</a> : null}
                  </div>
                  <Said text={said} />
                </>
              ) : <p className="px-faint">Helm suggests one as soon as it has read the repo.</p>}
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}
