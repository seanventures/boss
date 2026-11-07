"use client";

/**
 * The drawers' bodies that read more than the page already holds:
 *   WorkStoryBody   screen 05b "How it went": each station in words with its proof link
 *                   (GET …/work/{id}/story), the Preview and Cost mini cards;
 *   StageBody       a journey node's drawer (screen 04 note 1): what happened at that stage,
 *                   from the reads the page has (onboarding, work, roadmap, deployments);
 *   NeedsBody       a Needs-you item: why it waits, its path, the work and the PR.
 */
import React from "react";
import {
  ArrowSquareOut, Check, Flask, Globe, GitMerge, Kanban, ListChecks, MapTrifold, RocketLaunch, Warning,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError, idPost, refusalSentence } from "../../lib/identity/api";
import { isPxDemo } from "../../lib/project-experience";
import { notifyChanged } from "../changed";
import {
  PX_PATHS, parseDeployments, parseWorkStory, startOnboarding, usePxRead, type Agent, type Deployments, type NeedsYou, type Node,
  type WorkItem, type WorkStory,
} from "../../lib/project-experience";
import { FX_DEPLOYMENTS } from "../../lib/project-experience-fixtures";
import { whenLocal } from "../../lib/local-time";
import { Empty, Face, Flow, HEALTH_TONE, HEALTH_WORD, PxLink, Ring, Said, Spark, STAGE_ICON, STATE_WORD, Stations, Tag, Cost } from "./kit";
import type { PxCtx } from "./project-page";

// ── 05b ─────────────────────────────────────────────────────────────────────────────────────

/** Who does each station (screen 05b's faces): the role, then its callsign from the crew. */
const STATION_ROLES: Record<string, string[]> = {
  plan: ["product_manager", "pm", "product_owner"], build: ["engineering_captain", "software_engineer", "engineer"],
  review: ["qa_engineer", "qa", "adversarial_reviewer", "reviewer"], ship: ["sre", "site_reliability_engineer", "devops_engineer"],
};
const STATION_DEFAULT: Record<string, Agent> = {
  plan: { name: "Juno", role_key: "product_manager", avatar: "" } as Agent, build: { name: "Forge", role_key: "software_engineer", avatar: "" } as Agent,
  review: { name: "Vera", role_key: "qa_engineer", avatar: "" } as Agent, ship: { name: "Beacon", role_key: "sre", avatar: "" } as Agent,
};
export function stationAgent(key: string, crew: Agent[]): Agent | null {
  const roles = STATION_ROLES[key];
  if (!roles) return null;
  return crew.find((a) => roles.includes(a.role_key)) ?? STATION_DEFAULT[key] ?? null;
}

const STEP_STYLE: Record<string, React.CSSProperties> = {
  done: { background: "var(--green)", borderColor: "var(--green)", color: "#032015" },
  waiting: { borderColor: "var(--amber)", background: "var(--amber)", color: "#1b1300", borderRadius: "50%" },
  now: { borderColor: "var(--teal)", color: "var(--teal)" },
  failed: { borderColor: "var(--red)", color: "var(--red)" },
};

export function useWorkStory(ctx: PxCtx, id: string) {
  return usePxRead<WorkStory>(id && !ctx.demo ? PX_PATHS.workStory(ctx.projectId, id) : null, parseWorkStory, { everyMs: 20_000 });
}

/** "How it went", the Preview and Cost mini cards. Falls back to the row's stations while the
 *  story loads (or on an engine without it). */
export function WorkStoryBody({ ctx, w, story }: { ctx: PxCtx; w: WorkItem; story: WorkStory | null }) {
  const crew: Agent[] = [...w.agents, ...(ctx.overview.captain ? [ctx.overview.captain as unknown as Agent, ...ctx.overview.captain.squad] : [])];
  const rows = story?.stages.length ? story.stages : w.stages.filter((st) => st.state !== "skipped").map((st) => ({
    key: st.key, name: st.label, state: st.state, sub: "", at: "", person: st.key === "design" || st.key === "pr", link: null,
  }));
  const parts = story?.cost_parts ?? [];
  const total = parts.reduce((x, p) => x + p.usd, 0) || 1;
  const PART_TONE: Record<string, string> = { Plan: "var(--teal)", Build: "var(--accent)", Review: "var(--green)" };
  const preview = story?.preview || w.links.preview;
  return (
    <>
      {rows.length ? (
        <div>
          <span className="px-lbl">How it went</span>
          <div className="px-steps" data-testid="px-drawer-work-steps">
            {rows.map((st, i) => {
              const who = st.person ? null : stationAgent(st.key, crew);
              const mark = st.state === "done" ? <Check size={11} weight="bold" /> : st.state === "waiting" ? "!" : st.state === "failed" ? "✕" : "";
              return (
                <div key={`${st.key}-${i}`} className="px-step" data-testid={`px-drawer-step-${st.key}`}>
                  <span className="px-ck-b" aria-label={STATE_WORD[st.state]} style={STEP_STYLE[st.state]}>{mark}</span>
                  {who ? <Face agent={who} size={22} state={st.state === "now" ? "working" : "idle"} /> : <Face agent={{ name: "You" }} size={22} />}
                  <span style={{ minWidth: 0 }}>
                    <b style={{ fontSize: ".66rem" }}>{st.name}</b>
                    <small className="px-faint" style={{ display: "block", fontSize: ".56rem" }}>
                      {[st.sub, st.at ? whenLocal(st.at) : "", !st.sub && !st.at ? STATE_WORD[st.state] : ""].filter(Boolean).join(" · ")}</small>
                  </span>
                  {st.link ? (
                    <PxLink href={st.link.external_url || st.link.href} testid={`px-drawer-step-link-${st.key}`} className="px-chip">{st.link.label}</PxLink>
                  ) : <span />}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
      {preview || parts.length ? (
        <div className="px-grid px-g2 px-wk-minis" data-testid="px-drawer-work-minis">
          <div className="px-card px-mini">
            <span className="px-lbl">Preview</span>
            {preview ? (
              <a className="px-mono px-row" href={preview} target="_blank" rel="noreferrer noopener" data-testid="px-drawer-work-preview" style={{ gap: 6 }}>
                <Globe size={13} aria-hidden />{preview.replace(/^https?:\/\/[^:/]+/, "") || preview.replace(/^https?:\/\//, "")}<Tag tone="green">Up</Tag></a>
            ) : <span className="px-faint" style={{ fontSize: ".58rem" }}>No preview</span>}
          </div>
          <div className="px-card px-mini">
            <span className="px-lbl">Cost</span>
            {parts.length ? (
              <>
                <span className="px-bar" role="img" aria-label={parts.map((p) => `${p.label} $${p.usd.toFixed(2)}`).join(", ")} data-testid="px-drawer-work-cost">
                  {parts.map((p) => <span key={p.label} style={{ width: `${(100 * p.usd) / total}%`, background: PART_TONE[p.label] ?? "var(--accent)" }} />)}
                </span>
                <small className="px-faint" style={{ fontSize: ".54rem" }}>{parts.map((p) => p.label).join(" · ")}</small>
              </>
            ) : <Cost usd={w.cost_usd} list={w.list_usd} empty="No spend yet" />}
          </div>
        </div>
      ) : null}
    </>
  );
}

// ── journey nodes ───────────────────────────────────────────────────────────────────────────

const STAGE_WORDS: Record<string, string> = {
  onboarding: "Helm read the repository: stack, commands, tests and what only you can do.",
  ready: "Ready means every check the crew can do itself has passed.",
  backlog: "The backlog Marshal ranked: issues, failing checks and ideas, best first.",
  working: "Every piece of work is planned, built, reviewed and opened as a PR.",
  staging: "Staging deploys by the project's policy and is health-checked after each deploy.",
  production: "Production always waits for a person.",
};

export function StageBody({ ctx, n }: { ctx: PxCtx; n: Node }) {
  const deps = usePxRead<Deployments>(n.key === "staging" || n.key === "production" ? PX_PATHS.deployments(ctx.projectId) : null, parseDeployments,
                                      { everyMs: 30_000, demo: () => parseDeployments(FX_DEPLOYMENTS) });
  const [said, setSaid] = React.useState("");
  const ob = ctx.onboarding;
  const body = (() => {
    if (n.key === "onboarding") {
      return (
        <>
          {ob?.stages.length ? <Flow nodes={ob.stages.map((s) => ({ label: s.label, state: s.state }))} testid="px-stage-flow" /> : null}
          {ob?.discover.nodes.length ? (
            <div className="px-steps">{ob.discover.nodes.map((d) => (
              <div key={d.label} className="px-li"><span className="px-ck-b" style={STEP_STYLE[d.state]}>{d.state === "done" ? <Check size={11} weight="bold" /> : ""}</span>
                <span><b>{d.label}</b><small>{d.sub}</small></span><span /></div>))}</div>
          ) : <Empty icon={ListChecks} tone="teal" title="Not onboarded yet" line="Helm reads the repository and asks only for what it can't do itself." />}
          <div className="px-row px-wrap">
            <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid="px-stage-rerun"
                         onClick={() => void startOnboarding(ctx.projectId).then((r) => setSaid(r.ok ? "Helm is reading the repository again." : r.error ?? "Not started."))}>
              {ob && ob.state !== "not_started" ? "Read it again" : "Start onboarding"}</GatedButton>
            <button type="button" className="px-chip" onClick={() => ctx.open("onboarding", "")}>Full onboarding</button>
          </div>
          <Said text={said} />
        </>
      );
    }
    if (n.key === "ready") {
      const list = ob?.checklist ?? [];
      const done = list.filter((c) => c.state === "done" || c.state === "skipped").length;
      return list.length ? (
        <>
          <div className="px-row"><Ring pct={(100 * done) / list.length} tone={done === list.length ? "green" : "teal"} size="l" label={`${done} of ${list.length} ready`}>{done}/{list.length}</Ring>
            <span className="px-faint" style={{ fontSize: ".6rem" }}>{done === list.length ? "Every check passed." : `${list.length - done} left`}</span></div>
          <div className="px-steps" data-testid="px-stage-checklist">{list.map((c) => (
            <div key={c.key} className="px-li"><span className="px-ck-b" style={STEP_STYLE[c.state === "skipped" ? "done" : c.state]}>{c.state === "done" || c.state === "skipped" ? <Check size={11} weight="bold" /> : c.owner === "you" ? "!" : ""}</span>
              <span><b>{c.label}</b><small>{[c.sub || STATE_WORD[c.state], c.owner === "you" ? "you" : "Helm"].join(" · ")}</small></span>
              {c.owner === "you" && c.state !== "done" && c.action ? <PxLink href={c.action.href} className="px-btn">{c.action.label}</PxLink> : <span />}</div>))}</div>
        </>
      ) : <Empty icon={ListChecks} tone="teal" title="No readiness checks yet" line="Onboarding runs them: repo access, tests, the baseline build, a deploy target." />;
    }
    if (n.key === "backlog") {
      const items = [...ctx.overview.next, ...(ob?.backlog ?? []).filter((b) => !ctx.overview.next.some((x) => x.id === b.id))].slice(0, 8);
      return items.length ? (
        <div className="px-steps" data-testid="px-stage-backlog">{items.map((it, i) => (
          <div key={it.id || i} className="px-li"><span className="px-rank">{i + 1}</span>
            <span style={{ minWidth: 0 }}><b>{it.label}</b><small>{it.state === "next" ? "Next" : "Proposed"}{it.source ? ` · ${it.source === "ci" ? "Failing CI" : it.source === "bug" ? "Bug" : it.source === "ticket" ? "Issue" : "Roadmap"}` : ""}</small></span>
            {it.external_url ? <PxLink href={it.external_url}>Open</PxLink> : <span />}</div>))}</div>
      ) : <Empty icon={MapTrifold} tone="violet" title="Nothing ranked yet" line="Marshal ranks issues, failing checks and ideas once the repo is read." />;
    }
    if (n.key === "working") {
      const items = (ctx.work?.items ?? []).filter((w) => w.state !== "done").slice(0, 6);
      const merged = (ctx.work?.items ?? []).filter((w) => w.state === "done").length;
      return (
        <>
          <div className="px-row px-wrap" style={{ gap: 6 }}>
            {(["needs_you", "running", "stalled", "planned"] as const).map((k) => {
              const c = (ctx.work?.items ?? []).filter((w) => w.state === k).length;
              return c ? <Tag key={k} tone={k === "needs_you" || k === "stalled" ? "amber" : k === "running" ? "blue" : undefined}>{c} {k === "needs_you" ? "need you" : k}</Tag> : null;
            })}
            {merged ? <Tag tone="green">{merged} done</Tag> : null}
          </div>
          {items.length ? <div data-testid="px-stage-work">{items.map((w) => (
            <button key={w.id} type="button" className="px-li px-li-btn" onClick={() => ctx.open("work", w.id)}>
              <span className="px-dot" data-s={w.state === "running" ? "now" : w.state === "needs_you" ? "you" : w.state === "stalled" ? "stalled" : undefined} aria-hidden />
              <span style={{ minWidth: 0 }}><b className="px-ell" style={{ display: "block" }}>{w.label}</b><small>{w.links.pr ? `PR #${w.links.pr.number}` : w.age}</small></span>
              <Stations stages={w.stages} /></button>))}</div>
            : <Empty icon={Kanban} title="Nothing in flight" line="New work starts from New work, the Roadmap or What's next." />}
        </>
      );
    }
    const d = deps.data;
    const prod = n.key === "production";
    const envs = (d?.environments ?? []).filter((e) => (["production", "prod"].includes(e.key.toLowerCase())) === prod);
    if (!d) return <p className="px-faint">Reading the deployments…</p>;
    return envs.length ? (
      <>{envs.map((e) => (
        <div key={e.target_id || e.key} className="px-card px-mini" data-testid={`px-stage-env-${e.key}`}>
          <div className="px-row"><b>{e.label}</b><Tag tone={HEALTH_TONE[e.health]}>{HEALTH_WORD[e.health]}</Tag><span className="px-spacer" />
            <button type="button" className="px-chip" onClick={() => ctx.open("env", e.target_id || e.key)}>Details</button></div>
          {e.url ? <a className="px-env-url" href={e.url} target="_blank" rel="noreferrer noopener"><Globe size={12} aria-hidden />{e.url}<ArrowSquareOut size={10} aria-label="opens in a new tab" /></a> : null}
          <div className="px-row" style={{ gap: 12 }}>
            {e.uptime_pct !== null ? <Ring pct={e.uptime_pct} tone={e.health === "healthy" ? "green" : "amber"} label={`Uptime ${e.uptime_pct}%`}>{e.uptime_pct}</Ring> : null}
            <span className="px-mono px-faint" style={{ fontSize: ".58rem" }}>{e.p95_ms !== null ? `p95 ${e.p95_ms}ms` : ""}</span>
            {e.series.length > 1 ? <Spark series={e.series} tone="green" label="Health, last 24 hours" /> : null}
          </div>
          {e.last_deploy ? <small className="px-faint">Last deploy {e.last_deploy.label} · {whenLocal(e.last_deploy.at)}{e.last_deploy.by ? ` · ${e.last_deploy.by}` : ""}</small> : null}
        </div>))}
        {d.history.filter((h) => envs.some((e) => h.label.toLowerCase().startsWith(e.label.toLowerCase()))).slice(0, 4).map((h) => (
          <button key={h.id} type="button" className="px-li px-li-btn" onClick={() => ctx.open("deploy", h.id)}>
            <span className="px-ck-b" style={STEP_STYLE[h.state]}>{h.state === "done" ? <Check size={11} weight="bold" /> : ""}</span>
            <span><b>{h.label}</b><small>{[h.word, h.by, h.at ? whenLocal(h.at) : ""].filter(Boolean).join(" · ")}</small></span><span /></button>))}
      </>
    ) : prod ? (
      <Empty icon={RocketLaunch} tone="amber" title={d.production.state === "needs_host" ? "Pick a host for production" : "Set up production"}
             line="Beacon sets it up; you approve each prod deploy.">
        {d.production.action ? <PxLink href={d.production.action.href} className="px-btn" testid="px-stage-prod-act">{d.production.action.label}</PxLink> : null}
      </Empty>
    ) : <Empty icon={Flask} tone="green" title="Not deployed yet" line="The first merged work deploys to staging by the project's policy." />;
  })();
  return (
    <>
      <p className="px-faint" style={{ margin: 0, fontSize: ".6rem" }}>{STAGE_WORDS[n.key] ?? ""}</p>
      {body}
    </>
  );
}

export const stageIcon = (key: string) => STAGE_ICON[key] ?? ListChecks;

// ── needs you ───────────────────────────────────────────────────────────────────────────────

export function NeedsBody({ ctx, n }: { ctx: PxCtx; n: NeedsYou }) {
  const w = n.work_id ? (ctx.work?.items ?? []).find((x) => x.id === n.work_id) ?? ctx.overview.recent_work.find((x) => x.id === n.work_id) : undefined;
  return (
    <>
      {n.mini.length ? <Flow nodes={n.mini} testid="px-drawer-needs-flow" /> : null}
      {n.detail ? <p style={{ margin: 0, lineHeight: 1.5 }}>{n.detail}</p> : null}
      <dl className="px-kv">
        {n.created_at ? <><dt>Asked</dt><dd>{whenLocal(n.created_at)}</dd></> : null}
        {n.kind === "merge" ? <><dt>Who</dt><dd>Only a person merges</dd></> : n.kind === "deploy" ? <><dt>Who</dt><dd>A person approves this deploy</dd></> : null}
      </dl>
      <div className="px-row px-wrap" data-testid="px-drawer-needs-links">
        {w ? <button type="button" className="px-chip" onClick={() => ctx.open("work", w.id)} data-testid="px-drawer-needs-work"><Kanban size={11} aria-hidden /> {w.label}</button> : null}
        {n.external_url ? <PxLink href={n.external_url} testid="px-drawer-needs-pr"><GitMerge size={11} aria-hidden /> {/\/pull\/(\d+)/.exec(n.external_url) ? `PR #${/\/pull\/(\d+)/.exec(n.external_url)![1]}` : "On GitHub"}</PxLink> : null}
        {n.action?.decision_id ? <PxLink href={`?area=decisions&decision=${encodeURIComponent(n.action.decision_id)}`} testid="px-drawer-needs-approvals"><Warning size={11} aria-hidden /> Open in Approvals</PxLink> : null}
      </div>
    </>
  );
}


// ── acting on a Needs-you item in place (PX-05) ─────────────────────────────────────────────

/** The drawer's primary, named for what it does: "Merge PR #3", "Approve deploy". */
export function actWord(n: NeedsYou): string {
  if (n.kind === "merge") return /^Merge PR #\d+$/.test(n.label) ? n.label : "Merge";
  if (n.kind === "deploy") return "Approve deploy";
  if (n.kind === "plan") return "Approve plan";
  return "Approve";
}

/** Approve or reject the item's decision here, through the existing decision routes
 *  (POST /v1/decisions/{id}/approve | reject) — step-up asked when the answer needs it. */
export function DecisionActs({ ctx, n, onDone }: { ctx: PxCtx; n: NeedsYou; onDone?: () => void }) {
  const guarded = useGuardedCall();
  const [busy, setBusy] = React.useState(false);
  const [rejecting, setRejecting] = React.useState(false);
  const [reason, setReason] = React.useState("");
  const [said, setSaid] = React.useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const id = n.action?.decision_id;
  const review = n.external_url || (/^https?:\/\//.test(n.action?.href ?? "") ? n.action!.href : "");
  if (!id) return null;
  const answer = async (verb: "approve" | "reject") => {
    setBusy(true); setSaid({ text: "" });
    try {
      if (isPxDemo()) { setSaid({ text: "Sample mode — nothing was sent." }); return; }
      await guarded(verb === "approve" ? `${actWord(n)}.` : "Reject this.", () =>
        idPost(`/v1/decisions/${encodeURIComponent(id)}/${verb}`, verb === "reject" ? { note: reason.trim() || "Rejected from the project page." } : { note: "" }));
      setSaid({ text: verb === "approve" ? (n.kind === "merge" ? "Approved — BOSS merges it now." : n.kind === "deploy" ? "Approved — the deploy starts now." : "Approved.") : "Rejected.", tone: "green" });
      notifyChanged("decision", { id });
      ctx.refreshWork();
      onDone?.();
    } catch (e) {
      setSaid({ text: e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not sent: you cancelled the check." : refusalSentence(e), tone: "red" });
    } finally { setBusy(false); }
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: "1 1 100%" }} data-testid="px-drawer-needs-acts">
      {rejecting ? (
        <form className="px-row" onSubmit={(e) => { e.preventDefault(); void answer("reject"); }}>
          <input className="px-input" autoFocus placeholder="Why not? (one line)" value={reason} onChange={(e) => setReason(e.target.value)}
                 aria-label="Why reject" data-testid="px-drawer-needs-reason" />
          <GatedButton permission="decision:approve" projectId={ctx.projectId} type="submit" className="px-btn" data-size="s" disabled={busy}
                       data-testid="px-drawer-needs-reject-send">Reject</GatedButton>
          <button type="button" className="px-btn" data-size="s" onClick={() => setRejecting(false)}>Cancel</button>
        </form>
      ) : (
        <div className="px-row">
          <GatedButton permission="decision:approve" projectId={ctx.projectId} className="px-btn" data-kind="amber" data-size="s" disabled={busy}
                       data-testid="px-drawer-needs-approve" onClick={() => void answer("approve")}>
            {n.kind === "merge" ? <GitMerge size={12} aria-hidden /> : n.kind === "deploy" ? <RocketLaunch size={12} aria-hidden /> : <Check size={12} aria-hidden />} {actWord(n)}</GatedButton>
          <GatedButton permission="decision:approve" projectId={ctx.projectId} className="px-btn" data-size="s" disabled={busy}
                       data-testid="px-drawer-needs-reject" onClick={() => setRejecting(true)}>Reject</GatedButton>
          {review ? <PxLink href={review} testid="px-drawer-needs-review">Review on GitHub</PxLink> : null}
        </div>
      )}
      <Said text={said.text} tone={said.tone} />
    </div>
  );
}
