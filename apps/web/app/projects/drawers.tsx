"use client";

/**
 * SIDE DRAWERS (screen 5b, contract §10): `&drawer=<kind>:<id>`, deep-linkable, Back closes.
 *   work:<id>     how the work went, step by step, its one action and every link (5b)
 *   needs:<id>    what waits on you and why, with its action
 *   stage:<key>   a journey stage (and stage:how — how the journey reads)
 *   env:<key>     an environment's full picture;  deploy:<id>  one deploy
 * Prose lives here, never on the page.
 */
import React, { useState } from "react";
import {
  ChatCircleDots, Check, Code, FileText, FlowArrow, GitPullRequest, Globe, Graph, PaperPlaneRight, Path, RocketLaunch, Terminal, User, Warning,
} from "@phosphor-icons/react";

import { PX_PATHS, parseDeployments, pxHref, usePxRead, type Deployments, type WorkItem } from "../../lib/project-experience";
import { FX_DEPLOYMENTS } from "../../lib/project-experience-fixtures";
import { Drawer, Empty, Face, Flow, HEALTH_WORD, Ico, PxLink, STATE_WORD, Tag } from "./kit";
import { ActionButton } from "./tab-overview";
import { DemoLoginRow } from "./tab-settings";
import type { PxCtx } from "./project-page";

const STEP_WORDS: Record<string, string> = { plan: "Plan", design: "You approved", decide: "You approved", build: "Build", review: "Review & test", pr: "Merge", ship: "Staging" };

export function DrawerHost({ ctx, drawer, onClose }: { ctx: PxCtx; drawer: { kind: string; id: string }; onClose: () => void }) {
  if (drawer.kind === "work") {
    const w = ctx.work?.items.find((x) => x.id === drawer.id) ?? ctx.overview.recent_work.find((x) => x.id === drawer.id);
    return w ? <WorkDrawer ctx={ctx} w={w} onClose={onClose} /> : <Missing what="This work item" onClose={onClose} loading={!ctx.work} />;
  }
  if (drawer.kind === "needs") {
    const n = ctx.overview.needs_you.find((x) => x.id === drawer.id);
    if (!n) return <Missing what="This item" onClose={onClose} />;
    return (
      <Drawer title={n.label} icon={<span className="px-ny-ico" aria-hidden>!</span>} onClose={onClose} testid="px-drawer-needs" sub={<Tag tone="amber">needs you</Tag>}
              footer={n.action ? <ActionButton ctx={ctx} label={n.action.label} href={n.action.href} amber testid="px-drawer-needs-act" /> : null}>
        {n.mini.length ? <Flow nodes={n.mini} /> : null}
        {n.detail ? <p style={{ margin: 0, lineHeight: 1.5 }}>{n.detail}</p> : null}
        {n.action?.decision_id ? <PxLink href={`?area=decisions&decision_id=${encodeURIComponent(n.action.decision_id)}`}>Open in Approvals</PxLink> : null}
        <AskBox ctx={ctx} about={n.label} />
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
      <Drawer title={n.label} sub={<Tag tone={n.state === "done" ? "green" : n.state === "waiting" ? "amber" : "blue"}>{STATE_WORD[n.state]}</Tag>} icon={<Ico icon={Path} c="teal" />} onClose={onClose} testid="px-drawer-stage">
        {n.sub ? <p style={{ margin: 0 }}><b>{n.sub}</b></p> : null}
        <div className="px-row px-wrap">
          {n.key === "onboarding" || n.key === "ready" ? <button type="button" className="px-chip" onClick={() => ctx.tab("knowledge")}>Dossier</button> : null}
          {n.key === "backlog" ? <button type="button" className="px-chip" onClick={() => ctx.tab("roadmap")}>Roadmap</button> : null}
          {n.key === "working" ? <button type="button" className="px-chip" onClick={() => ctx.tab("work")}>Work</button> : null}
          {n.key === "staging" || n.key === "production" ? <button type="button" className="px-chip" onClick={() => ctx.tab("deployments")}>Deployments</button> : null}
        </div>
        <AskBox ctx={ctx} about={n.label} />
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
function AskBox({ ctx, about }: { ctx: PxCtx; about: string }) {
  const [q, setQ] = useState("");
  const name = ctx.overview.captain?.name ?? "BOSS";
  return (
    <form className="px-row" style={{ marginTop: "auto", border: "1px solid var(--border-strong)", borderRadius: 10, padding: "6px 8px", background: "var(--surface-2)" }}
          onSubmit={(e) => { e.preventDefault(); ctx.ask(q.trim() ? `${q.trim()}\n\n(About: ${about})` : `Tell me about "${about}".`); }} data-testid="px-drawer-ask">
      {ctx.overview.captain ? <Face agent={ctx.overview.captain} size={20} /> : <ChatCircleDots size={16} aria-hidden />}
      <input className="px-input" style={{ border: 0, background: "transparent", padding: 4 }} placeholder={`Ask ${name} about this…`} value={q} onChange={(e) => setQ(e.target.value)}
             aria-label={`Ask ${name} about ${about}`} />
      <button type="submit" className="px-icon-btn" aria-label="Ask"><PaperPlaneRight size={15} /></button>
    </form>
  );
}

function WorkDrawer({ ctx, w, onClose }: { ctx: PxCtx; w: WorkItem; onClose: () => void }) {
  const l = w.links;
  return (
    <Drawer title={w.label} icon={<Ico icon={w.state === "needs_you" ? GitPullRequest : FlowArrow} c={w.state === "needs_you" ? "amber" : "teal"} />} onClose={onClose} testid="px-drawer-work"
            sub={<><Tag>{w.kind[0].toUpperCase() + w.kind.slice(1)}</Tag>{w.cost_usd !== null ? <span className="px-mono">${w.cost_usd.toFixed(2)}</span> : null}{w.age ? <span className="px-mono">{w.age}</span> : null}</>}
            footer={<AskBox ctx={ctx} about={w.label} />}>
      {w.state === "needs_you" ? (
        <div className="px-ny" data-testid="px-drawer-work-needs">
          <span className="px-ny-ico" aria-hidden>!</span>
          <b style={{ flex: 1 }}>{l.pr ? "Ready to merge" : "Waiting on you"}</b>
          {l.pr ? <a className="px-btn" data-kind="amber" data-size="s" href={l.pr.external_url} target="_blank" rel="noreferrer noopener" data-testid="px-drawer-work-merge">Merge PR #{l.pr.number} ↗</a>
            : l.workflow_run ? <PxLink href={l.workflow_run} className="px-btn">Decide</PxLink> : null}
        </div>
      ) : null}
      {w.stages.length ? (
        <div>
          <span className="px-lbl">How it went</span>
          <div className="px-steps" data-testid="px-drawer-work-steps">
            {w.stages.map((st, i) => {
              const person = st.key === "design" || st.key === "decide" || st.key === "pr";
              const who = person ? null : w.agents[Math.min(i, w.agents.length - 1)];
              return (
                <div key={`${st.key}-${i}`} className="px-step">
                  <span className="px-ck-b" aria-hidden style={st.state === "done" ? { background: "var(--green)", borderColor: "var(--green)", color: "#032015" }
                    : st.state === "now" ? { borderColor: "var(--amber)", background: "var(--amber)", color: "#1b1300", borderRadius: "50%" } : undefined}>
                    {st.state === "done" ? <Check size={11} weight="bold" /> : st.state === "now" ? "!" : ""}</span>
                  {who ? <Face agent={who} size={22} state={st.state === "now" ? "working" : "idle"} /> : <User size={18} aria-hidden />}
                  <span><b style={{ fontSize: ".66rem" }}>{STEP_WORDS[st.key] ?? st.label}</b><small className="px-faint" style={{ display: "block", fontSize: ".56rem" }}>{STATE_WORD[st.state]}{who ? ` · ${who.name}` : person ? " · you" : ""}</small></span>
                  <span />
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
      {w.detail ? <p style={{ margin: 0, lineHeight: 1.5 }} className="px-muted">{w.detail}</p> : null}
      {l.preview ? (
        <div className="px-card" style={{ padding: 10 }}>
          <span className="px-lbl">Preview</span>
          <div><a className="px-mono" href={l.preview} target="_blank" rel="noreferrer noopener"><Globe size={12} aria-hidden /> {l.preview.replace(/^https?:\/\//, "")} ↗</a> <Tag tone="green">Up</Tag></div>
        </div>
      ) : null}
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
  const env = d.environments.find((e) => e.key === drawer.id);
  const hist = d.history.find((h) => h.id === drawer.id);
  if (env) {
    return (
      <Drawer title={env.label} icon={<Ico icon={RocketLaunch} c="green" />} onClose={onClose} testid="px-drawer-env" sub={<Tag tone="green">{HEALTH_WORD[env.health]}</Tag>}>
        {env.url ? <a className="px-env-url" href={env.url} target="_blank" rel="noreferrer noopener"><Globe size={12} aria-hidden />{env.url} ↗</a> : null}
        <dl className="px-kv">
          {env.uptime_pct !== null ? <><dt>Uptime</dt><dd>{env.uptime_pct}%</dd></> : null}
          {env.p95_ms !== null ? <><dt>p95</dt><dd>{env.p95_ms}ms</dd></> : null}
          <dt>Deploys</dt><dd>{env.policy === "auto" ? "automatically after merge" : "when a person approves"}</dd>
          {env.host ? <><dt>Server</dt><dd><PxLink href={env.host_href || "?area=infra&view=fleet"}>runs on {env.host}</PxLink></dd></> : null}
          {env.last_deploy ? <><dt>Last deploy</dt><dd>{env.last_deploy.label} · {env.last_deploy.at} · {env.last_deploy.by}</dd></> : null}
        </dl>
        {env.demo_logins.map((l) => <DemoLoginRow key={l.user} projectId={ctx.projectId} login={{ label: l.label, value: l.user, sub: env.label, reveal: l.reveal, href: "" }} />)}
        <button type="button" className="px-chip" onClick={() => ctx.tab("health")}>Health</button>
      </Drawer>
    );
  }
  if (hist) {
    return (
      <Drawer title={hist.label} icon={<Ico icon={RocketLaunch} c={hist.state === "done" ? "green" : "amber"} />} onClose={onClose} testid="px-drawer-deploy">
        <p style={{ margin: 0 }}>{hist.sub}</p>
        <PxLink href={`?area=infra&view=deploys&project_id=${encodeURIComponent(ctx.projectId)}`}>The full deploy log in Infra</PxLink>
        <AskBox ctx={ctx} about={hist.label} />
      </Drawer>
    );
  }
  return <Missing what="This deploy" onClose={onClose} />;
}

export { pxHref };
