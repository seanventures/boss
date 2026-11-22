"use client";

/**
 * SIDE DRAWERS (screen 5b, contract §10): `&drawer=<kind>:<id>`, deep-linkable, Back closes.
 *   work:<id>     (also run:, build:) the Work panel: one task's stages, what next and its links
 *                 (`&wp=full` lays it over the whole screen)
 *   needs:<id>    what waits on you and why, with its action
 *   stage:<key>   a journey stage (and stage:how — how the journey reads)
 *   env:<key>     an environment's full picture;  deploy:<id>  one deploy
 * Prose lives here, never on the page.
 */
import { FailedActions } from "./build-failed";
import React, { useEffect, useMemo, useState } from "react";
import {
  ChatCircleDots, GitMerge, Globe, Graph, ListChecks, PaperPlaneRight, Path, Question,
  RocketLaunch, Warning, Wrench,
} from "@phosphor-icons/react";

import { PX_PATHS, findWork, nodeWords, parseDeployments, pxHref, toTab, usePxRead, writePxUrl, type Deployments, type WorkItem } from "../../lib/project-experience";
import { FX_DEPLOYMENTS } from "../../lib/project-experience-fixtures";
import { groupTasks, taskOf } from "../../lib/work-tasks";
import { FullToggle, WorkPanel } from "./work-panel";
import { Drawer, Empty, Face, Flow, HEALTH_WORD, Ico, PxLink, STATE_WORD, Spark, Tag } from "./kit";
import { ActionButton } from "./tab-overview";
import { CaptainDrawer } from "./captain-drawer";
import { DecisionActs, NeedsBody, StageBody, stageIcon } from "./drawers-more";

/** Each Needs-you item's icon, by what it asks (as on Overview). */
const NEED_ICON: Record<string, typeof GitMerge> = { merge: GitMerge, deploy: RocketLaunch, plan: ListChecks, setup: Wrench, question: Question };
import { DemoLoginRow } from "./tab-settings";
import { KnowledgeDrawer } from "./tab-knowledge";
import { RecDrawer } from "./tab-roadmap";
import { LOCAL, whenLocal } from "../../lib/local-time";
import type { PxCtx } from "./project-page";
import { LearnedStrip } from "../knowledge/learned-strip";

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
  if (drawer.kind === "learned") {
    const w = findWork(ctx.work?.items ?? [], drawer.id) ?? ctx.overview.recent_work.find((x) => x.id === drawer.id);
    return (
      <Drawer title="What it learned" icon={<Ico icon={Graph} c="violet" />} onClose={onClose} testid="px-drawer-learned"
              sub={w ? <span className="px-faint">{w.label}</span> : undefined}
              footer={<PxLink href={`?area=knowledge&project_id=${encodeURIComponent(ctx.projectId)}&kg_tab=graph`} testid="px-drawer-learned-graph"><Graph size={11} aria-hidden /> Whole knowledge graph</PxLink>}>
        <p className="px-faint" style={{ margin: 0, fontSize: ".6rem" }}>What this run&apos;s agents learned, and the rules and lessons they consulted. Each opens the graph on it.</p>
        <LearnedStrip always projectId={ctx.projectId} actor={ctx.actor} orgId={ctx.orgId} runId={drawer.id}
                      live={w ? w.state === "running" || w.state === "needs_you" : false} />
      </Drawer>
    );
  }
  // `run:<id>` and `build:<id>` (the engine's WorkItem hrefs and ⌘K) are the work drawer.
  if (drawer.kind === "work" || drawer.kind === "run" || drawer.kind === "build") {
    // An attempt of a grouped piece of work (a follow-up, a retry) opens as itself.
    const w = findWork(ctx.work?.items ?? [], drawer.id) ?? findWork(ctx.overview.recent_work, drawer.id);
    return w ? <WorkDrawer ctx={ctx} w={w} onClose={onClose} /> : <Missing what="This work item" onClose={onClose} loading={!ctx.work} />;
  }
  if (drawer.kind === "needs") {
    const n = ctx.overview.needs_you.find((x) => x.id === drawer.id);
    if (!n) return <Missing what="This item" onClose={onClose} />;
    // A FAILED BUILD's card has its own answers (Retry is a POST, only when it helps), never a
    // generic action that would navigate to an API URL (review #218 L4).
    if (n.kind === "failed") {
      return (
        <Drawer title={n.label} icon={<Ico icon={Warning} c="red" />} onClose={onClose} testid="px-drawer-needs" sub={<Tag tone="red">failed</Tag>}
                footer={<><FailedActions retry={n.retry} fix={n.fix} retryReason={n.retry_reason} seeHref={n.see_href} projectId={ctx.projectId}
                                         actor={ctx.actor} testid="px-drawer-failed" onDone={() => { ctx.refreshWork(); ctx.refreshOverview?.(); }} />
                          <AskBox ctx={ctx} about={n.label} /></>}>
          <p style={{ margin: 0 }} data-testid="px-drawer-failed-why">{n.detail}</p>
          {n.fix?.detail ? <p className="px-faint" style={{ margin: 0 }}>{n.fix.detail}</p> : null}
        </Drawer>
      );
    }
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
    const found = ctx.overview.journey.find((x) => x.key === drawer.id);
    if (!found) return <Missing what="This stage" onClose={onClose} />;
    // The stage's day in the viewer's zone (04-02), as on the strip.
    const n = found.at ? { ...found, ...nodeWords(found, LOCAL.day) } : found;
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
  if (drawer.kind === "rec") return <RecDrawer ctx={ctx} id={drawer.id} onClose={onClose} />;
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

/** Is the panel laid over the whole screen (`&wp=full`, the rc10.32 panel's own links)? */
function useWholePage(): [boolean, (on: boolean) => void] {
  const read = () => { try { return new URLSearchParams(window.location.search).get("wp") === "full"; } catch { return false; } };
  const [full, setFull] = useState(read);
  useEffect(() => {
    const on = () => setFull(read());
    window.addEventListener("popstate", on);
    return () => window.removeEventListener("popstate", on);
  }, []);
  return [full, (v: boolean) => { writePxUrl({ wp: v ? "full" : null }, "replace"); setFull(v); }];
}

/** THE WORK PANEL (v2, work-panel.tsx) in the drawer: a Work row's click, `drawer=work:<id>`,
 *  `run:` and `build:` links, and ⤢ (`wp=full`) for the whole screen. */
function WorkDrawer({ ctx, w, onClose }: { ctx: PxCtx; w: WorkItem; onClose: () => void }) {
  const [full, setFull] = useWholePage();
  const task = useMemo(() => taskOf(groupTasks((ctx.work?.items ?? []).filter((x) => x.origin !== "ticket_import")), w.id), [ctx.work, w.id]);
  const close = () => { if (full) writePxUrl({ wp: null }, "replace"); onClose(); };
  return (
    <Drawer title={full ? <span className="px-faint" style={{ fontWeight: 500 }}>{ctx.overview.project.name} / Work / <span style={{ color: "var(--ink)" }}>{w.label}</span></span> : "Work"}
            onClose={close} testid="px-drawer-work" wide full={full} bodyClass="wp2-body"
            actions={<FullToggle full={full} onToggle={() => setFull(!full)} testid="px-drawer-work-full" />}>
      <WorkPanel ctx={ctx} w={w} task={task} />
    </Drawer>
  );
}

/** App and probe only. Any other source, including none, stays off the drawer. */
export function drawerP95SourceSentence(source: string | null | undefined): string | null {
  if (source === "app") return "From the app's requests.";
  if (source === "probe") return "From health probes.";
  return null;
}

function DeployDrawer({ ctx, drawer, onClose }: { ctx: PxCtx; drawer: { kind: string; id: string }; onClose: () => void }) {
  const read = usePxRead<Deployments>(PX_PATHS.deployments(ctx.projectId), parseDeployments, { everyMs: 15_000, demo: () => parseDeployments(FX_DEPLOYMENTS) });
  const d = read.data;
  if (!d) return <Missing what="This deploy" onClose={onClose} loading={read.state === "loading"} />;
  // `env:<target id>` (the engine's hrefs) or `env:<key>` (older links): the same card.
  const env = d.environments.find((e) => e.target_id === drawer.id) ?? d.environments.find((e) => e.key === drawer.id);
  const hist = d.history.find((h) => h.id === drawer.id);
  if (env) {
    const p95Source = drawerP95SourceSentence(env.p95_source);
    return (
      <Drawer title={env.label} icon={<Ico icon={RocketLaunch} c="green" />} onClose={onClose} testid="px-drawer-env" sub={<Tag tone="green">{HEALTH_WORD[env.health]}</Tag>}>
        {env.url ? <a className="px-env-url" href={env.url} target="_blank" rel="noreferrer noopener"><Globe size={12} aria-hidden />{env.url} ↗</a> : null}
        <dl className="px-kv">
          {env.uptime_pct !== null ? <><dt>Uptime</dt><dd>{env.uptime_pct}%</dd></> : null}
          {env.p95_ms !== null ? <><dt>p95</dt><dd>{env.p95_ms}ms{p95Source ? <span data-testid="px-drawer-env-p95-source">{p95Source}</span> : null}</dd></> : null}
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
