"use client";

/**
 * WORK (screen 5, contract §3/§14): the New work bar (it opens the New work modal, new-work.tsx:
 * Helm's understanding, the workflow's real steps, ≈ time and ≈ cost from this project's history,
 * the chips, Start or Add to Next) — then every task as a row: the station strip (the onboarding
 * row's own Discover → Ready → Backlog), who works it, its cost, its issue chip and every link,
 * grouped Needs you · Running · Planned · Done. Needs you is the header bar's own list (one
 * count); Planned is Marshal's rank, re-ranked by drag or Alt+↑/↓ and saved through the plan
 * route. A row opens its drawer in place. List or Board.
 */
import { changeWords, projectIsLocal } from "../../lib/repo-links";
import React, { useEffect, useMemo, useState } from "react";
import {
  ArrowDown, ArrowUp, Bug, Check, Compass, Database, DotsSixVertical, EnvelopeSimple, FlowArrow, GitMerge,
  GithubLogo, HandWaving, Kanban, Lightning, ListBullets, PauseCircle, Play,
  Ticket, Tray, TreeStructure, XCircle,
} from "@phosphor-icons/react";
import { WorkTreeView } from "./work-tree";
import { InboxView } from "./work-inbox";

import { GatedButton, useGate } from "../../components/gated";
import {
  KIND_WORDS, WORK_ORDER, WORK_WORDS, attemptsPill, movePlanItem, needsYouCards,
  pxHref, reRank, runAccept, startWork, workWhen, writePxUrl,
  type NeedsYou, type WorkItem, type WorkState,
} from "../../lib/project-experience";
import { stampLocal, whenLocal } from "../../lib/local-time";
import { Empty, Face, Loading, Failed, EngineNotReady, PxLink, Said } from "./kit";
import { NewWorkBar } from "./new-work";
import { RowChipsProvider, RowTicket, TicketWorkBand, useRowChips, useTicketConnections, useTicketsParam } from "./ticket-work";
import { TicketImportPage } from "./ticket-import";
import type { Connection } from "../../lib/tickets";
import type { PxCtx } from "./project-page";
import { WhyLine } from "../../components/stuck-work";
import { workNextStep } from "../../lib/work-next-step";
import { stateSentence } from "../../lib/work-panel";
import { WorkPreviewButton } from "./work-preview";
import "./work.css";
import "./work-panel.css";

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
const whereNow = (w: WorkItem, local = false) => {
  const now = w.flow.length ? w.flow.find((f) => f.state === "now") : w.stages.find((st) => st.state === "now") ?? w.stages.find((st) => st.state === "todo");
  // A local project's PR station is its change set (BOSSAPP-946).
  const name = !now ? "" : w.flow.length ? now.label : now.key === "pr" ? changeWords(local).station : STATION_NAME[now.key] ?? now.label;
  return [name, w.age].filter(Boolean).join(" · ");
};
/** A finished item's time: the one basis every surface uses (workWhen). */
const doneDay = (w: WorkItem) => workWhen(w);

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
  const [view, setView] = useState<"list" | "board" | "tree" | "mine">("list");
  // The row a Start just made (the New work modal's): it slides in (150 ms) once the list has it.
  const fresh = ctx.freshWork ?? "";
  // Imported tickets not started yet are drawn once, as T9's "Ready to start" cards (ticket-work).
  const raw = useMemo(() => (ctx.work?.items ?? []).filter((w) => w.origin !== "ticket_import"), [ctx.work]);
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
  // TICKETS (BOSSAPP-1047 T5/T8/T9): the project's ticket systems, the Import page, row chips.
  const tickets = useTicketConnections(ctx.projectId, ctx.demo);
  const ticketsParam = useTicketsParam();
  const chips = useRowChips(items, tickets.read?.ok ? tickets.read.data : NO_CONNECTIONS);
  if (ticketsParam === "import") return <TicketImportPage ctx={ctx} conns={tickets.read} />;
  return (
    <RowChipsProvider chips={chips}>
    <div className="px-stack" data-testid="px-work">
      <NewWorkBar ctx={ctx} said={ctx.workSaid} />
      <TicketWorkBand ctx={ctx} conns={tickets.read} />
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
        <div className="px-seg px-hide-phone" role="group" aria-label="View">
          <button type="button" aria-pressed={view === "list"} onClick={() => setView("list")} data-testid="px-work-list"><ListBullets size={13} /> List</button>
          <button type="button" aria-pressed={view === "board"} onClick={() => setView("board")} data-testid="px-work-board"><Kanban size={13} /> Board</button>
          <button type="button" aria-pressed={view === "tree"} onClick={() => setView("tree")} data-testid="px-work-tree"
                  title="Every work item as one tree, with what blocks what and its review stages"><TreeStructure size={13} /> Tree</button>
          <button type="button" aria-pressed={view === "mine"} onClick={() => setView("mine")} data-testid="px-work-mine"
                  title="What waits on you, first what blocks the most"><Tray size={13} /> Mine</button>
        </div>
      </div>
      {view === "tree" ? (
        <WorkTreeView projectId={ctx.projectId} demo={ctx.demo} onOpenListed={(id) => ctx.open("work", id)}
          listed={items.map((w) => ({
            id: w.id, title: w.title, label: w.label, state: w.state, source: w.source,
            source_label: w.source_label, work_item_id: w.work_item_id,
          }))} />
      ) : view === "mine" ? (
        <InboxView projectId={ctx.projectId} demo={ctx.demo} />
      ) : !ctx.work ? (
        ctx.workState === "loading" ? <Loading rows={3} label="Loading work" />
          : ctx.workState === "error" ? <Failed what="Work" error={null} onRetry={ctx.refreshWork} />
          : <EngineNotReady what="The work list"><a className="px-btn" href={pxHref("?area=code_tasks")}>Open Code Tasks</a></EngineNotReady>
      ) : !items.length && !needs.loose.length ? (
        <div className="px-card"><Empty icon={Kanban} title="No work yet" line="Press New work above (or N) to describe a feature or a bug. Helm plans it, you approve, the crew builds it to a PR." testid="px-work-empty" /></div>
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
                  <b style={{ fontSize: ".66rem" }}>{w.label}</b><RowDots w={w} local={projectIsLocal(ctx.overview?.project?.repos)} testid={`px-board-flow-${w.id}`} />
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
    </RowChipsProvider>
  );
}

const NO_CONNECTIONS: Connection[] = [];

/** Opening a row: the work drawer (05b) in place; the onboarding row opens the onboarding page (screen 10). */
function openItem(ctx: PxCtx, w: WorkItem) {
  if (w.kind === "setup" && w.source === "onboarding") writePxUrl({ tab: "onboarding", drawer: null });
  // The row lands on the Work panel's whole page (the drawer's short preview is for other doors).
  else ctx.open("work", w.id, { wp: "full" });
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

/** The row's compact flow, one dot per node (the panel's "Who did what" in miniature): code work's
 *  Spec · Build · Review & tests · Merge · Staging; a workflow run that changes no app, its own
 *  steps (engine work_outcome.step_flow); the onboarding row's Discover → Ready → Backlog. */
function rowFlow(w: WorkItem, local = false): { key: string; name: string; s: string }[] {
  const S: Record<string, string> = { done: "done", now: "live", waiting: "wait", failed: "failed", skipped: "skipped", todo: "todo" };
  if (w.flow.length) return w.flow.map((f) => ({ key: f.key, name: f.label, s: S[f.state] ?? "todo" }));
  const st = (k: string) => w.stages.find((x) => x.key === k)?.state;
  const plan = st("plan"); const design = st("design");
  const spec = design === "waiting" ? "waiting" : [plan, design].includes("failed") ? "failed" : [plan, design].includes("now") ? "now"
    : [plan, design].includes("done") ? "done" : [plan, design].every((x) => !x || x === "skipped") ? "skipped" : "todo";
  const merge = w.state === "needs_you" && (w.links.merge_pr || w.action_label === "Merge") ? "waiting" : st("pr") ?? "todo";
  const review = w.links.merge_pr?.fixing ? "now" : st("review") ?? "todo";
  return [
    { key: "spec", name: "Spec", s: S[spec] }, { key: "build", name: "Build", s: S[st("build") ?? "todo"] },
    { key: "review", name: "Review & tests", s: S[review] }, { key: "pr", name: local ? changeWords(local).station : "Merge", s: S[merge] },
    { key: "ship", name: "Staging", s: S[st("ship") ?? "todo"] },
  ];
}
const FLOW_WORD: Record<string, string> = { done: "done", live: "in progress", wait: "waiting on you", failed: "failed", skipped: "skipped", todo: "next" };

/** The dots themselves (the list row and the board card): no letters, the names on hover. */
function RowDots({ w, local = false, testid = `px-work-flow-${w.id}` }: { w: WorkItem; local?: boolean; testid?: string }) {
  const flow = rowFlow(w, local);
  return (
    <span className="wr-flow" role="img" data-testid={testid}
          aria-label={flow.map((f) => `${f.name} ${FLOW_WORD[f.s]}`).join(", ")} title={flow.map((f) => `${f.name}: ${FLOW_WORD[f.s]}`).join("\n")}>
      {flow.map((f) => <i key={f.key} data-s={f.s} />)}
    </span>
  );
}

/** A WORK ROW (operator 2026-11-19: "the current one does not look good" — run/build/change chips,
 *  a letter-dot pipeline, truncated titles, two buttons). Now: the title (short), one line of
 *  state, a compact flow, and ONE action (+ the change's preview). Everything else — links,
 *  attempts, cost, faces — is in the Work panel the row opens. */
export function WorkRow({ w, ctx, fresh = false, rank = null }: { w: WorkItem; ctx: PxCtx; fresh?: boolean; rank?: Rank | null }) {
  const [said, setSaid] = useState("");
  const setup = w.kind === "setup" && w.source === "onboarding";
  const next = setup ? null : workNextStep(w);
  const local = projectIsLocal(ctx.overview?.project?.repos);
  const startNow = async () => {
    // A planned item starts as itself, through the body BOSS wrote for it (its title, the item it
    // came from and the issue it names); an older engine's row, from its words.
    const r = w.start ? await runAccept(ctx.projectId, w.start, "Start now")
      : await startWork(ctx.projectId, w.kind === "bug" ? "bug" : "feature", w.detail && w.detail !== w.label ? `${w.label}\n\n${w.detail}` : w.label,
                        { title: w.label, ...(w.source === "roadmap" ? { source_ref: `rec:${w.id}` } : {}) });
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : "Started.") : (r.error ?? "It didn't start."));
    if (r.ok && !r.demo) ctx.refreshWork();
  };
  const said1 = setup ? null : stateSentence(w, next);
  const tag = rowTag(w);
  return (
    <div className="px-wk wr" data-s={w.state} data-fresh={fresh || undefined} data-testid={`px-work-${w.id}`}
         tabIndex={rank ? 0 : undefined} aria-label={rank ? `${w.label}, ${rank.position} of ${rank.total} in the rank. Alt+Up or Alt+Down moves it.` : undefined}
         onClick={(e) => { if (!(e.target as HTMLElement).closest("a,button,input")) openItem(ctx, w); }}>
      <RowTile w={w} />
      <span className="wr-main">
        <span className="px-row" style={{ gap: 6, flexWrap: "nowrap", minWidth: 0 }}>
          <button type="button" className="wr-t" title={w.title && w.title !== w.label ? w.title : undefined} onClick={() => openItem(ctx, w)} data-testid={`px-work-open-${w.id}`}>{w.label}</button>
          {w.issue ? (
            <a className="px-chip" href={w.issue.external_url} target="_blank" rel="noreferrer noopener" data-testid={`px-work-issue-${w.id}`}
               title={w.issue.provider === "github" ? `${w.issue.kind === "pr" ? "Pull request" : "Issue"} ${w.issue.label} on GitHub` : `Issue ${w.issue.label}`}
               onClick={(e) => e.stopPropagation()}>
              {w.issue.provider === "github" ? <GithubLogo size={11} aria-hidden /> : <Ticket size={11} aria-hidden />}{w.issue.label}</a>
          ) : null}
          <RowTicket w={w} />
        </span>
        <span className="wr-line" data-testid={`px-work-line-${w.id}`}>
          {/* WAITING FOR WHAT, from the server (stuck_work.wait_reason), when the build is not working. */}
          {w.why && w.state !== "needs_you" ? <WhyLine why={w.why} testId={`px-work-why-${w.id}`} compact />
            : w.state === "stalled" ? <StalledTag w={w} />
            : setup ? <span><span className="px-tag" data-testid={`px-work-tag-${w.id}`} style={{ marginRight: 6 }}>{tag.label}</span>
                {w.state === "needs_you" && w.waiting.length ? `Waiting: ${w.waiting[0].label}` : w.detail}</span>
            : w.state === "planned" ? <span className="px-row" style={{ gap: 6, flexWrap: "nowrap" }}>
                <span className="px-tag" data-tone={tag.tone} data-testid={`px-work-tag-${w.id}`}>{tag.label}</span>
                {w.next_up ? <><span data-testid={`px-work-next-${w.id}`}>Next up</span>
                  {w.agents[0] ? <span className="px-faces" title={`${w.agents[0].name} plans it`}><Face agent={w.agents[0]} size={18} /></span> : null}</>
                  : w.where ? <span className="px-mono" data-testid={`px-work-where-${w.id}`}>{w.where}</span> : null}</span>
            : said1 ? <span><b data-tone={said1.tone}>{said1.lead}</b>{" "}
                {w.state === "running" ? whereNow(w, local) : w.state === "done" || w.state === "failed" ? [said1.rest, doneDay(w)].filter(Boolean).join(" · ") : said1.rest}</span> : null}
        </span>
      </span>
      <RowDots w={w} local={local} />
      <span className="wr-act">
        {w.state === "needs_you" && w.preview_app ? (
          <WorkPreviewButton app={w.preview_app} projectId={ctx.projectId} actor={ctx.actor} testid={`px-work-preview-${w.id}`} />
        ) : null}
        {w.state === "needs_you" ? (
          // ONE NEXT STEP (workNextStep): the same words the panel's header says; while BOSS still
          // fixes the change, Merge is offered but not as the amber call.
          <button type="button" className="px-btn" data-kind={next?.secondary ? undefined : "amber"} data-size="s" data-testid={`px-work-act-${w.id}`} onClick={() => openItem(ctx, w)}
                  title={next?.secondary ? next.words : next?.hint ? `${next.label} · ${next.hint}` : undefined}>
            {setup ? (w.action_label || "Decide") : next?.label ?? "Decide"}</button>
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
        ) : (w.state === "running" || w.state === "stalled") && w.links.log ? (
          // BOSS is working: the one thing to do is watch it (the build's own live log).
          <PxLink href={w.links.log} title="This build's live log" testid={`px-work-log-${w.id}`}>Watch live</PxLink>
        ) : setup ? (
          <button type="button" className="px-chip" title="What the onboarding agent did, step by step" data-testid={`px-work-onb-${w.id}`}
                  onClick={() => writePxUrl({ tab: "onboarding", drawer: null })}><FlowArrow size={11} aria-hidden /> Onboarding run</button>
        ) : null}
        <Said text={said} />
      </span>
    </div>
  );
}
