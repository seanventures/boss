"use client";

/**
 * WORK (screen 5, contract §3): new work inline (Feature · Bug · Ticket, Helm's plan as a strip),
 * then every task as a row — the station strip, who works it, its cost and every link — grouped
 * Needs you · Running · Planned · Done. A row opens its drawer in place. List or Board.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  ArrowBendDownRight, Bug, CaretDown, Check, Code, Database, EnvelopeSimple, FlowArrow, GitMerge, GitPullRequest, Kanban, Lightning, ListBullets,
  PauseCircle, Play, Plus, Sparkle, TerminalWindow, Ticket, XCircle, Globe,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { post } from "../workflows/delivery-api";
import { ReadinessPanel, unaccepted, type Readiness } from "../workflow-readiness-ui";
import { startBody, type StartView } from "../../lib/start-project";
import {
  ATTEMPT_WORDS, KIND_WORDS, WORK_ORDER, WORK_WORDS, attemptsPill, isMissingStatus, pxHref, startWork, writePxUrl,
  type Attempt, type WorkItem, type WorkState,
} from "../../lib/project-experience";
import { localWith, stampLocal, whenLocal } from "../../lib/local-time";
import { Empty, Face, Flow, Ico, Loading, Failed, EngineNotReady, PxLink, Said, Stations } from "./kit";
import type { PxCtx } from "./project-page";

type Kind = "feature" | "bug" | "ticket";
const KINDS: { key: Kind; label: string; icon: typeof Sparkle; ph: string }[] = [
  { key: "feature", label: "Feature", icon: Sparkle, ph: "Describe it — e.g. \"let agents tag tickets by product\"" },
  { key: "bug", label: "Bug", icon: Bug, ph: "What's wrong — e.g. \"SLA timer ignores weekends\"" },
  { key: "ticket", label: "Ticket", icon: Ticket, ph: "Paste a GitHub or Plane issue link" },
];
/** The row's tile (05-29): what the work is waiting on, else what kind it is. */
function RowTile({ w }: { w: WorkItem }) {
  const t: [React.ElementType, string | undefined, "bold" | "fill" | "regular"] =
    w.state === "needs_you" ? [GitMerge, "amber", "bold"]
    : w.state === "running" ? [Lightning, "teal", "fill"]
    : w.state === "stalled" ? [PauseCircle, "amber", "fill"]
    : w.state === "done" ? [Check, "green", "bold"]
    : w.state === "failed" ? [XCircle, "red", "regular"]
    : w.kind === "bug" || w.kind === "fix" ? [Bug, "red", "regular"]
    : w.kind === "chore" || /^ci$/i.test(w.source) ? [XCircle, "red", "regular"]
    : w.source === "roadmap" ? [Database, "violet", "regular"]
    : w.kind === "ticket" ? [Ticket, undefined, "regular"]
    : [EnvelopeSimple, undefined, "regular"];
  const [I, c, weight] = t;
  return <span className="px-ico px-wk-ico" data-c={c} data-plain={c ? undefined : ""} aria-hidden><I size={12} weight={weight} /></span>;
}

const STATION_NAME: Record<string, string> = { plan: "Plan", design: "Decide", build: "Build", review: "Review", pr: "PR", ship: "Staging" };
/** "Build · 12m": where a running piece of work is, and for how long. */
const whereNow = (w: WorkItem) => {
  const now = w.stages.find((st) => st.state === "now") ?? w.stages.find((st) => st.state === "todo");
  return [now ? STATION_NAME[now.key] ?? now.label : "", w.age].filter(Boolean).join(" · ");
};
/** A finished item's day, in the viewer's own zone: "Wed" this week, else "Nov 2". */
const doneDay = (w: WorkItem) => {
  const at = Date.parse(w.updated_at || w.created_at || "");
  if (!Number.isFinite(at)) return w.age;
  return Date.now() - at < 6 * 86_400_000 ? localWith(at, { weekday: "short" }) : localWith(at, { month: "short", day: "numeric" });
};

const DOT: Record<WorkState, string | undefined> = { needs_you: "you", stalled: "stalled", running: "now", done: "done", failed: "bad", planned: undefined };

export function WorkTab({ ctx }: { ctx: PxCtx }) {
  const [filter, setFilter] = useState<WorkState | "all">("all");
  const [view, setView] = useState<"list" | "board">("list");
  const items = ctx.work?.items ?? [];
  const counts = Object.fromEntries(WORK_ORDER.map((k) => [k, items.filter((w) => w.state === k).length])) as Record<WorkState, number>;
  const shown = items.filter((w) => filter === "all" || w.state === filter);
  // The board's columns: the four of the wireframe, plus Stalled and Failed whenever any exist
  // (an item never disappears because the board has no column for it).
  const boardCols = (["needs_you", "stalled", "running", "planned", "done", "failed"] as WorkState[])
    .filter((k) => (k !== "stalled" && k !== "failed") || counts[k]);
  return (
    <div className="px-stack" data-testid="px-work">
      <Composer ctx={ctx} />
      <div className="px-row px-wrap">
        <div className="px-filters" role="group" aria-label="Filter work">
          <button type="button" className="px-filter" aria-pressed={filter === "all"} onClick={() => setFilter("all")} data-testid="px-work-f-all">All <span className="px-mono">{items.length}</span></button>
          {WORK_ORDER.filter((k) => counts[k]).map((k) => (
            <button key={k} type="button" className="px-filter" aria-pressed={filter === k} onClick={() => setFilter(k)} data-testid={`px-work-f-${k}`}>
              <span className="px-dot" data-s={DOT[k]} aria-hidden />
              {WORK_WORDS[k]} <span className="px-mono">{counts[k]}</span></button>
          ))}
        </div>
        <span className="px-spacer" />
        <span className="px-legend px-hide-phone" aria-hidden>
          {[["P", "Plan"], ["D", "Decide"], ["B", "Build"], ["R", "Review"], ["PR", "PR"], ["S", "Staging"]].map(([k, v]) => <span key={k}><span className="px-st-n">{k}</span>{v}</span>)}
        </span>
        <div className="px-seg px-hide-phone" role="group" aria-label="View">
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
              <div className="px-list-g"><span className="px-dot" data-s={DOT[k]} aria-hidden />
                {WORK_WORDS[k]}{k === "planned" ? <span className="px-faint" style={{ fontWeight: 400 }}> · ranked by Marshal</span>
                  : k === "stalled" ? <span className="px-faint" style={{ fontWeight: 400 }}> · nothing has moved; open one to retry or stop it</span> : null}</div>
              {shown.filter((w) => w.state === k).map((w, i) => <WorkRow key={w.id} w={w} ctx={ctx} first={k === "planned" && i === 0} />)}
            </React.Fragment>
          ))}
        </div>
      ) : (
        <div className={`px-grid px-g${boardCols.length}`} data-testid="px-work-columns">
          {boardCols.map((k) => (
            <section key={k} className="px-col" aria-label={WORK_WORDS[k]}>
              <div className="px-row" style={{ marginBottom: 8 }}><span className="px-lbl">{WORK_WORDS[k]}</span><span className="px-nbadge">{shown.filter((w) => w.state === k).length}</span></div>
              {shown.filter((w) => w.state === k).map((w) => (
                <button key={w.id} type="button" className="px-card" data-link onClick={() => ctx.open("work", w.id)} data-testid={`px-board-${w.id}`}
                        style={{ width: "100%", textAlign: "left", font: "inherit", color: "inherit", marginBottom: 8, display: "flex", flexDirection: "column", gap: 6, padding: 10 }}>
                  <b style={{ fontSize: ".66rem" }}>{w.label}</b><Stations stages={w.stages} />
                  {attemptsPill(w) || w.state === "stalled" ? (
                    <span className="px-row px-wrap" style={{ gap: 6 }}>
                      {w.state === "stalled" ? <StalledTag w={w} /> : null}
                      {attemptsPill(w) ? <span className="px-attempts-pill" data-testid={`px-board-attempts-${w.id}`}>{attemptsPill(w)}</span> : null}
                    </span>
                  ) : null}
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
      let r = await startWork(ctx.projectId, kind, t, startAnyway.length ? { start_anyway: startAnyway } : {});
      // NOT READY (409 from THE start door, §3): the project's checklist, and Start anyway with the ticks.
      if (!r.ok && r.readiness) { setRefusal(r.readiness as Readiness); return; }
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

/** "Stalled · since Tue 14:05": amber, in the viewer's own time. */
export function StalledTag({ w }: { w: Pick<WorkItem, "stalled_since" | "id"> }) {
  const since = whenLocal(w.stalled_since);
  return (
    <span className="px-tag" data-tone="amber" data-testid={`px-work-stalled-${w.id}`}
          title={w.stalled_since ? `No progress since ${stampLocal(w.stalled_since)}` : "No progress"}>
      Stalled{since ? ` · since ${since}` : ""}</span>
  );
}

export function WorkRow({ w, ctx, first = false }: { w: WorkItem; ctx: PxCtx; first?: boolean }) {
  const [said, setSaid] = useState("");
  const [open, setOpen] = useState(false);
  const startNow = async () => {
    // A planned roadmap item starts as itself: its title, and the item it came from.
    const r = await startWork(ctx.projectId, w.kind === "bug" ? "bug" : "feature", w.detail && w.detail !== w.label ? `${w.label}\n\n${w.detail}` : w.label,
                              { title: w.label, ...(w.source === "roadmap" ? { source_ref: `rec:${w.id}` } : {}) });
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Started.") : (r.error ?? "It didn't start."));
    if (r.ok && !r.demo) ctx.refreshWork();
  };
  const pill = attemptsPill(w);
  const grouped = w.attempts.length > 1;
  const regionId = `px-attempts-${w.id}`;
  // Never a raw internal string ("code_task"): the source's human word, or the source as given.
  const source = w.source === "roadmap" ? "" : w.source_label || (/^[a-z]+_[a-z_]+$/.test(w.source) ? "" : w.source);
  return (
    <>
      <div className="px-wk" data-s={w.state} data-open={open || undefined} data-testid={`px-work-${w.id}`}
           onClick={(e) => { if (!(e.target as HTMLElement).closest("a,button")) ctx.open("work", w.id); }}>
        <RowTile w={w} />
        <span style={{ minWidth: 0 }}>
          <span className="px-row" style={{ gap: 6, flexWrap: "nowrap", minWidth: 0 }}>
            <button type="button" className="px-wk-t px-ell" style={{ display: "block", minWidth: 0 }} onClick={() => ctx.open("work", w.id)} data-testid={`px-work-open-${w.id}`}>{w.label}</button>
            {grouped ? (
              <button type="button" className="px-attempts-pill" aria-expanded={open} aria-controls={regionId} data-testid={`px-work-attempts-${w.id}`}
                      title={`${w.attempt_count} attempts: ${w.followups} follow-up${w.followups === 1 ? "" : "s"}, ${w.retries} retr${w.retries === 1 ? "y" : "ies"}`}
                      onClick={() => setOpen((v) => !v)}>
                {pill}<CaretDown size={10} weight="bold" className="px-chev" aria-hidden /></button>
            ) : null}
          </span>
          <span className="px-wk-sub">
            <span className="px-tag" data-tone={w.kind === "bug" || w.kind === "fix" ? "red" : w.source === "roadmap" ? "violet" : undefined}>{w.source === "roadmap" ? "from Roadmap" : KIND_WORDS[w.kind] ?? w.kind}</span>
            {source && w.source !== "code_task" && w.source !== "workflow_run" ? <span>{source}</span> : null}
            {w.state === "stalled" ? <StalledTag w={w} /> : null}
            {w.state === "needs_you" ? (w.age ? <span>Waiting {w.age}</span> : null)
              : w.state === "running" ? <span>{whereNow(w)}</span>
              : w.state === "planned" ? (first ? <span>Next up</span> : null)
              : w.state === "done" || w.state === "failed" ? <span>{doneDay(w)}</span>
              : w.state === "stalled" ? null : w.age ? <span>{w.age}</span> : null}
            {w.cost_usd !== null && (w.cost_usd > 0 || w.state !== "planned") ? <span className="px-mono">${w.cost_usd.toFixed(2)}</span> : null}
          </span>
        </span>
        <Stations stages={w.stages} />
        <span className="px-faces">{w.agents.slice(0, 3).map((a, i) => <Face key={`${a.name}-${i}`} agent={a} size={20} state={w.state === "running" && i === w.agents.length - 1 ? "working" : "idle"} />)}</span>
        <span className="px-wk-links">
          {w.links.workflow_run ? <PxLink href={w.links.workflow_run} title="Workflow run" testid={`px-work-run-${w.id}`}><FlowArrow size={11} aria-hidden /> {w.run_number ? `Run #${w.run_number}` : "Run"}</PxLink> : null}
          {w.links.build && w.state !== "done" ? <PxLink href={w.links.build} title="Build page in Code Tasks" testid={`px-work-build-${w.id}`}><Code size={11} aria-hidden /> Build</PxLink> : null}
          {w.links.build && (w.state === "running" || w.state === "stalled") ? <PxLink href={`${w.links.build}&view=logs`} title="The build's live log" testid={`px-work-log-${w.id}`}><TerminalWindow size={11} aria-hidden /> Live log</PxLink> : null}
          {w.links.pr ? <PxLink href={w.links.pr.external_url} title="Pull request on GitHub" testid={`px-work-pr-${w.id}`}>{w.state === "done" ? <GitMerge size={11} aria-hidden /> : <GitPullRequest size={11} aria-hidden />} PR #{w.links.pr.number}</PxLink> : null}
          {w.links.preview ? <PxLink href={w.links.preview} title="The running preview"><Globe size={11} aria-hidden /> Preview</PxLink> : null}
          {w.links.deploy ? <PxLink href={w.links.deploy} title="Where it is deployed"><Globe size={11} aria-hidden /> Staging</PxLink> : null}
        </span>
        <span className="px-row">
          {w.state === "needs_you" ? (
            <button type="button" className="px-btn" data-kind="amber" data-size="s" data-testid={`px-work-act-${w.id}`} onClick={() => ctx.open("work", w.id)}>
              {w.action_label || (w.links.pr ? "Merge" : "Decide")}</button>
          ) : w.state === "planned" ? (
            <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-work-start-${w.id}`}
                         onClick={() => void startNow()}><Play size={11} weight="fill" aria-hidden /> Start now</GatedButton>
          ) : null}
          <Said text={said} />
        </span>
      </div>
      {grouped ? (
        <div className="px-attempts" id={regionId} data-open={open || undefined} role="region" aria-label={`Attempts of ${w.label}`} hidden={!open} data-testid={`px-work-attempts-list-${w.id}`}>
          <div className="px-attempts-in">
            {w.attempts.map((t) => <AttemptRow key={t.id} t={t} ctx={ctx} />)}
          </div>
        </div>
      ) : null}
    </>
  );
}

const ATTEMPT_DOT: Record<Attempt["state"], string | undefined> = {
  running: "now", stalled: "stalled", needs_you: "you", done: "done", failed: "bad", handed_on: undefined, planned: undefined,
};

/** One attempt in a group's expander: what it was, when (local), how it ended, its PR, open it. */
function AttemptRow({ t, ctx }: { t: Attempt; ctx: PxCtx }) {
  const when = localWith(t.created_at, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return (
    <div className="px-att" data-s={t.state} data-testid={`px-attempt-${t.id}`}>
      <ArrowBendDownRight size={12} className="px-faint" aria-hidden />
      <span className="px-dot" data-s={ATTEMPT_DOT[t.state]} aria-hidden />
      <span style={{ minWidth: 0 }}>
        <b className="px-ell" style={{ display: "block" }}>{t.label}</b>
        <small className="px-faint">{when}</small>
      </span>
      <span className="px-tag" data-tone={t.state === "stalled" || t.state === "needs_you" ? "amber" : t.state === "failed" ? "red" : t.state === "done" ? "green" : t.state === "running" ? "blue" : "idle"}
            title={t.state === "handed_on" ? "Its checks failed; the repair continued in the next attempt." : t.stalled_since ? `No progress since ${stampLocal(t.stalled_since)}` : undefined}>
        {ATTEMPT_WORDS[t.state]}</span>
      <span className="px-mono px-faint px-hide-phone">{t.cost_usd !== null ? `$${t.cost_usd.toFixed(2)}` : ""}</span>
      <span className="px-row" style={{ gap: 4, justifyContent: "flex-end" }}>
        {t.pr ? <PxLink href={t.pr.external_url} testid={`px-attempt-pr-${t.id}`}><GitPullRequest size={11} aria-hidden /> #{t.pr.number}</PxLink> : null}
        <button type="button" className="px-btn" data-size="s" onClick={() => ctx.open("work", t.id)} data-testid={`px-attempt-open-${t.id}`}>Open</button>
      </span>
    </div>
  );
}

/** A grouped item's attempts in its drawer (05b): newest first, each opens as itself. */
export function DrawerAttempts({ w, ctx }: { w: WorkItem; ctx: PxCtx }) {
  if (w.attempts.length <= 1) return null;
  return (
    <div data-testid="px-drawer-work-attempts">
      <span className="px-lbl">{w.attempt_count} attempts</span>
      <div className="px-steps" style={{ marginTop: 6 }}>
        {w.attempts.map((t) => (
          <button key={t.id} type="button" className="px-li" style={{ width: "100%", background: "none", border: 0, font: "inherit", color: "inherit", textAlign: "left", cursor: "pointer" }}
                  onClick={() => ctx.open("work", t.id)} data-testid={`px-drawer-attempt-${t.id}`}>
            <span className="px-dot" data-s={ATTEMPT_DOT[t.state]} aria-hidden />
            <span style={{ minWidth: 0 }}><b>{t.label}</b><small>{whenLocal(t.created_at)}{t.pr ? ` · PR #${t.pr.number}` : ""}</small></span>
            <span className="px-tag" data-tone={t.state === "stalled" ? "amber" : t.state === "failed" ? "red" : t.state === "done" ? "green" : undefined}>{ATTEMPT_WORDS[t.state]}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
