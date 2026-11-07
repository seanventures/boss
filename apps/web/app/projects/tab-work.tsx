"use client";

/**
 * WORK (screen 5, contract §3/§14): new work inline (Feature · Bug · Ticket) with Helm's plan —
 * the chosen workflow's real steps as a strip, ≈ time and ≈ cost from this project's history, and
 * "change" to pick another workflow — then every task as a row: the station strip (the onboarding
 * row's own Discover → Ready → Backlog), who works it, its cost, its issue chip and every link,
 * grouped Needs you · Running · Planned · Done. Needs you is the header bar's own list (one
 * count); Planned is Marshal's rank, re-ranked by drag or Alt+↑/↓ and saved through the plan
 * route. A row opens its drawer in place. List or Board.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowBendDownRight, ArrowDown, ArrowUp, Bug, CaretDown, Check, Code, Compass, Database, DotsSixVertical, EnvelopeSimple, FlowArrow, GitMerge,
  GitPullRequest, GithubLogo, Globe, Hammer, HandWaving, Kanban, Lightning, ListBullets, MagnifyingGlass, PauseCircle, Play, Plus, RocketLaunch,
  Sparkle, TerminalWindow, Ticket, XCircle,
} from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { post } from "../workflows/delivery-api";
import { ReadinessPanel, unaccepted, type Readiness } from "../workflow-readiness-ui";
import { startBody, type StartView } from "../../lib/start-project";
import {
  ATTEMPT_WORDS, KIND_WORDS, PX_PATHS, WORK_ORDER, WORK_WORDS, attemptsPill, dollarsAbout, isMissingStatus, movePlanItem, needsYouCards,
  parseWorkPlan, pxHref, reRank, runAccept, startWork, usePxRead, writePxUrl,
  type Attempt, type NeedsYou, type PlanNode, type WorkItem, type WorkPlan, type WorkState,
} from "../../lib/project-experience";
import { FX_WORK_PLAN } from "../../lib/project-experience-fixtures";
import { localWith, stampLocal, whenLocal } from "../../lib/local-time";
import { Empty, Face, Flow, Ico, Loading, Failed, EngineNotReady, PxLink, Said, Stations } from "./kit";
import type { PxCtx } from "./project-page";
import "./work.css";

type Kind = "feature" | "bug" | "ticket";
const KINDS: { key: Kind; label: string; icon: typeof Sparkle; ph: string }[] = [
  { key: "feature", label: "Feature", icon: Sparkle, ph: "Describe it — e.g. \"let agents tag tickets by product\"" },
  { key: "bug", label: "Bug", icon: Bug, ph: "What's wrong — e.g. \"SLA timer ignores weekends\"" },
  { key: "ticket", label: "Ticket", icon: Ticket, ph: "Paste a GitHub or Plane issue link" },
];
/** The row's tile (05-29): what the work is waiting on, else what kind it is and where it came from. */
function RowTile({ w }: { w: WorkItem }) {
  const setup = w.kind === "setup";
  const t: [React.ElementType, string | undefined, "bold" | "fill" | "regular"] =
    setup ? (w.state === "needs_you" ? [HandWaving, "amber", "regular"] : w.state === "running" ? [Compass, "teal", "fill"]
      : w.state === "failed" ? [XCircle, "red", "regular"] : [Check, "green", "bold"])
    : w.state === "needs_you" ? [GitMerge, "amber", "bold"]
    : w.state === "running" ? [Lightning, "teal", "fill"]
    : w.state === "stalled" ? [PauseCircle, "amber", "fill"]
    : w.state === "done" ? [Check, "green", "bold"]
    : w.state === "failed" ? [XCircle, "red", "regular"]
    : w.origin === "ci" || /^ci$/i.test(w.source) ? [XCircle, "red", "regular"]
    : w.kind === "bug" || w.kind === "fix" || w.origin === "bug" ? [Bug, "red", "regular"]
    : w.origin === "roadmap" ? [Database, "violet", "regular"]
    : w.kind === "chore" ? [XCircle, "red", "regular"]
    : w.source === "roadmap" && !w.origin ? [Database, "violet", "regular"]
    : w.kind === "ticket" ? [Ticket, undefined, "regular"]
    : [EnvelopeSimple, undefined, "regular"];
  const [I, c, weight] = t;
  return <span className="px-ico px-wk-ico" data-c={c} data-plain={c ? undefined : ""} aria-hidden><I size={12} weight={weight} /></span>;
}

/** The row's tag: CI · Bug · from Roadmap for planned work by where it came from, else its kind. */
function rowTag(w: WorkItem): { label: string; tone?: "red" | "violet" } {
  if (w.state === "planned" && (w.origin === "ci" || w.origin === "bug")) return { label: w.origin_label || (w.origin === "ci" ? "CI" : "Bug"), tone: "red" };
  if (w.state === "planned" && (w.origin === "roadmap" || (!w.origin && w.source === "roadmap"))) return { label: "from Roadmap", tone: "violet" };
  if (w.source === "roadmap" && !w.origin && w.state !== "planned") return { label: "from Roadmap", tone: "violet" };
  return { label: KIND_WORDS[w.kind] ?? w.kind, tone: w.kind === "bug" || w.kind === "fix" ? "red" : undefined };
}

const STATION_NAME: Record<string, string> = { plan: "Plan", design: "Decide", build: "Build", review: "Review", pr: "PR", ship: "Staging" };
/** "Build · 12m": where a running piece of work is, and for how long. */
const whereNow = (w: WorkItem) => {
  const now = w.flow.length ? w.flow.find((f) => f.state === "now") : w.stages.find((st) => st.state === "now") ?? w.stages.find((st) => st.state === "todo");
  return [now ? STATION_NAME[now.key] ?? now.label : "", w.age].filter(Boolean).join(" · ");
};
/** A finished item's day, in the viewer's own zone: "Wed" this week, else "Nov 2". */
const doneDay = (w: WorkItem) => {
  const at = Date.parse(w.updated_at || w.created_at || "");
  if (!Number.isFinite(at)) return w.age;
  return Date.now() - at < 6 * 86_400_000 ? localWith(at, { weekday: "short" }) : localWith(at, { month: "short", day: "numeric" });
};

const DOT: Record<WorkState, string | undefined> = { needs_you: "you", stalled: "stalled", running: "now", done: "done", failed: "bad", planned: undefined };

/** Needs you, from ONE source (lib needsYouCards — the header bar's own list): the work rows that
 *  wait on a person, plus the cards that are no row's (Pick a prod host). The count is the bar's. */
function useNeeds(ctx: PxCtx, items: WorkItem[]) {
  return useMemo(() => {
    const onboarding = ctx.overview.project.stage === "onboarding" ? ctx.onboarding : null;
    const cards = needsYouCards(ctx.overview.needs_you, onboarding, items);
    const rows = items.filter((w) => w.state === "needs_you");
    const rowIds = new Set(rows.map((w) => w.id));
    const loose = cards.filter((c) => !c.work_id || !rowIds.has(c.work_id));
    return { cards, rows, loose, count: cards.length };
  }, [ctx.overview.needs_you, ctx.overview.project.stage, ctx.onboarding, items]);
}

export function WorkTab({ ctx }: { ctx: PxCtx }) {
  const [filter, setFilter] = useState<WorkState | "all">("all");
  const [view, setView] = useState<"list" | "board">("list");
  // The row a Start just made: it slides in (150 ms) once the list has it.
  const [fresh, setFresh] = useState("");
  const raw = ctx.work?.items ?? [];
  // DRAG TO RE-RANK (05-23): the order a person just set shows at once; a refusal puts it back.
  const [order, setOrder] = useState<string[] | null>(null);
  const [rankSaid, setRankSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const items = useMemo(() => {
    if (!order) return raw;
    const at = new Map(order.map((id, i) => [id, i]));
    const planned = raw.filter((w) => w.state === "planned").sort((x, y) => (at.get(x.id) ?? 1e6) - (at.get(y.id) ?? 1e6));
    let i = 0;
    return raw.map((w) => (w.state === "planned" ? planned[i++] : w));
  }, [raw, order]);
  // A fresh read from BOSS is the truth again.
  useEffect(() => { setOrder(null); }, [ctx.work]);
  const needs = useNeeds(ctx, items);
  const counts = Object.fromEntries(WORK_ORDER.map((k) => [k, k === "needs_you" ? Math.max(needs.count, needs.rows.length) : items.filter((w) => w.state === k).length])) as Record<WorkState, number>;
  const shown = items.filter((w) => filter === "all" || w.state === filter);
  const planned = items.filter((w) => w.state === "planned");
  const rankGate = useGate("work_item:manage", ctx.projectId);
  const rankable = rankGate.allowed && !ctx.demo && planned.some((w) => w.move);
  const rank = async (id: string, to: number) => {
    const plan = reRank(planned, id, to);
    const w = planned.find((x) => x.id === id);
    if (!plan || !w?.move) return;
    const before = planned.map((x) => x.id);
    setOrder(plan.order.map((x) => x.id)); setRankSaid({ text: "" });
    const r = await movePlanItem(ctx.projectId, id, plan.lane, plan.before);
    if (!r.ok) { setOrder(before); setRankSaid({ text: r.error ?? "The new order wasn't saved.", tone: "red" }); return; }
    setRankSaid({ text: r.demo ? "Sample mode — nothing was sent." : `Saved: ${w.label} is ${plan.order.findIndex((x) => x.id === id) === 0 ? "next up" : `#${plan.order.findIndex((x) => x.id === id) + 1}`}.`, tone: "green" });
    if (!r.demo) ctx.refreshWork();
  };
  // The board's columns: the four of the wireframe, plus Stalled and Failed whenever any exist
  // (an item never disappears because the board has no column for it).
  const boardCols = (["needs_you", "stalled", "running", "planned", "done", "failed"] as WorkState[])
    .filter((k) => (k !== "stalled" && k !== "failed") || counts[k]);
  const groupOf = (k: WorkState) => shown.filter((w) => w.state === k);
  const boardDrag = useRankDrag(planned, rankable, (id, to) => void rank(id, to));
  const showLoose = filter === "all" || filter === "needs_you";
  return (
    <div className="px-stack" data-testid="px-work">
      <Composer ctx={ctx} onStarted={setFresh} />
      <div className="px-row px-wrap">
        <div className="px-filters" role="group" aria-label="Filter work">
          <button type="button" className="px-filter" aria-pressed={filter === "all"} onClick={() => setFilter("all")} data-testid="px-work-f-all">All <span className="px-mono">{items.length}</span></button>
          {WORK_ORDER.filter((k) => counts[k]).map((k) => (
            <button key={k} type="button" className="px-filter" aria-pressed={filter === k} onClick={() => setFilter(k)} data-testid={`px-work-f-${k}`}
                    title={k === "needs_you" ? "The same list as the Needs you bar at the top" : undefined}>
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
      ) : !items.length && !needs.loose.length ? (
        <div className="px-card"><Empty icon={Kanban} title="No work yet" line="Describe a feature or a bug above. Helm plans it, you approve, the crew builds it to a PR." testid="px-work-empty" /></div>
      ) : view === "list" ? (
        <div className="px-list" data-testid="px-work-rows">
          {WORK_ORDER.filter((k) => groupOf(k).length || (k === "needs_you" && showLoose && needs.loose.length)).map((k) => (
            <React.Fragment key={k}>
              <div className="px-list-g"><span className="px-dot" data-s={DOT[k]} aria-hidden />
                {WORK_WORDS[k]}{k === "planned" ? <span className="px-faint" style={{ fontWeight: 400 }}> · ranked by Marshal{rankable ? <span className="px-hide-phone"> · drag to re-rank</span> : null}</span>
                  : k === "stalled" ? <span className="px-faint" style={{ fontWeight: 400 }}> · nothing has moved; open one to retry or stop it</span> : null}
                {k === "planned" ? <Said text={rankSaid.text} tone={rankSaid.tone} /> : null}</div>
              {k === "planned" ? (
                <PlannedRows rows={groupOf(k)} all={planned} ctx={ctx} rankable={rankable} onRank={(id, to) => void rank(id, to)} fresh={fresh} />
              ) : groupOf(k).map((w) => <WorkRow key={w.id} w={w} ctx={ctx} fresh={fresh === w.id} />)}
              {k === "needs_you" && showLoose ? needs.loose.map((c) => <NeedsCardRow key={c.id} card={c} ctx={ctx} />) : null}
            </React.Fragment>
          ))}
        </div>
      ) : (
        <div className={`px-grid px-g${boardCols.length}`} data-testid="px-work-columns">
          {boardCols.map((k) => (
            <section key={k} className="px-col" aria-label={WORK_WORDS[k]}>
              <div className="px-row" style={{ marginBottom: 8 }}><span className="px-lbl">{WORK_WORDS[k]}</span><span className="px-nbadge">{k === "needs_you" ? counts.needs_you : groupOf(k).length}</span></div>
              {groupOf(k).map((w) => (
                <button key={w.id} type="button" className="px-card" data-link onClick={() => openItem(ctx, w)} data-testid={`px-board-${w.id}`}
                        {...(k === "planned" ? boardDrag.props(w) : {})}
                        style={{ width: "100%", textAlign: "left", font: "inherit", color: "inherit", marginBottom: 8, display: "flex", flexDirection: "column", gap: 6, padding: 10 }}>
                  <b style={{ fontSize: ".66rem" }}>{w.label}</b>{w.flow.length ? <Flow nodes={w.flow.map((f) => ({ label: f.label, state: f.state, type: f.state }))} /> : <Stations stages={w.stages} />}
                  {attemptsPill(w) || w.state === "stalled" || w.next_up ? (
                    <span className="px-row px-wrap" style={{ gap: 6 }}>
                      {w.next_up ? <span className="px-tag" data-tone="blue">Next up</span> : null}
                      {w.state === "stalled" ? <StalledTag w={w} /> : null}
                      {attemptsPill(w) ? <span className="px-attempts-pill" data-testid={`px-board-attempts-${w.id}`}>{attemptsPill(w)}</span> : null}
                    </span>
                  ) : null}
                </button>
              ))}
              {k === "needs_you" ? needs.loose.map((c) => (
                <button key={c.id} type="button" className="px-card" data-link onClick={() => ctx.open("needs", c.id)} data-testid={`px-board-needs-${c.id}`}
                        style={{ width: "100%", textAlign: "left", font: "inherit", color: "inherit", marginBottom: 8, display: "flex", gap: 6, padding: 10, alignItems: "center" }}>
                  <HandWaving size={12} aria-hidden style={{ color: "var(--amber)" }} /><b style={{ fontSize: ".66rem" }}>{c.label}</b>
                </button>
              )) : null}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

/** Opening a row: the work drawer (05b) in place; the onboarding row opens the onboarding page (screen 10). */
function openItem(ctx: PxCtx, w: WorkItem) {
  if (w.kind === "setup" && w.source === "onboarding") writePxUrl({ tab: "onboarding", drawer: null });
  else ctx.open("work", w.id);
}

/** A Needs-you card that is no piece of work's (Pick a prod host, a setup gap): its own row, the
 *  bar's same item, opening the same drawer. */
function NeedsCardRow({ card, ctx }: { card: NeedsYou; ctx: PxCtx }) {
  return (
    <div className="px-wk px-wk-card" data-s="needs_you" data-testid={`px-work-needs-${card.id}`}
         onClick={(e) => { if (!(e.target as HTMLElement).closest("a,button")) ctx.open("needs", card.id); }}>
      <span className="px-ico px-wk-ico" data-c="amber" aria-hidden><HandWaving size={12} /></span>
      <span style={{ minWidth: 0 }}>
        <button type="button" className="px-wk-t px-ell" style={{ display: "block", minWidth: 0 }} onClick={() => ctx.open("needs", card.id)}>{card.label}</button>
        <span className="px-wk-sub"><span className="px-tag" data-tone="amber">Only you</span>{card.detail ? <span className="px-ell">{card.detail}</span> : null}</span>
      </span>
      <span className="px-hide-phone" />
      <span className="px-faces" />
      <span className="px-wk-links" />
      <span className="px-row">
        <button type="button" className="px-btn" data-kind="amber" data-size="s" data-testid={`px-work-needs-act-${card.id}`} onClick={() => ctx.open("needs", card.id)}>
          {card.action?.label || "Open"}</button>
      </span>
    </div>
  );
}

/** PLANNED, in Marshal's rank (05-18): drag a row onto another to put it there, or use the row's
 *  ↑ ↓ (and Alt+↑ / Alt+↓ on a focused row) — the same move, saved through the plan route. */
function PlannedRows({ rows, all, ctx, rankable, onRank, fresh }: {
  rows: WorkItem[]; all: WorkItem[]; ctx: PxCtx; rankable: boolean; onRank: (id: string, to: number) => void; fresh: string;
}) {
  const drag = useRankDrag(all, rankable, onRank);
  return (
    <div role="list" aria-label="Planned work, in rank order" data-testid="px-work-planned">
      {rows.map((w) => {
        const i = drag.index(w.id);
        const can = drag.can(w);
        return (
          <div key={w.id} role="listitem" data-testid={`px-rank-${w.id}`} {...drag.props(w)}
               onKeyDown={can ? (e) => {
                 if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
                 e.preventDefault();
                 onRank(w.id, i + (e.key === "ArrowUp" ? -1 : 1));
               } : undefined}>
            <WorkRow w={w} ctx={ctx} fresh={fresh === w.id}
                     rank={can ? { up: i > 0 ? () => onRank(w.id, i - 1) : null, down: i < all.length - 1 ? () => onRank(w.id, i + 1) : null, position: i + 1, total: all.length } : null} />
          </div>
        );
      })}
    </div>
  );
}

/** Drag to re-rank, shared by the list's Planned group and the board's Planned column: drop a
 *  row on another to put it in that one's place. */
function useRankDrag(all: WorkItem[], rankable: boolean, onRank: (id: string, to: number) => void) {
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const index = (id: string) => all.findIndex((w) => w.id === id);
  const can = (w: WorkItem) => rankable && Boolean(w.move);
  const props = (w: WorkItem) => {
    const ok = can(w);
    return {
      draggable: ok,
      "data-dragging": dragging === w.id || undefined,
      "data-drag-over": (over === w.id && dragging !== w.id) || undefined,
      onDragStart: ok ? (e: React.DragEvent) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", w.id); setDragging(w.id); } : undefined,
      onDragEnd: () => { setDragging(null); setOver(null); },
      onDragOver: ok ? (e: React.DragEvent) => { if (dragging && dragging !== w.id) { e.preventDefault(); setOver(w.id); } } : undefined,
      onDragLeave: () => setOver((cur) => (cur === w.id ? null : cur)),
      onDrop: ok ? (e: React.DragEvent) => {
        e.preventDefault();
        const id = dragging || e.dataTransfer.getData("text/plain");
        setDragging(null); setOver(null);
        if (id && id !== w.id) onRank(id, index(w.id));
      } : undefined,
    };
  };
  return { index, can, props };
}

const NODE_ICON: Record<string, React.ReactNode> = {
  build: <Hammer size={11} aria-hidden />, review: <MagnifyingGlass size={11} aria-hidden />, pr: <GitPullRequest size={11} aria-hidden />,
  ship: <RocketLaunch size={11} aria-hidden />, design: <Sparkle size={11} aria-hidden />,
};
/** Before BOSS answers (or on an engine without the plan route): the wireframe's strip, said as typical. */
const TYPICAL: PlanNode[] = [
  { key: "plan", label: "Plan", type: "agent", agent: { name: "Juno", role_key: "pm", avatar: "" } },
  { key: "approve", label: "You approve", type: "you", agent: null },
  { key: "build", label: "Build", type: "todo", agent: null }, { key: "review", label: "Review", type: "todo", agent: null },
  { key: "pr", label: "PR", type: "todo", agent: null }, { key: "ship", label: "Staging", type: "todo", agent: null },
];

function Composer({ ctx, onStarted }: { ctx: PxCtx; onStarted: (id: string) => void }) {
  const [kind, setKind] = useState<Kind>("feature");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const input = useRef<HTMLInputElement>(null);
  // "change" on Helm's plan: another of this project's workflows (cleared when the kind changes).
  const [picked, setPicked] = useState("");
  const [choosing, setChoosing] = useState(false);
  // HELM'S PLAN for this kind (and the picked workflow), read before Start (05-31).
  const plan = usePxRead<WorkPlan>(PX_PATHS.workPlan(ctx.projectId, kind, picked), parseWorkPlan, { everyMs: 120_000, demo: () => parseWorkPlan({ ...FX_WORK_PLAN, kind }) });
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
  const choose = (k: Kind) => { setKind(k); setPicked(""); setChoosing(false); };
  const start = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true); setSaid({ text: "" });
    try {
      const extra = { ...(startAnyway.length ? { start_anyway: startAnyway } : {}), ...(picked ? { workflow_key: picked } : {}) };
      let r = await startWork(ctx.projectId, kind, t, extra);
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
        setSaid({ text: r.demo ? "Sample mode — nothing was sent." : `Started — ${plan.data?.nodes[0]?.agent?.name ?? "Helm"} is planning it.`, tone: "green" });
        const id = typeof r.body?.run_id === "string" ? r.body.run_id : "";
        if (id) onStarted(id);
        ctx.refreshWork();
      } else setSaid({ text: r.error ?? "It didn't start.", tone: "red" });
    } finally { setBusy(false); }
  };
  const k = KINDS.find((x) => x.key === kind)!;
  const p = plan.data;
  const nodes = p?.nodes.length ? p.nodes : TYPICAL;
  const est = p?.estimate ?? null;
  const wfName = p?.workflow ? `${p.workflow.name.replace(/\s+workflow$/i, "")} workflow` : plan.state === "loading" ? "Reading the plan…" : "Minor Feature workflow";
  return (
    <section className="px-card" aria-label="New work" data-testid="px-composer" style={{ borderColor: "var(--accent-line)" }}>
      <form className="px-row px-wrap" onSubmit={(e) => { e.preventDefault(); void start(); }}>
        <Ico icon={Plus} c="teal" />
        <div className="px-seg" role="group" aria-label="Kind of work">
          {KINDS.map((x) => (
            <button key={x.key} type="button" aria-pressed={kind === x.key} onClick={() => choose(x.key)} data-testid={`px-compose-${x.key}`}>
              <x.icon size={13} aria-hidden /> {x.label}</button>
          ))}
        </div>
        <label className="px-sr" htmlFor="px-compose-text">Describe the work</label>
        <input id="px-compose-text" ref={input} className="px-input" style={{ flex: "1 1 260px", width: "auto" }} value={text} placeholder={k.ph}
               inputMode={kind === "ticket" ? "url" : undefined}
               onChange={(e) => setText(e.target.value)} data-testid="px-compose-text" maxLength={2000} />
        <GatedButton permission="workflow_run:start" projectId={ctx.projectId} type="submit" className="px-btn" data-kind="primary" data-testid="px-compose-start"
                     disabled={!text.trim() || busy || (!!refusal && unaccepted(refusal, startAnyway).length > 0)}>
          <Play size={12} weight="fill" aria-hidden /> {busy ? "Starting…" : refusal ? "Start anyway" : "Start"}</GatedButton>
      </form>
      <div className="px-row px-wrap px-plan" style={{ marginTop: 10 }} data-testid="px-compose-plan" data-state={p ? "ready" : plan.state}>
        <span className="px-lbl">{p?.planner?.name ?? "Helm"}&apos;s plan</span>
        <Flow testid="px-compose-strip" nodes={nodes.map((n) => ({
          label: n.label, type: n.type === "you" ? "waiting" : n.type === "agent" ? "agent" : "todo",
          icon: n.type === "agent" && n.agent ? <Face agent={n.agent} size={16} /> : NODE_ICON[n.key],
        }))} />
        {est?.minutes ? (
          <span className="px-tag" data-tone="cost" data-testid="px-compose-est-min"
                title={est.minutes_basis === "history" ? `Median agent time to the pull request over the last ${est.minutes_runs} run${est.minutes_runs === 1 ? "" : "s"} of this workflow here` : "The steps' typical time — no run of this workflow here yet"}>
            ≈ {est.minutes} min</span>
        ) : null}
        {est && est.usd !== null ? (
          <span className="px-tag" data-tone="cost" data-testid="px-compose-est-usd"
                title={`Median cost of the last ${est.usd_runs} run${est.usd_runs === 1 ? "" : "s"} of this workflow here`}>≈ {dollarsAbout(est.usd)}</span>
        ) : est ? <span className="px-faint" style={{ fontSize: ".58rem" }} data-testid="px-compose-est-usd-none" title="BOSS prices a workflow from its own runs in this project">cost after its first run</span> : null}
        <span className="px-faint px-wf-pick" style={{ fontSize: ".6rem" }} data-testid="px-compose-workflow">
          <span title={p?.workflow?.why ? `Start runs ${p.workflow.name}: ${p.workflow.why}` : undefined}>{wfName}</span>
          {" · "}
          <button type="button" className="px-linkbtn" aria-haspopup="listbox" aria-expanded={choosing} data-testid="px-compose-change"
                  disabled={!p?.options.length} onClick={() => setChoosing((v) => !v)}>change</button>
          {choosing && p ? (
            <span className="px-wf-menu" role="listbox" aria-label="Workflow for this work" data-testid="px-compose-workflows"
                  onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setChoosing(false); } }}>
              {p.options.map((o) => (
                <button key={o.key} type="button" role="option" aria-selected={p.workflow?.key === o.key} data-testid={`px-compose-wf-${o.key}`}
                        data-fits={o.fits || undefined} title={o.detail || undefined}
                        onClick={() => { setPicked(o.key); setChoosing(false); }}>
                  {p.workflow?.key === o.key ? <Check size={11} weight="bold" aria-hidden /> : <span style={{ width: 11 }} aria-hidden />}
                  <span>{o.name}</span>{o.fits ? <span className="px-tag" data-tone="green">fits a {k.label.toLowerCase()}</span> : null}
                </button>
              ))}
              {picked ? <button type="button" role="option" aria-selected={false} data-testid="px-compose-wf-default" onClick={() => { setPicked(""); setChoosing(false); }}>
                <span style={{ width: 11 }} aria-hidden /><span>Back to the project&apos;s default</span></button> : null}
            </span>
          ) : null}
        </span>
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

type Rank = { up: (() => void) | null; down: (() => void) | null; position: number; total: number };

export function WorkRow({ w, ctx, fresh = false, rank = null }: { w: WorkItem; ctx: PxCtx; fresh?: boolean; rank?: Rank | null }) {
  const [said, setSaid] = useState("");
  const [open, setOpen] = useState(false);
  const setup = w.kind === "setup" && w.source === "onboarding";
  const startNow = async () => {
    // A planned item starts as itself, through the body BOSS wrote for it (its title, the item it
    // came from and the issue it names); an older engine's row, from its words.
    const r = w.start ? await runAccept(ctx.projectId, w.start, "Start now")
      : await startWork(ctx.projectId, w.kind === "bug" ? "bug" : "feature", w.detail && w.detail !== w.label ? `${w.label}\n\n${w.detail}` : w.label,
                        { title: w.label, ...(w.source === "roadmap" ? { source_ref: `rec:${w.id}` } : {}) });
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Started.") : (r.error ?? "It didn't start."));
    if (r.ok && !r.demo) ctx.refreshWork();
  };
  const pill = attemptsPill(w);
  const grouped = w.attempts.length > 1;
  const regionId = `px-attempts-${w.id}`;
  // Never a raw internal string ("code_task"): the source's human word, or the source as given.
  const source = w.source === "roadmap" || setup ? "" : w.source_label || (/^[a-z]+_[a-z_]+$/.test(w.source) ? "" : w.source);
  const tag = rowTag(w);
  return (
    <>
      <div className="px-wk" data-s={w.state} data-open={open || undefined} data-fresh={fresh || undefined} data-testid={`px-work-${w.id}`}
           tabIndex={rank ? 0 : undefined} aria-label={rank ? `${w.label}, ${rank.position} of ${rank.total} in the rank. Alt+Up or Alt+Down moves it.` : undefined}
           onClick={(e) => { if (!(e.target as HTMLElement).closest("a,button")) openItem(ctx, w); }}>
        <RowTile w={w} />
        <span style={{ minWidth: 0 }}>
          <span className="px-row" style={{ gap: 6, flexWrap: "nowrap", minWidth: 0 }}>
            <button type="button" className="px-wk-t px-ell" style={{ display: "block", minWidth: 0 }} onClick={() => openItem(ctx, w)} data-testid={`px-work-open-${w.id}`}>{w.label}</button>
            {grouped ? (
              <button type="button" className="px-attempts-pill" aria-expanded={open} aria-controls={regionId} data-testid={`px-work-attempts-${w.id}`}
                      title={`${w.attempt_count} attempts: ${w.followups} follow-up${w.followups === 1 ? "" : "s"}, ${w.retries} retr${w.retries === 1 ? "y" : "ies"}`}
                      onClick={() => setOpen((v) => !v)}>
                {pill}<CaretDown size={10} weight="bold" className="px-chev" aria-hidden /></button>
            ) : null}
          </span>
          <span className="px-wk-sub">
            <span className="px-tag" data-tone={tag.tone} data-testid={`px-work-tag-${w.id}`}>{tag.label}</span>
            {w.issue ? (
              <a className="px-chip" href={w.issue.external_url} target="_blank" rel="noreferrer noopener" data-testid={`px-work-issue-${w.id}`}
                 title={w.issue.provider === "github" ? `${w.issue.kind === "pr" ? "Pull request" : "Issue"} ${w.issue.label} on GitHub` : `Issue ${w.issue.label}`}
                 onClick={(e) => e.stopPropagation()}>
                {w.issue.provider === "github" ? <GithubLogo size={11} aria-hidden /> : <Ticket size={11} aria-hidden />}{w.issue.label}</a>
            ) : null}
            {w.where ? <span className="px-mono px-ell" data-testid={`px-work-where-${w.id}`} title="The file its failing check names">{w.where}</span> : null}
            {source && w.source !== "code_task" && w.source !== "workflow_run" ? <span>{source}</span> : null}
            {w.state === "stalled" ? <StalledTag w={w} /> : null}
            {w.state === "needs_you" ? (setup && w.waiting.length ? <span>Waiting: {w.waiting[0].label}</span> : w.age ? <span>Waiting {w.age}</span> : null)
              : w.state === "running" ? <span>{whereNow(w)}</span>
              : w.state === "planned" ? (w.next_up ? <span data-testid={`px-work-next-${w.id}`}>Next up</span> : null)
              : w.state === "done" || w.state === "failed" ? <span>{doneDay(w)}</span>
              : w.state === "stalled" ? null : w.age ? <span>{w.age}</span> : null}
            {w.cost_usd !== null && (w.cost_usd > 0 || (w.state !== "planned" && !setup)) ? <span className="px-mono">${w.cost_usd.toFixed(2)}</span> : null}
          </span>
        </span>
        {w.flow.length ? (
          <span className="px-hide-phone" data-testid={`px-work-flow-${w.id}`}>
            <Flow nodes={w.flow.map((f) => ({ label: f.label, state: f.state, type: f.state === "skipped" ? "todo" : f.state }))} />
          </span>
        ) : <Stations stages={w.stages} />}
        <span className="px-faces">{w.agents.slice(0, 3).map((a, i) => <Face key={`${a.name}-${i}`} agent={a} size={20} state={w.state === "running" && i === w.agents.length - 1 ? "working" : "idle"} />)}</span>
        <span className="px-wk-links">
          {w.links.workflow_run ? <PxLink href={w.links.workflow_run} title="Workflow run" testid={`px-work-run-${w.id}`}><FlowArrow size={11} aria-hidden /> {w.run_number ? `Run #${w.run_number}` : "Run"}</PxLink> : null}
          {setup ? (
            <button type="button" className="px-chip" title="What the onboarding agent did, step by step" data-testid={`px-work-onb-${w.id}`}
                    onClick={() => writePxUrl({ tab: "onboarding", drawer: null })}><FlowArrow size={11} aria-hidden /> Onboarding run</button>
          ) : null}
          {w.links.build && w.state !== "done" ? <PxLink href={w.links.build} title="Build page in Code Tasks" testid={`px-work-build-${w.id}`}><Code size={11} aria-hidden /> Build</PxLink> : null}
          {w.links.build && (w.state === "running" || w.state === "stalled") ? <PxLink href={`${w.links.build}&view=logs`} title="The build's live log" testid={`px-work-log-${w.id}`}><TerminalWindow size={11} aria-hidden /> Live log</PxLink> : null}
          {w.links.pr ? <PxLink href={w.links.pr.external_url} title="Pull request on GitHub" testid={`px-work-pr-${w.id}`}>{w.state === "done" ? <GitMerge size={11} aria-hidden /> : <GitPullRequest size={11} aria-hidden />} PR #{w.links.pr.number}</PxLink> : null}
          {w.links.preview ? <PxLink href={w.links.preview} title="The running preview"><Globe size={11} aria-hidden /> Preview</PxLink> : null}
          {w.links.deploy ? <PxLink href={w.links.deploy} title="Where it is deployed"><Globe size={11} aria-hidden /> Staging</PxLink> : null}
        </span>
        <span className="px-row">
          {w.state === "needs_you" ? (
            <button type="button" className="px-btn" data-kind="amber" data-size="s" data-testid={`px-work-act-${w.id}`} onClick={() => openItem(ctx, w)}>
              {w.action_label || (w.links.pr ? "Merge" : "Decide")}</button>
          ) : w.state === "planned" ? (
            <>
              {rank ? (
                <span className="px-rank" role="group" aria-label={`Rank of ${w.label}`}>
                  <span className="px-rank-handle px-hide-phone" title="Drag to re-rank" aria-hidden><DotsSixVertical size={13} /></span>
                  <button type="button" aria-label={`Move ${w.label} up`} title="Move up (Alt+↑)" disabled={!rank.up} data-testid={`px-rank-up-${w.id}`}
                          onClick={() => rank.up?.()}><ArrowUp size={11} aria-hidden /></button>
                  <button type="button" aria-label={`Move ${w.label} down`} title="Move down (Alt+↓)" disabled={!rank.down} data-testid={`px-rank-down-${w.id}`}
                          onClick={() => rank.down?.()}><ArrowDown size={11} aria-hidden /></button>
                </span>
              ) : null}
              <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-work-start-${w.id}`}
                           onClick={() => void startNow()}><Play size={11} weight="fill" aria-hidden /> Start now</GatedButton>
            </>
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
