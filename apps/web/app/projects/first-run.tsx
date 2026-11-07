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
  PX_PATHS, firstWorkAccept, navigateTo, usePageSearch, parseOnboarding, projectHref, pxHref, readPxUrl, runAccept, startOnboarding, usePxRead,
  type Onboarding, type ProjectCard, type StepState,
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

export type WorkspaceStatus = { state: WelcomeState | null; view: SetupView | null; status: "loading" | "ready" | "failed" };

/** GitHub and the model are WORKSPACE settings (Setup › Repo and models), never a project's: these
 *  screens only read them, from the same view Repo and models draws, and send you there to change
 *  them. Read again when you come back (focus, the tab showing again, Back in the console). */
export function useWelcome(projectId: string): WorkspaceStatus {
  const { status: signin } = useSignin();
  const [view, setView] = useState<SetupView | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "failed">("loading");
  useEffect(() => {
    let alive = true;
    const read = () => fetchSetup(projectId)
      .then((v) => { if (alive) { setView(v); setStatus("ready"); } })
      .catch(() => { if (alive) setStatus((cur) => (cur === "ready" ? cur : "failed")); });
    void read();
    const again = () => { if (document.visibilityState !== "hidden") void read(); };
    window.addEventListener("focus", again);
    window.addEventListener("popstate", again);
    document.addEventListener("visibilitychange", again);
    return () => {
      alive = false;
      window.removeEventListener("focus", again);
      window.removeEventListener("popstate", again);
      document.removeEventListener("visibilitychange", again);
    };
  }, [projectId]);
  return { state: view ? welcomeState(view, signin) : null, view, status };
}

const MODE_WORDS: Record<string, string> = { subscription: "subscription", metered: "API key", api_key: "API key", local: "local" };
const modelWords = (s: WelcomeState) => [s.model.label, s.model.kind ? MODE_WORDS[s.model.kind] ?? s.model.kind : null].filter(Boolean).join(" · ");
const githubWords = (s: WelcomeState) => ["Connected", s.github.login, `${s.github.repos} repo${s.github.repos === 1 ? "" : "s"}`].filter(Boolean).join(" · ");

/** One workspace step's card: configured (read-only, Manage ›), to do (a button to Repo and models),
 *  or unknown (status can't be read: a neutral link, never a false "Configured"). */
function WorkspaceDoor({ id, step, title, icon, tone, known, loading, done, doneText, todoText, todoLabel, focus, primary }: {
  id: string; step: number; title: string; icon: typeof Brain; tone: "teal" | "violet"; known: boolean; loading: boolean; done: boolean;
  doneText: string; todoText: string; todoLabel: string; focus: "repository" | "models"; primary: boolean;
}) {
  const href = setupHref(focus);
  return (
    <div className="px-door" data-current={primary || undefined} data-done={(known && done) || undefined} data-testid={`px-fr-${id}`}
         data-state={!known ? "unknown" : done ? "configured" : "todo"}>
      <div className="px-row"><Ico icon={icon} c={tone} size="l" />
        <span className="px-tag" data-tone={known && done ? "green" : "blue"}>{known && done ? <><Check size={10} /> Configured</> : `Step ${step}`}</span></div>
      <h3>{title}</h3>
      {!known ? (
        <>
          <p className="px-faint" role="status">{loading ? "Checking…" : "Couldn't read this just now."}</p>
          {loading ? null : <a className="px-btn" data-kind="ghost" data-size="s" href={href} data-testid={`px-fr-${id}-check`}>Check in Repo and models ›</a>}
        </>
      ) : done ? (
        <>
          <p data-testid={`px-fr-${id}-done`}><Check size={11} weight="bold" style={{ color: "var(--green)" }} aria-hidden /> {doneText}</p>
          <a className="px-btn" data-kind="ghost" data-size="s" href={href} data-testid={`px-fr-${id}-manage`}>Manage ›</a>
        </>
      ) : (
        <>
          <p>{todoText}</p>
          <div className="px-row px-wrap">
            <a className="px-btn" data-kind={primary ? "primary" : undefined} href={href} data-testid={`px-fr-${id}-go`}>{todoLabel}</a>
          </div>
        </>
      )}
    </div>
  );
}

export function FirstRun({ projectId, cards }: { projectId: string; actor: string; cards: ProjectCard[] }) {
  const { state, view, status } = useWelcome(projectId);
  const url = usePxUrl();
  const withRepo = cards.filter((c) => c.repo);
  const askedId = usePageSearch().get("project_id");
  const guided = url.firstRun && withRepo.length > 0 ? (withRepo.find((c) => c.id === askedId) ?? withRepo[0]) : null;
  const known = status === "ready" && state !== null;
  // Unknown (loading, or it can't be read): nothing is ticked that we don't know, nothing is blocked.
  const s: WelcomeState = state ?? { github: { done: false, login: null, repos: 0 }, model: { done: false, label: null, kind: null }, sample: { done: false } };
  const hasProject = cards.length > 0;
  if (guided) return <Guided card={guided} s={s} known={known} />;
  const current = !known ? 3 : !s.github.done ? 1 : !s.model.done ? 2 : 3;
  const strip: { key: string; label: string; state: StepState; sub: string; href: string }[] = [
    { key: "github", label: "Connect GitHub", state: known && s.github.done ? "done" : current === 1 ? "now" : "todo", sub: known && s.github.done ? (s.github.login ?? "") : "", href: setupHref("repository") },
    { key: "model", label: "Add a model", state: known && s.model.done ? "done" : current === 2 ? "now" : "todo", sub: known && s.model.done ? (s.model.label ?? "") : "", href: setupHref("models") },
    { key: "project", label: "Add project", state: hasProject ? "done" : current === 3 ? "now" : "todo", sub: "", href: "" },
    { key: "onboard", label: "Helm onboards", state: "todo", sub: "", href: "" },
    { key: "pr", label: "First PR", state: s.sample.done ? "done" : "todo", sub: "", href: "" },
  ];
  return (
    <div className="px-stack" data-testid="px-first-run" data-step={current} data-status={status}>
      <nav className="px-crumbs" aria-label="Breadcrumb"><span aria-current="page">Projects</span></nav>
      <section className="px-hero" aria-labelledby="px-fr-title">
        <Orbit />
        <h1 id="px-fr-title">Add your first project</h1>
        <p>Point the crew at a repo or an idea. Helm, your captain, reads it, gets it ready, and suggests the first piece of work.</p>
        <Journey nodes={strip} testid="px-fr-strip" onOpen={(n) => { if (n.href) navigateTo(n.href); else if (n.key === "project") openAddProject("import"); }} />
        <small className="px-faint">About 10 minutes · change anything later in <a href={pxHref("?area=setup")} data-testid="px-fr-setup">Repo and models</a></small>
      </section>

      <div className="px-grid px-g3">
        <WorkspaceDoor id="github" step={1} title="Connect GitHub" icon={GithubLogo} tone="teal" known={known} loading={status === "loading"} done={s.github.done}
                       doneText={githubWords(s)} todoText="Pick the repos the crew may work on." todoLabel="Connect GitHub ›" focus="repository" primary={current === 1} />
        <WorkspaceDoor id="model" step={2} title="Add a model" icon={Brain} tone="violet" known={known} loading={status === "loading"} done={s.model.done}
                       doneText={modelWords(s)} todoText="A subscription, an API key, or a local model." todoLabel="Add a model ›" focus="models" primary={current === 2} />
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

/** 0b: the project's 4-step guide (workspace settings are a read-only row above it) beside Helm's live onboarding diagram, ending at suggested first work. */
function Guided({ card, s, known }: { card: ProjectCard; s: WelcomeState; known: boolean }) {
  const ob = usePxRead<Onboarding>(PX_PATHS.onboarding(card.id), parseOnboarding, { everyMs: 5_000, demo: () => parseOnboarding(FX_ONBOARDING) });
  const o = ob.data;
  const discover = o?.stages.find((x) => x.key === "discover");
  const ready = o?.stages.find((x) => x.key === "ready");
  const steps: { label: string; sub: string; state: StepState; icon: React.ReactNode }[] = [
    { label: "Add project", sub: card.repo || card.name, state: "done", icon: <FolderPlus size={13} /> },
    { label: "Helm onboards", sub: discover ? `Discover · ${discover.sub || discover.state}` : "starting", state: !o ? "now" : discover?.state === "done" ? "done" : "now", icon: <Compass size={13} /> },
    { label: "Ready for work", sub: ready?.sub || "checks the agent runs", state: ready?.state === "done" ? "done" : ready?.state === "now" ? "now" : "todo", icon: <Signpost size={13} /> },
    { label: "First work", sub: "suggested → your PR", state: o?.first_work && ready?.state === "done" ? "now" : "todo", icon: <GitPullRequest size={13} /> },
  ];
  const done = steps.filter((x) => x.state === "done").length;
  const [said, setSaid] = useState("");
  const startFirst = async () => {
    if (!o?.first_work) return;
    const r = await runAccept(card.id, firstWorkAccept(card.id, o.first_work), "Start");
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
          <div className="px-card-h"><Ico icon={Signpost} c="blue" /><h2 id="px-g-title">Your first project</h2><span className="px-more px-mono px-faint">{done} / {steps.length}</span></div>
          {/* WORKSPACE READY: GitHub and the model are workspace settings (Setup › Repo and models),
              never a project's — shown read-only here, each a link to its own page. */}
          <div className="px-row px-wrap" data-testid="px-guide-workspace" style={{ marginBottom: 8, fontSize: ".6rem" }}>
            <span className="px-lbl">Workspace</span>
            <a className="px-chip" href={setupHref("repository")} data-testid="px-guide-ws-github" title="GitHub accounts live in Setup › Repo and models"
               style={{ color: known && s.github.done ? "var(--green)" : "var(--amber)" }}>
              <GithubLogo size={11} aria-hidden /> {!known ? "GitHub ?" : s.github.done ? `GitHub ✓ ${s.github.login ?? ""} · ${s.github.repos} repo${s.github.repos === 1 ? "" : "s"}` : "GitHub — connect"}</a>
            <a className="px-chip" href={setupHref("models")} data-testid="px-guide-ws-model" title="Models live in Setup › Repo and models"
               style={{ color: known && s.model.done ? "var(--green)" : "var(--amber)" }}>
              <Brain size={11} aria-hidden /> {!known ? "Model ?" : s.model.done ? `Model ✓ ${s.model.label ?? ""}` : "Model — add"}</a>
          </div>
          <ol style={{ listStyle: "none", margin: 0, padding: 0 }} data-testid="px-guide-steps">
            {steps.map((st) => (
              <li key={st.label} className="px-gstep" data-s={st.state}>
                <span className="px-ck-b" aria-hidden>{st.state === "done" ? <Check size={13} weight="bold" /> : st.icon}</span>
                <span><b>{st.label}</b><small>{st.sub}</small></span>
              </li>
            ))}
          </ol>

        </section>
        <div className="px-stack">
          <section className="px-card" aria-labelledby="px-g-live">
            <div className="px-card-h"><Ico icon={Compass} c="teal" /><h2 id="px-g-live">Helm is reading your repo</h2><span className="px-tag" data-tone="blue"><span className="px-dot" data-s="now" aria-hidden /> live</span>
              {o ? <span className="px-more px-mono px-faint">{Math.floor(o.elapsed_s / 60)}m {o.elapsed_s % 60}s</span> : null}</div>
            {o && o.state === "not_started" ? (
              <div className="px-row px-wrap" data-testid="px-guide-not-started">
                <p className="px-faint" style={{ margin: 0 }}>Helm hasn&apos;t started reading this repo yet.</p>
                <GatedButton permission="workflow_run:start" projectId={card.id} className="px-btn" data-kind="primary" data-size="s" data-testid="px-guide-start-onboarding"
                             onClick={() => void startOnboarding(card.id).then((r) => { setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Onboarding started.") : (r.error ?? "It didn't start.")); if (r.ok) ob.refresh(); })}>
                  <Compass size={12} /> Start onboarding</GatedButton>
              </div>
            ) : o ? <DiscoverDiagram nodes={o.discover.nodes} /> : (
              <p className="px-faint" data-testid="px-guide-waiting">{ob.state === "missing" ? "Helm's live view didn't load — the project page shows its progress." : ob.state === "error" ? (ob.error ?? "Helm's live view didn't load.") : "Starting…"}</p>
            )}
            <a className="px-btn" data-kind="ghost" href={projectHref(card.id)} data-testid="px-guide-open-project">Open {card.name} →</a>
          </section>
          <div className="px-grid px-g2">
            <section className="px-card" aria-labelledby="px-g-you">
              <div className="px-card-h"><Ico icon={HandWaving} c="amber" /><h2 id="px-g-you">Only you can</h2></div>
              {known && !s.github.done ? (
                <div className="px-ny" data-testid="px-guide-you-github"><span className="px-ny-ico" aria-hidden>!</span>
                  <div style={{ flex: 1, minWidth: 0 }}><div className="px-ny-t">Connect GitHub</div><small className="px-faint">A workspace setting</small></div>
                  <a className="px-btn" data-kind="amber" data-size="s" href={setupHref("repository")}>Connect in Setup ›</a></div>
              ) : null}
              {known && !s.model.done ? (
                <div className="px-ny" data-testid="px-guide-you-model"><span className="px-ny-ico" aria-hidden>!</span>
                  <div style={{ flex: 1, minWidth: 0 }}><div className="px-ny-t">Connect a model</div><small className="px-faint">A workspace setting</small></div>
                  <a className="px-btn" data-kind="amber" data-size="s" href={setupHref("models")}>Connect a model in Setup ›</a></div>
              ) : null}
              {o && o.only_you.length ? o.only_you.map((it) => (
                <div key={it.key} className="px-ny" data-testid={`px-guide-you-${it.key}`}>
                  <span className="px-ny-ico" aria-hidden>!</span>
                  <div style={{ flex: 1, minWidth: 0 }}><div className="px-ny-t">{it.label}</div><small className="px-faint">{it.detail}</small></div>
                  {it.action ? <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(it.action.href)}>{it.action.label}</a> : null}
                </div>
              )) : (known && s.github.done && s.model.done) || !known ? <p className="px-faint">Nothing yet — Helm asks only for what it can&apos;t do itself.</p> : null}
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
