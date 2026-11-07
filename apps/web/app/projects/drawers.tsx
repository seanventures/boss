"use client";

/**
 * SIDE DRAWERS (screen 5b, contract §10): `&drawer=<kind>:<id>`, deep-linkable, Back closes.
 *   work:<id>     how the work went, step by step, its one action and every link (5b)
 *   needs:<id>    what waits on you and why, with its action
 *   stage:<key>   a journey stage (and stage:how — how the journey reads)
 *   env:<key>     an environment's full picture;  deploy:<id>  one deploy
 * Prose lives here, never on the page.
 */
import React, { useEffect, useState } from "react";
import {
  ArrowsOut, ChatCircleDots, Code, FileText, FlowArrow, GitMerge, GitPullRequest, Globe, Graph, ListChecks, PaperPlaneRight, Path, PauseCircle, Question,
  RocketLaunch, Terminal, User, Warning, Wrench,
} from "@phosphor-icons/react";

import { ATTEMPT_WORDS, KIND_WORDS, PX_PATHS, findWork, parseDeployments, pxHref, toTab, usePxRead, writePxUrl, type Deployments, type WorkItem } from "../../lib/project-experience";
import { FX_DEPLOYMENTS } from "../../lib/project-experience-fixtures";
import { Drawer, Empty, Face, Flow, HEALTH_WORD, Ico, PxLink, STATE_WORD, Spark, Tag } from "./kit";
import { ActionButton } from "./tab-overview";
import { DrawerAttempts } from "./tab-work";
import { CaptainDrawer } from "./captain-drawer";
import { DecisionActs, NeedsBody, StageBody, WorkStoryBody, stageIcon, useWorkStory } from "./drawers-more";

/** Each Needs-you item's icon, by what it asks (as on Overview). */
const NEED_ICON: Record<string, typeof GitMerge> = { merge: GitMerge, deploy: RocketLaunch, plan: ListChecks, setup: Wrench, question: Question };
import { DemoLoginRow } from "./tab-settings";
import { KnowledgeDrawer } from "./tab-knowledge";
import { whenLocal } from "../../lib/local-time";
import type { PxCtx } from "./project-page";

const STEP_WORDS: Record<string, string> = { plan: "Plan", design: "You approved", decide: "You approved", build: "Build", review: "Review & test", pr: "Merge", ship: "Staging" };

/** Drawer links that name a PLACE on this page rather than a drawer (the engine's hrefs, §10):
 *  `section:<key>` is Settings at that section; `dossier` and `doc:<path>` are Repo & knowledge;
 *  `onboarding` is screen 10's own page (the Overview while the project still onboards): the
 *  wireframes draw no onboarding drawer, so the old quick-view drawer opens the full view.
 *  (`captain` is its own drawer, screen 09.) */
function placeOf(drawer: { kind: string; id: string }, onboarding: boolean): Record<string, string | null> | null {
  if (drawer.kind === "section") return { tab: "settings", section: drawer.id || null, drawer: null };
  if (drawer.kind === "onboarding") return { tab: onboarding ? null : "onboarding", drawer: null };
  return null;
}

export function DrawerHost({ ctx, drawer, onClose }: { ctx: PxCtx; drawer: { kind: string; id: string }; onClose: () => void }) {
  const place = placeOf(drawer, ctx.overview.project.stage === "onboarding");
  useEffect(() => {
    if (!place) return;
    writePxUrl({ ...place, ...(place.tab ? { tab: place.tab === "onboarding" ? "onboarding" : toTab(place.tab) === "overview" ? null : place.tab } : {}) }, "replace");
  }, [place === null ? "" : JSON.stringify(place)]); // eslint-disable-line react-hooks/exhaustive-deps
  if (place) return null;
  // The full dossier, a doc and Add doc (screen 06): Repo & knowledge's own drawers.
  if (drawer.kind === "dossier" || drawer.kind === "doc") return <KnowledgeDrawer ctx={ctx} drawer={drawer} onClose={onClose} />;
  if (drawer.kind === "captain") return <CaptainDrawer ctx={ctx} onClose={onClose} />;
  // `run:<id>` and `build:<id>` (the engine's WorkItem hrefs and ⌘K) are the work drawer.
  if (drawer.kind === "work" || drawer.kind === "run" || drawer.kind === "build") {
    // An attempt of a grouped piece of work (a follow-up, a retry) opens as itself.
    const w = findWork(ctx.work?.items ?? [], drawer.id) ?? findWork(ctx.overview.recent_work, drawer.id);
    return w ? <WorkDrawer ctx={ctx} w={w} onClose={onClose} /> : <Missing what="This work item" onClose={onClose} loading={!ctx.work} />;
  }
  if (drawer.kind === "needs") {
    const n = ctx.overview.needs_you.find((x) => x.id === drawer.id);
    if (!n) return <Missing what="This item" onClose={onClose} />;
    return (
      <Drawer title={n.label} icon={<Ico icon={NEED_ICON[n.kind] ?? Warning} c="amber" />} onClose={onClose} testid="px-drawer-needs" sub={<Tag tone="amber">needs you</Tag>}
              footer={<>{n.action?.decision_id ? <DecisionActs ctx={ctx} n={n} />
                : n.action ? <ActionButton ctx={ctx} label={n.action.label} href={n.action.href} amber testid="px-drawer-needs-act" /> : null}<AskBox ctx={ctx} about={n.label} /></>}>
        <NeedsBody ctx={ctx} n={n} />
      </Drawer>
    );
  }
  if (drawer.kind === "stage") {
    if (drawer.id === "how") {
      return (
        <Drawer title="How the journey reads" icon={<Ico icon={Path} c="teal" />} onClose={onClose} testid="px-drawer-stage">
          <p style={{ margin: 0, lineHeight: 1.55 }}>The journey is the project&apos;s whole life in one strip. Onboarded and Ready are Helm&apos;s onboarding: it reads the repo,
            checks it builds and tests, and asks only for what it can&apos;t do. Backlog is the work Marshal ranked. Working is the verified loop — every item is planned,
            built, reviewed and opened as a PR. Staging deploys by policy; production is always a person&apos;s call.</p>
          <Flow nodes={[{ label: "Done", state: "done" }, { label: "Now", state: "now" }, { label: "Needs you", state: "waiting" }, { label: "Not yet", state: "todo" }]} />
          <p className="px-faint" style={{ margin: 0 }}>A pulse means live; amber means only you can move it. Each stage opens what happened there.</p>
        </Drawer>
      );
    }
    const n = ctx.overview.journey.find((x) => x.key === drawer.id);
    if (!n) return <Missing what="This stage" onClose={onClose} />;
    return (
      <Drawer title={n.label} sub={<><Tag tone={n.state === "done" ? "green" : n.state === "waiting" ? "amber" : n.state === "todo" ? undefined : "blue"}>{STATE_WORD[n.state]}</Tag>{n.sub ? <span className="px-mono">{n.sub}</span> : null}</>}
              icon={<Ico icon={stageIcon(n.key)} c={n.state === "waiting" ? "amber" : n.state === "done" ? "green" : "teal"} />} onClose={onClose} testid="px-drawer-stage"
              footer={<AskBox ctx={ctx} about={n.label} />}>
        <StageBody ctx={ctx} n={n} />
        <div className="px-row px-wrap">
          {n.key === "onboarding" || n.key === "ready" ? <>
            <button type="button" className="px-chip" data-testid="px-stage-onboarding" onClick={() => writePxUrl({ tab: "onboarding", drawer: null })}>Onboarding</button>
            <button type="button" className="px-chip" onClick={() => ctx.tab("knowledge")}>Dossier</button></> : null}
          {n.key === "backlog" ? <button type="button" className="px-chip" onClick={() => ctx.tab("roadmap")}>Roadmap</button> : null}
          {n.key === "working" ? <button type="button" className="px-chip" onClick={() => ctx.tab("work")}>Work</button> : null}
          {n.key === "staging" || n.key === "production" ? <button type="button" className="px-chip" onClick={() => ctx.tab("deployments")}>Deployments</button> : null}
        </div>
      </Drawer>
    );
  }
  if (drawer.kind === "env" || drawer.kind === "deploy") return <DeployDrawer ctx={ctx} drawer={drawer} onClose={onClose} />;
  if (drawer.kind === "rec") {
    return (
      <Drawer title="Recommendation" icon={<Ico icon={Path} c="violet" />} onClose={onClose} testid="px-drawer-rec">
        <p style={{ margin: 0 }}>Recommendations are ranked, accepted and rejected on the Roadmap.</p>
        <button type="button" className="px-btn" data-kind="primary" onClick={() => ctx.tab("roadmap")}>Open the Roadmap</button>
      </Drawer>
    );
  }
  return <Missing what="This" onClose={onClose} />;
}

function Missing({ what, onClose, loading }: { what: string; onClose: () => void; loading?: boolean }) {
  return (
    <Drawer title={loading ? "Loading…" : "Not found"} onClose={onClose} testid="px-drawer-missing">
      {loading ? <p className="px-faint">Reading it…</p> : <Empty icon={Warning} tone="amber" title={`${what} isn't here any more`} line="It may have finished and moved, or the link is old." />}
    </Drawer>
  );
}

/** "Ask Helm about this…": the project's captain dock, with the question already in it. */
function AskBox({ ctx, about, placeholder }: { ctx: PxCtx; about: string; placeholder?: string }) {
  const [q, setQ] = useState("");
  const name = ctx.overview.captain?.name ?? "BOSS";
  return (
    <form className="px-row" style={{ marginTop: "auto", border: "1px solid var(--border-strong)", borderRadius: 10, padding: "6px 8px", background: "var(--surface-2)" }}
          onSubmit={(e) => { e.preventDefault(); ctx.ask(q.trim() ? `${q.trim()}\n\n(About: ${about})` : `Tell me about "${about}".`); }} data-testid="px-drawer-ask">
      {ctx.overview.captain ? <Face agent={ctx.overview.captain} size={20} /> : <ChatCircleDots size={16} aria-hidden />}
      <input className="px-input" style={{ border: 0, background: "transparent", padding: 4 }} placeholder={placeholder ?? `Ask ${name} about this…`} value={q} onChange={(e) => setQ(e.target.value)}
             aria-label={`Ask ${name} about ${about}`} />
      <button type="submit" className="px-icon-btn" aria-label="Ask"><PaperPlaneRight size={15} /></button>
    </form>
  );
}

function WorkDrawer({ ctx, w, onClose }: { ctx: PxCtx; w: WorkItem; onClose: () => void }) {
  const l = w.links;
  const story = useWorkStory(ctx, w.id).data;
  const full = l.build || l.workflow_run;
  return (
    <Drawer title={w.label} icon={<Ico icon={w.state === "needs_you" ? GitPullRequest : w.state === "stalled" ? PauseCircle : FlowArrow} c={w.state === "needs_you" || w.state === "stalled" ? "amber" : "teal"} />} onClose={onClose} testid="px-drawer-work"
            sub={<><Tag>{KIND_WORDS[w.kind] ?? w.kind}</Tag>{w.state === "stalled" ? <Tag tone="amber">Stalled</Tag> : null}
              {story?.number ? <span className="px-mono" data-testid="px-drawer-work-number">Run #{story.number}</span> : null}
              {w.cost_usd !== null ? <span className="px-mono">${w.cost_usd.toFixed(2)}</span> : null}
              {story?.minutes !== null && story?.minutes !== undefined ? <span className="px-mono">{story.minutes} min</span> : w.age ? <span className="px-mono">{w.age}</span> : null}</>}
            actions={full ? <a className="px-icon-btn" href={pxHref(full)} title={l.build ? "Open full page in Code Tasks" : "Open the workflow run"} aria-label="Open full page" data-testid="px-drawer-work-expand"><ArrowsOut size={15} /></a> : null}
            footer={<AskBox ctx={ctx} about={w.label} placeholder={`Ask ${ctx.overview.captain?.name ?? "BOSS"} about this work…`} />}>
      {w.state === "needs_you" ? (
        <div className="px-ny" data-testid="px-drawer-work-needs">
          <span className="px-ny-ico" aria-hidden>!</span>
          <b style={{ flex: 1 }}>{l.pr ? "Ready to merge" : "Waiting on you"}</b>
          {/* ONE ACTION (screen 05b): merging is approving the build's merge question in BOSS;
              with no question on record it opens the PR on GitHub. */}
          {l.pr ? (l.decision
            ? <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(l.decision)} data-testid="px-drawer-work-merge"><GitMerge size={12} aria-hidden /> Merge PR #{l.pr.number}</a>
            : <a className="px-btn" data-kind="amber" data-size="s" href={l.pr.external_url} target="_blank" rel="noreferrer noopener" data-testid="px-drawer-work-merge"><GitMerge size={12} aria-hidden /> Merge PR #{l.pr.number} ↗</a>)
            : l.decision ? <PxLink href={l.decision} className="px-btn" testid="px-drawer-work-decide">Decide</PxLink>
            : l.workflow_run ? <PxLink href={l.workflow_run} className="px-btn">Decide</PxLink> : null}
        </div>
      ) : null}
      {w.state === "stalled" ? (
        <div className="px-ny" data-testid="px-drawer-work-stalled">
          <span className="px-ny-ico" aria-hidden>‖</span>
          <span style={{ flex: 1 }}><b>Stalled{w.stalled_since ? ` since ${whenLocal(w.stalled_since)}` : ""}</b>
            <small className="px-faint" style={{ display: "block" }}>Nothing is working on it. Open the build to retry or stop it.</small></span>
          {l.build ? <PxLink href={l.build} className="px-btn" testid="px-drawer-work-stalled-open">Open build</PxLink> : null}
        </div>
      ) : null}
      <WorkStoryBody ctx={ctx} w={w} story={story} />
      <DrawerAttempts w={w} ctx={ctx} />
      {w.detail && !story ? <p style={{ margin: 0, lineHeight: 1.5 }} className="px-muted">{w.detail}</p> : null}
      <div>
        <span className="px-lbl">Links</span>
        <div className="px-row px-wrap" style={{ marginTop: 6 }} data-testid="px-drawer-work-links">
          {l.workflow_run ? <PxLink href={l.workflow_run} testid="px-drawer-link-run"><FlowArrow size={11} aria-hidden /> Workflow run</PxLink> : null}
          {l.build ? <PxLink href={l.build} testid="px-drawer-link-build"><Code size={11} aria-hidden /> Build in Code Tasks</PxLink> : null}
          {l.pr ? <PxLink href={l.pr.external_url} testid="px-drawer-link-pr"><GitPullRequest size={11} aria-hidden /> PR #{l.pr.number}</PxLink> : null}
          {l.deploy ? <PxLink href={l.deploy}><RocketLaunch size={11} aria-hidden /> Deploy</PxLink> : null}
          {l.build ? <PxLink href={`${l.build}&view=logs`}><Terminal size={11} aria-hidden /> Logs</PxLink> : null}
          <PxLink href={`?area=knowledge&project_id=${encodeURIComponent(ctx.projectId)}`}><Graph size={11} aria-hidden /> What it learned</PxLink>
          {w.href && !w.href.includes("drawer=") ? <PxLink href={w.href}><FileText size={11} aria-hidden /> Open</PxLink> : null}
        </div>
      </div>
    </Drawer>
  );
}

function DeployDrawer({ ctx, drawer, onClose }: { ctx: PxCtx; drawer: { kind: string; id: string }; onClose: () => void }) {
  const read = usePxRead<Deployments>(PX_PATHS.deployments(ctx.projectId), parseDeployments, { everyMs: 15_000, demo: () => parseDeployments(FX_DEPLOYMENTS) });
  const d = read.data;
  if (!d) return <Missing what="This deploy" onClose={onClose} loading={read.state === "loading"} />;
  // `env:<target id>` (the engine's hrefs) or `env:<key>` (older links): the same card.
  const env = d.environments.find((e) => e.target_id === drawer.id) ?? d.environments.find((e) => e.key === drawer.id);
  const hist = d.history.find((h) => h.id === drawer.id);
  if (env) {
    return (
      <Drawer title={env.label} icon={<Ico icon={RocketLaunch} c="green" />} onClose={onClose} testid="px-drawer-env" sub={<Tag tone="green">{HEALTH_WORD[env.health]}</Tag>}>
        {env.url ? <a className="px-env-url" href={env.url} target="_blank" rel="noreferrer noopener"><Globe size={12} aria-hidden />{env.url} ↗</a> : null}
        <dl className="px-kv">
          {env.uptime_pct !== null ? <><dt>Uptime</dt><dd>{env.uptime_pct}%</dd></> : null}
          {env.p95_ms !== null ? <><dt>p95</dt><dd>{env.p95_ms}ms</dd></> : null}
          <dt>Deploys</dt><dd>{env.policy === "auto" ? "automatically after merge" : "when a person approves"}</dd>
          {env.host ? <><dt>Server</dt><dd><PxLink href={env.host_href || "?area=infra&view=fleet"} testid="px-drawer-env-host" title={env.host}>runs on {env.host_name || env.host}</PxLink></dd></> : null}
          {env.last_deploy ? <><dt>Last deploy</dt><dd>{env.last_deploy.label} · {whenLocal(env.last_deploy.at)} · {env.last_deploy.by}</dd></> : null}
        </dl>
        {env.series.length > 1 ? <div><span className="px-lbl">24h</span><Spark series={env.series} tone="green" label="Health over the last 24 hours" /></div> : null}
        {d.history.filter((h) => h.label.toLowerCase().startsWith(env.label.toLowerCase())).slice(0, 4).map((h) => (
          <button key={h.id} type="button" className="px-li px-li-btn" onClick={() => ctx.open("deploy", h.id)} data-testid={`px-drawer-env-h-${h.id}`}>
            <span className="px-dot" data-s={h.state === "done" ? "done" : h.state === "failed" ? "bad" : "you"} aria-hidden />
            <span><b>{h.label}</b><small>{[h.word, h.by, h.at ? whenLocal(h.at) : ""].filter(Boolean).join(" · ")}</small></span><span /></button>))}
        {env.demo_logins.map((l) => <DemoLoginRow key={l.user} projectId={ctx.projectId} login={{ label: l.label, value: l.user, sub: env.label, reveal: l.reveal, href: "" }} />)}
        <div className="px-row px-wrap">
          <button type="button" className="px-chip" onClick={() => ctx.tab("health")}>Health</button>
          {env.manage_href ? <PxLink href={env.manage_href} testid="px-drawer-env-manage">Manage in Infra</PxLink> : null}
        </div>
      </Drawer>
    );
  }
  if (hist) {
    return (
      <Drawer title={hist.label} icon={<Ico icon={RocketLaunch} c={hist.state === "done" ? "green" : "amber"} />} onClose={onClose} testid="px-drawer-deploy">
        <p style={{ margin: 0 }}>{[hist.sub, hist.at ? whenLocal(hist.at) : ""].filter(Boolean).join(" · ")}</p>
        <PxLink href={hist.href || `?area=infra&view=deploys&project_id=${encodeURIComponent(ctx.projectId)}`} testid="px-drawer-deploy-infra">The full deploy log in Infra</PxLink>
        <AskBox ctx={ctx} about={hist.label} />
      </Drawer>
    );
  }
  return <Missing what="This deploy" onClose={onClose} />;
}

export { pxHref };
