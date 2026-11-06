"use client";

/**
 * WORK (screen 5, contract §3): new work inline (Feature · Bug · Ticket, Helm's plan as a strip),
 * then every task as a row — the station strip, who works it, its cost and every link — grouped
 * Needs you · Running · Planned · Done. A row opens its drawer in place. List or Board.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  Bug, CheckCircle, Code, FlowArrow, GitPullRequest, Kanban, Lightning, ListBullets, Play, Plus, Sparkle, Terminal, Ticket, XCircle, Globe,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { post } from "../workflows/delivery-api";
import { ReadinessPanel, unaccepted, type Readiness } from "../workflow-readiness-ui";
import { startBody, type StartView } from "../../lib/start-project";
import {
  WORK_ORDER, WORK_WORDS, isMissingStatus, pxHref, startWork, writePxUrl, type WorkItem, type WorkState,
} from "../../lib/project-experience";
import { Empty, Face, Flow, Ico, Loading, Failed, EngineNotReady, PxLink, Said, Stations } from "./kit";
import type { PxCtx } from "./project-page";

type Kind = "feature" | "bug" | "ticket";
const KINDS: { key: Kind; label: string; icon: typeof Sparkle; ph: string }[] = [
  { key: "feature", label: "Feature", icon: Sparkle, ph: "Describe it — e.g. \"let agents tag tickets by product\"" },
  { key: "bug", label: "Bug", icon: Bug, ph: "What's wrong — e.g. \"SLA timer ignores weekends\"" },
  { key: "ticket", label: "Ticket", icon: Ticket, ph: "Paste a GitHub or Plane issue link" },
];
const STATE_ICON: Record<WorkState, React.ReactNode> = {
  needs_you: <GitPullRequest size={14} style={{ color: "var(--amber)" }} aria-hidden />, running: <Lightning size={14} weight="fill" style={{ color: "var(--teal)" }} aria-hidden />,
  planned: <ListBullets size={14} className="px-faint" aria-hidden />, done: <CheckCircle size={14} weight="fill" style={{ color: "var(--green)" }} aria-hidden />,
  failed: <XCircle size={14} weight="fill" style={{ color: "var(--red)" }} aria-hidden />,
};

export function WorkTab({ ctx }: { ctx: PxCtx }) {
  const [filter, setFilter] = useState<WorkState | "all">("all");
  const [view, setView] = useState<"list" | "board">("list");
  const items = ctx.work?.items ?? [];
  const counts = Object.fromEntries(WORK_ORDER.map((k) => [k, items.filter((w) => w.state === k).length])) as Record<WorkState, number>;
  const shown = items.filter((w) => filter === "all" || w.state === filter);
  return (
    <div className="px-stack" data-testid="px-work">
      <Composer ctx={ctx} />
      <div className="px-row px-wrap">
        <div className="px-filters" role="group" aria-label="Filter work">
          <button type="button" className="px-filter" aria-pressed={filter === "all"} onClick={() => setFilter("all")} data-testid="px-work-f-all">All <span className="px-mono">{items.length}</span></button>
          {WORK_ORDER.filter((k) => counts[k]).map((k) => (
            <button key={k} type="button" className="px-filter" aria-pressed={filter === k} onClick={() => setFilter(k)} data-testid={`px-work-f-${k}`}>
              <span className="px-dot" data-s={k === "needs_you" ? "you" : k === "running" ? "now" : k === "done" ? "done" : k === "failed" ? "bad" : undefined} aria-hidden />
              {WORK_WORDS[k]} <span className="px-mono">{counts[k]}</span></button>
          ))}
        </div>
        <span className="px-spacer" />
        <span className="px-legend px-hide-phone" aria-hidden>
          {[["P", "Plan"], ["D", "Decide"], ["B", "Build"], ["R", "Review"], ["PR", "PR"], ["S", "Staging"]].map(([k, v]) => <span key={k}><span className="px-st-n">{k}</span>{v}</span>)}
        </span>
        <div className="px-seg" role="group" aria-label="View">
          <button type="button" aria-pressed={view === "list"} onClick={() => setView("list")} data-testid="px-work-list"><ListBullets size={13} /> List</button>
          <button type="button" aria-pressed={view === "board"} onClick={() => setView("board")} data-testid="px-work-board"><Kanban size={13} /> Board</button>
        </div>
      </div>
      {!ctx.work ? (
        ctx.workState === "loading" ? <Loading rows={3} label="Loading work" />
          : ctx.workState === "error" ? <Failed what="Work" error={null} onRetry={ctx.refreshWork} />
          : <EngineNotReady what="The work list"><a className="px-btn" href={pxHref("?area=code_tasks")}>Open Code Tasks</a></EngineNotReady>
      ) : !items.length ? (
        <div className="px-card"><Empty icon={Kanban} title="No work yet" line="Describe a feature or a bug above. Helm plans it, you approve, the crew builds it to a PR." testid="px-work-empty" /></div>
      ) : view === "list" ? (
        <div className="px-list" data-testid="px-work-rows">
          {WORK_ORDER.filter((k) => shown.some((w) => w.state === k)).map((k) => (
            <React.Fragment key={k}>
              <div className="px-list-g"><span className="px-dot" data-s={k === "needs_you" ? "you" : k === "running" ? "now" : k === "done" ? "done" : k === "failed" ? "bad" : undefined} aria-hidden />
                {WORK_WORDS[k]}{k === "planned" ? <span className="px-faint" style={{ fontWeight: 400 }}> · ranked by Marshal</span> : null}</div>
              {shown.filter((w) => w.state === k).map((w) => <WorkRow key={w.id} w={w} ctx={ctx} />)}
            </React.Fragment>
          ))}
        </div>
      ) : (
        <div className="px-grid px-g4" data-testid="px-work-columns">
          {(["needs_you", "running", "planned", "done"] as WorkState[]).map((k) => (
            <section key={k} className="px-col" aria-label={WORK_WORDS[k]}>
              <div className="px-row" style={{ marginBottom: 8 }}><span className="px-lbl">{WORK_WORDS[k]}</span><span className="px-nbadge">{counts[k] ?? 0}</span></div>
              {shown.filter((w) => w.state === k).map((w) => (
                <button key={w.id} type="button" className="px-card" data-link onClick={() => ctx.open("work", w.id)} data-testid={`px-board-${w.id}`}
                        style={{ width: "100%", textAlign: "left", font: "inherit", color: "inherit", marginBottom: 8, display: "flex", flexDirection: "column", gap: 6, padding: 10 }}>
                  <b style={{ fontSize: ".66rem" }}>{w.label}</b><Stations stages={w.stages} />
                </button>
              ))}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

function Composer({ ctx }: { ctx: PxCtx }) {
  const [kind, setKind] = useState<Kind>("feature");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const input = useRef<HTMLInputElement>(null);
  // A start refused for what the project lacks: the checklist, and Start anyway with the ticks.
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  // "New work" in the header lands here with `&compose=1`: focus the box, then drop the flag.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.get("compose") === "1") {
      input.current?.focus();
      input.current?.scrollIntoView?.({ block: "center" });
      writePxUrl({ compose: null }, "replace");
    }
  });
  const start = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true); setSaid({ text: "" });
    try {
      let r = await startWork(ctx.projectId, kind, t);
      // AN ENGINE WITHOUT §3's POST: the project's normal start door (the old "What should we add?").
      if (!r.ok && isMissingStatus(r.status)) {
        const view = await fetch(`/v1/projects/${encodeURIComponent(ctx.projectId)}/start`, { cache: "no-store" }).then((x) => (x.ok ? x.json() : null)).catch(() => null);
        if (view) {
          const s = await post("start", { ...startBody(view as StartView, t), ...(startAnyway.length ? { start_anyway: startAnyway } : {}) });
          if (!s.ok && "readiness" in s && s.readiness) { setRefusal(s.readiness); return; }
          r = s.ok ? { ok: true, status: 200, body: {}, error: null } : { ok: false, status: 400, body: null, error: typeof s.error === "string" ? s.error : "It didn't start." };
        }
      }
      if (r.ok) {
        setText(""); setRefusal(null); setStartAnyway([]);
        setSaid({ text: r.demo ? "Sample mode — nothing was sent." : "Started — Helm is planning it.", tone: "green" });
        ctx.refreshWork();
      } else setSaid({ text: r.error ?? "It didn't start.", tone: "red" });
    } finally { setBusy(false); }
  };
  const k = KINDS.find((x) => x.key === kind)!;
  return (
    <section className="px-card" aria-label="New work" data-testid="px-composer" style={{ borderColor: "var(--accent-line)" }}>
      <form className="px-row px-wrap" onSubmit={(e) => { e.preventDefault(); void start(); }}>
        <Ico icon={Plus} c="teal" />
        <div className="px-seg" role="group" aria-label="Kind of work">
          {KINDS.map((x) => (
            <button key={x.key} type="button" aria-pressed={kind === x.key} onClick={() => setKind(x.key)} data-testid={`px-compose-${x.key}`}>
              <x.icon size={13} aria-hidden /> {x.label}</button>
          ))}
        </div>
        <label className="px-sr" htmlFor="px-compose-text">Describe the work</label>
        <input id="px-compose-text" ref={input} className="px-input" style={{ flex: "1 1 260px", width: "auto" }} value={text} placeholder={k.ph}
               onChange={(e) => setText(e.target.value)} data-testid="px-compose-text" maxLength={2000} />
        <GatedButton permission="workflow_run:start" projectId={ctx.projectId} type="submit" className="px-btn" data-kind="primary" data-testid="px-compose-start"
                     disabled={!text.trim() || busy || (!!refusal && unaccepted(refusal, startAnyway).length > 0)}>
          <Play size={12} weight="fill" aria-hidden /> {busy ? "Starting…" : refusal ? "Start anyway" : "Start"}</GatedButton>
      </form>
      <div className="px-row px-wrap" style={{ marginTop: 10 }}>
        <span className="px-lbl">Helm&apos;s plan</span>
        <Flow nodes={[
          { label: "Plan", type: "agent", icon: <Face agent={{ name: "Juno", role_key: "pm" }} size={16} /> },
          { label: "You approve", type: "waiting" },
          { label: "Build", type: "todo", icon: <Code size={11} aria-hidden /> },
          { label: "Review", type: "todo" }, { label: "PR", type: "todo", icon: <GitPullRequest size={11} aria-hidden /> },
          { label: "Staging", type: "todo", icon: <Globe size={11} aria-hidden /> },
        ]} />
      </div>
      {refusal ? <div style={{ marginTop: 10 }} data-testid="px-compose-readiness"><ReadinessPanel readiness={refusal} accepted={startAnyway} onAccept={setStartAnyway} /></div> : null}
      <Said text={said.text} tone={said.tone} />
    </section>
  );
}

export function WorkRow({ w, ctx }: { w: WorkItem; ctx: PxCtx }) {
  const [said, setSaid] = useState("");
  const startNow = async () => {
    const r = await startWork(ctx.projectId, w.kind === "bug" ? "bug" : "feature", w.detail && w.detail !== w.label ? `${w.label}\n\n${w.detail}` : w.label);
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Started.") : (r.error ?? "It didn't start."));
    if (r.ok && !r.demo) ctx.refreshWork();
  };
  return (
    <div className="px-wk" data-s={w.state} data-testid={`px-work-${w.id}`} onClick={(e) => { if (!(e.target as HTMLElement).closest("a,button")) ctx.open("work", w.id); }}>
      {STATE_ICON[w.state]}
      <span style={{ minWidth: 0 }}>
        <button type="button" className="px-wk-t px-ell" style={{ display: "block", maxWidth: "100%" }} onClick={() => ctx.open("work", w.id)} data-testid={`px-work-open-${w.id}`}>{w.label}</button>
        <span className="px-wk-sub">
          <span className="px-tag" data-tone={w.kind === "bug" ? "red" : w.source === "roadmap" ? "violet" : undefined}>{w.source === "roadmap" ? "from Roadmap" : w.kind[0].toUpperCase() + w.kind.slice(1)}</span>
          {w.source && w.source !== "roadmap" ? <span>{w.source}</span> : null}
          {w.age ? <span>{w.state === "needs_you" ? `Waiting ${w.age}` : w.age}</span> : null}
          {w.cost_usd !== null ? <span className="px-mono">${w.cost_usd.toFixed(2)}</span> : null}
        </span>
      </span>
      <Stations stages={w.stages} />
      <span className="px-faces">{w.agents.slice(0, 3).map((a, i) => <Face key={`${a.name}-${i}`} agent={a} size={20} state={w.state === "running" && i === w.agents.length - 1 ? "working" : "idle"} />)}</span>
      <span className="px-wk-links">
        {w.links.workflow_run ? <PxLink href={w.links.workflow_run} testid={`px-work-run-${w.id}`}><FlowArrow size={11} aria-hidden /> Run</PxLink> : null}
        {w.links.build ? <PxLink href={w.links.build} testid={`px-work-build-${w.id}`}><Code size={11} aria-hidden /> Build</PxLink> : null}
        {w.links.pr ? <PxLink href={w.links.pr.external_url} testid={`px-work-pr-${w.id}`}><GitPullRequest size={11} aria-hidden /> PR #{w.links.pr.number}</PxLink> : null}
        {w.links.preview ? <PxLink href={w.links.preview}><Globe size={11} aria-hidden /> Preview</PxLink> : null}
        {w.links.deploy ? <PxLink href={w.links.deploy}><Terminal size={11} aria-hidden /> Staging</PxLink> : null}
      </span>
      <span className="px-row">
        {w.state === "needs_you" ? (
          <button type="button" className="px-btn" data-kind="amber" data-size="s" data-testid={`px-work-act-${w.id}`} onClick={() => ctx.open("work", w.id)}>
            {w.links.pr ? "Merge" : "Decide"}</button>
        ) : w.state === "planned" ? (
          <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-work-start-${w.id}`}
                       onClick={() => void startNow()}><Play size={11} weight="fill" aria-hidden /> Start now</GatedButton>
        ) : null}
        <Said text={said} />
      </span>
    </div>
  );
}
