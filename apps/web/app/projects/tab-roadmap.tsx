"use client";

/**
 * ROADMAP (screen 07, contract §6): the PLAN as three lanes — Now (the work moving now), Next and
 * Later (roadmap items a person accepted, in Marshal's rank; drag or use ⇄ to move them, ▶ to start
 * one through THE start door) — then the ranked RECOMMENDATIONS, each a small diagram: evidence →
 * detector → the agent that would do it. Accept puts it on Next (planned work, nothing starts);
 * Reject asks a reason in one tap (it teaches the detector); Snooze sits in the card's ⋯ menu.
 * Reads GET /v1/projects/{id}/plan and the existing GET /v1/ambient/roadmap — stored items, so
 * it works with the ambient preview off.
 */
import React, { useState } from "react";
import {
  ArrowRight, ArrowsLeftRight, BellSlash, CaretRight, Compass, Detective, Gauge, GitPullRequest, Plug, Shuffle, TrendUp, ChartLineUp, ChatCircleDots, Chats, Check, Clock, Coins, DotsThree, Fire,
  HandPointing, Kanban, Lightbulb, MagnifyingGlass, MapTrifold, Play, Sparkle, TestTube, Timer, Warning, X, type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { parseRec, parseRoadmap, recPath, roadmapPath, type ElsewhereGroup, type Recommendation, type RejectCategory, type RoadmapView } from "../../lib/ambient";
import {
  PX_PATHS, acceptRecommendation, handRecommendation, movePlanItem, parsePlan, rejectRecommendation, runAccept, snoozeRecommendation, usePxRead,
  type Plan, type PlanCard, type PlanLane,
} from "../../lib/project-experience";
import { FX_ROADMAP } from "../../lib/project-experience-fixtures";
import { notifyChanged } from "../changed";
import { Drawer, Empty, Face, Flow, Ico, REC_KIND_WORDS, ReadGate, Said, Tag } from "./kit";
import type { PxCtx } from "./project-page";

/** The squad's role keys → the crew's names (the kit's faces). */
const CREW_NAME: Record<string, string> = {
  engineering_captain: "Helm", product_manager: "Juno", builder: "Forge", software_engineer: "Forge", qa: "Vera", sre: "Beacon",
  chief_architect: "Atlas", chief_of_staff: "Marshal", security: "Sentry",
};
/** What the agent would do, by the recommendation's kind and shape (the card's last node). */
const DOES: Record<string, string> = {
  hardening: "Test + fix", test_gap: "Add tests", refactor: "Refactor", design_fix: "Fix design", cost: "Cut cost", process: "Migration",
  feature: "Build it",
};
/** An evidence source's icon (the card's first nodes). */
const SOURCE_ICON: Record<string, Icon> = {
  incidents: Warning, "code graph": Fire, "live app": Plug, "review findings": MagnifyingGlass, "prd decisions": Chats, cost: Coins, "test runs": TestTube,
  "stage timings": Timer, overrides: HandPointing,
};
/** Each detector's icon (wireframe 07): the engine names it (`icon`), older engines by its name. */
const DETECTOR_ICON: Record<string, Icon> = {
  detective: Detective, "bell-slash": BellSlash, fire: Fire, "test-tube": TestTube, "magnifying-glass": MagnifyingGlass,
  compass: Compass, coins: Coins, shuffle: Shuffle, gauge: Gauge, "hand-pointing": HandPointing, "trend-up": TrendUp,
  "Incident hotspot": Detective, "Incident patterns": Detective, "Alert gap": BellSlash, "Feature demand": TrendUp,
  "Slow query": Gauge, "Code hotspot": Fire, "Test gap": TestTube,
  warning: Warning, chats: Chats, "git-pull-request": GitPullRequest, plug: Plug, timer: Timer, "chart-line-up": ChartLineUp,
};
const KIND_TAG = REC_KIND_WORDS;
/** The one-tap reject reasons the wireframe names → the engine's categories (each teaches its detector). */
export const REJECT_CHIPS: { label: string; category: RejectCategory }[] = [
  { label: "Not now", category: "not_valuable" }, { label: "Wrong idea", category: "wrong" },
  { label: "Duplicate", category: "duplicate" }, { label: "Won't do", category: "other" },
];
/** Recommendation filters, in words (never a raw status). */
export const STATUS_WORDS: { key: string; label: string }[] = [
  { key: "open", label: "Open" }, { key: "accepted", label: "Accepted" }, { key: "rejected", label: "Rejected" },
  { key: "snoozed", label: "Snoozed" }, { key: "resolved_by_change", label: "Fixed by a change" },
];

export function RoadmapTab({ ctx }: { ctx: PxCtx }) {
  const plan = usePxRead<Plan>(PX_PATHS.plan(ctx.projectId), parsePlan, { everyMs: 30_000, demo: () => fallbackPlan(ctx) });
  const [status, setStatus] = useState("open");
  const read = usePxRead<RoadmapView>(roadmapPath(ctx.projectId, status), parseRoadmap, { everyMs: 30_000, demo: () => parseRoadmap(FX_ROADMAP) });
  // The fallback (Now from Work, planned on Next) only for an engine without the plan read —
  // never while the read is still on its way, or the lanes would jump.
  const lanes = plan.data ?? (plan.state === "loading" ? null : fallbackPlan(ctx));
  const changed = () => { plan.refresh(); read.refresh(); ctx.refreshWork(); notifyChanged("project.roadmap"); };
  return (
    <div className="px-stack" data-testid="px-roadmap">
      {lanes ? <PlanCard ctx={ctx} plan={lanes} onChanged={changed} />
        : <div className="px-skel" style={{ minHeight: 210 }} role="status" aria-label="Loading the plan" data-testid="px-rm-plan-loading" />}
      <ReadGate read={read} what="Recommendations" loadingRows={2}>
        {(rm) => <Recommendations ctx={ctx} rm={rm} status={status} onStatus={setStatus} onChanged={changed} />}
      </ReadGate>
    </div>
  );
}

/** An engine without the plan read: Now from the work list, planned work on Next. */
function fallbackPlan(ctx: PxCtx): Plan {
  const items = ctx.work?.items ?? [];
  const card = (lane: PlanCard["lane"]) => (w: (typeof items)[number]): PlanCard => ({
    id: w.id, label: w.label, title: w.label, state: w.state, agent: w.agents[w.agents.length - 1] ?? null, href: w.href, lane,
    source: w.source, external_url: "", start: null,
  });
  const now = items.filter((w) => w.state === "running" || w.state === "needs_you" || w.state === "stalled").map(card("now"));
  const next = items.filter((w) => w.state === "planned").map(card("next"));
  return { now, next, later: [], counts: { now: now.length, next: next.length, later: 0 } };
}

function PlanCard({ ctx, plan, onChanged }: { ctx: PxCtx; plan: Plan; onChanged: () => void }) {
  const captain = ctx.overview.captain?.name ?? "Helm";
  const [said, setSaid] = useState("");
  const [dragging, setDragging] = useState<string | null>(null);
  const move = async (id: string, lane: PlanLane, before?: string) => {
    const r = await movePlanItem(ctx.projectId, id, lane, before);
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : `Moved to ${lane === "next" ? "Next" : "Later"}.`) : (r.error ?? "It didn't move."));
    if (r.ok && !r.demo) onChanged();
  };
  const start = async (c: PlanCard) => {
    if (!c.start) return;
    const r = await runAccept(ctx.projectId, c.start, "Start");
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : `${c.label} started — it's on Work.`) : (r.error ?? "It didn't start."));
    if (r.ok && !r.demo) onChanged();
  };
  const cols: { key: "now" | PlanLane; label: string; rows: PlanCard[] }[] = [
    { key: "now", label: "Now", rows: plan.now }, { key: "next", label: "Next", rows: plan.next }, { key: "later", label: "Later", rows: plan.later },
  ];
  return (
    <section className="px-card" aria-labelledby="px-rm-plan" data-testid="px-rm-plan">
      <div className="px-card-h"><Ico icon={MapTrifold} c="violet" /><h2 id="px-rm-plan">Plan</h2>
        <small className="px-faint px-hide-phone">{captain} proposes · Marshal ranks · you decide</small>
        <a className="px-more" href="#" data-testid="px-rm-plan-ask" onClick={(e) => { e.preventDefault(); ctx.ask("Plan the next two weeks for this project."); }}>
          <ChatCircleDots size={12} /> Plan with {captain}</a></div>
      <div className="px-rm-cols">
        {cols.map((c) => (
          <div key={c.key} className="px-rm-lane" data-testid={`px-rm-${c.key}`} data-drop={dragging && c.key !== "now" ? "" : undefined}
               onDragOver={c.key !== "now" ? (e) => { if (dragging) e.preventDefault(); } : undefined}
               onDrop={c.key !== "now" ? (e) => { e.preventDefault(); const id = dragging; setDragging(null); if (id) void move(id, c.key as PlanLane); } : undefined}>
            <div className="px-row px-rm-lane-h"><span className="px-dot" data-lane={c.key} aria-hidden /><b>{c.label}</b>
              <span className="px-nbadge">{c.rows.length}</span></div>
            {c.rows.map((w) => (
              <PlanRow key={w.id} ctx={ctx} w={w} lane={c.key}
                       onDrag={(on) => setDragging(on ? w.id : null)}
                       onDropBefore={c.key !== "now" ? (id) => { setDragging(null); if (id !== w.id) void move(id, c.key as PlanLane, w.id); } : undefined}
                       onMove={c.key !== "now" ? () => void move(w.id, c.key === "next" ? "later" : "next") : undefined}
                       onStart={c.key !== "now" && w.start ? () => void start(w) : undefined} dragging={dragging} />
            ))}
            {!c.rows.length ? (
              <div className="px-rm-empty" data-testid={`px-rm-${c.key}-empty`}>
                {c.key === "now" ? <span>Nothing running. Start one from Next.</span>
                  : <><span>{dragging ? "Drop it here" : "Nothing here yet"}</span>
                      <a href="#" onClick={(e) => { e.preventDefault(); ctx.ask(`Plan what goes ${c.key === "next" ? "next" : "later"} for this project.`); }}
                         data-testid={`px-rm-${c.key}-ask`}><ChatCircleDots size={11} /> Ask {captain} to plan</a>
                      <small className="px-faint">or drag an item here</small></>}
              </div>
            ) : null}
          </div>
        ))}
      </div>
      <Said text={said} tone={said && !said.includes("didn't") ? "green" : "red"} />
    </section>
  );
}

function PlanRow({ ctx, w, lane, onDrag, onDropBefore, onMove, onStart, dragging }: {
  ctx: PxCtx; w: PlanCard; lane: "now" | PlanLane; dragging: string | null;
  onDrag: (on: boolean) => void; onDropBefore?: (id: string) => void; onMove?: () => void; onStart?: () => void;
}) {
  const needs = w.state === "needs_you";
  const running = w.state === "running";
  const open = () => (lane === "now" ? ctx.open("work", w.id) : ctx.open("rec", w.id));
  return (
    <div className="px-rm-row" data-s={w.state} data-testid={`px-rm-item-${w.id}`} draggable={lane !== "now"}
         onDragStart={lane !== "now" ? (e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", w.id); onDrag(true); } : undefined}
         onDragEnd={() => onDrag(false)}
         onDragOver={onDropBefore ? (e) => { if (dragging) e.preventDefault(); } : undefined}
         onDrop={onDropBefore ? (e) => { e.preventDefault(); e.stopPropagation(); if (dragging) onDropBefore(dragging); } : undefined}>
      <span className="px-rm-glyph" data-s={needs ? "you" : running ? "now" : w.state === "stalled" ? "stalled" : "todo"} role="img"
            aria-label={needs ? "needs you" : running ? "running" : w.state === "stalled" ? "stalled" : "planned"}>{needs ? "!" : null}</span>
      <button type="button" className="px-rm-t px-ell" onClick={open} title={w.title}>{w.label}</button>
      {lane === "now" ? (
        needs ? <Face agent={{ name: "You" }} size={20} /> : w.agent ? <Face agent={w.agent} size={20} state={running ? "working" : "idle"} /> : <span />
      ) : (
        <span className="px-rm-acts">
          {onStart ? <GatedButton permission="workflow_run:start" projectId={ctx.projectId} className="px-icon-btn" data-testid={`px-rm-start-${w.id}`}
                                  aria-label={`Start ${w.label}`} title="Start it now (a build starts)" onClick={onStart}><Play size={11} weight="fill" /></GatedButton> : null}
          {onMove ? <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-icon-btn" data-testid={`px-rm-move-${w.id}`}
                                 aria-label={lane === "next" ? `Move ${w.label} to Later` : `Move ${w.label} to Next`}
                                 title={lane === "next" ? "Move to Later" : "Move to Next"} onClick={onMove}><ArrowsLeftRight size={12} /></GatedButton> : null}
        </span>
      )}
    </div>
  );
}

function Recommendations({ ctx, rm, status, onStatus, onChanged }: {
  ctx: PxCtx; rm: RoadmapView; status: string; onStatus: (s: string) => void; onChanged: () => void;
}) {
  const [done, setDone] = useState<Record<string, "accepted" | "rejected" | "snoozed">>({});
  // A card decided here STAYS as its outcome (wireframe 07: the green "Accepted → On Work · Next"
  // card) when the list re-reads and it has left the Open filter (tester 07-17/18).
  const [kept, setKept] = useState<Record<string, Recommendation>>({});
  const listed = new Set(rm.recommendations.map((r) => r.id));
  const recs = [...rm.recommendations, ...Object.values(kept).filter((r) => !listed.has(r.id))];
  const captain = ctx.overview.captain?.name ?? "Helm";
  return (
    <section aria-labelledby="px-rm-recs" className="px-stack" style={{ gap: 10 }} data-testid="px-rm-recs">
      <div className="px-row px-wrap">
        <Ico icon={Lightbulb} c="violet" /><h2 id="px-rm-recs" className="px-rm-h">Recommendations</h2>
        <Tag tone="violet">{Math.max(0, (rm.counts.open ?? 0) - (status === "open" ? (rm.elsewhere ?? []).reduce((t, g) => t + g.count, 0) : 0))} new</Tag>
        <span className="px-spacer" />
        <span className="px-filters" role="group" aria-label="Show recommendations" style={{ margin: 0 }}>
          {STATUS_WORDS.filter((f) => f.key !== "resolved_by_change" || (rm.counts[f.key] ?? 0) > 0).map((f) => (
            <button key={f.key} type="button" className="px-filter" aria-pressed={status === f.key} data-testid={`px-rm-filter-${f.key}`}
                    onClick={() => onStatus(f.key)}>{f.label} {rm.counts[f.key] ?? 0}</button>
          ))}
        </span>
      </div>
      {recs.length ? recs.map((r, i) => (
        <RecCard key={r.id} ctx={ctx} r={r} i={i} state={done[r.id] ?? (r.status === "accepted" ? "accepted" : undefined)}
                 onDone={(s) => { setDone((d) => ({ ...d, [r.id]: s })); setKept((k) => ({ ...k, [r.id]: r })); onChanged(); }} />
      )) : (
        <div className="px-card"><Empty icon={Lightbulb} tone="violet" title={status === "open" ? "No ideas yet" : "Nothing here"} testid="px-rm-empty"
               line={status === "open" ? `${captain} proposes work from incidents, hot code, test gaps and your PRDs.` : "No recommendation is in this state."}>
          <button type="button" className="px-btn" onClick={() => ctx.ask("What should we work on next?")}><ChatCircleDots size={13} /> Plan with {captain}</button></Empty></div>
      )}
      {rm.elsewhere?.length ? <Elsewhere ctx={ctx} groups={rm.elsewhere} onChanged={onChanged} /> : null}
    </section>
  );
}

/** OTHER APPS IN THIS REPOSITORY (BOSSAPP-931): their recommendations, collapsed, grouped by app,
 *  each group with "Hand to <that project>" when a project owns the app. This project's own items
 *  stay above; a repository with one app never shows this. */
export function Elsewhere({ ctx, groups, onChanged }: { ctx: PxCtx; groups: ElsewhereGroup[]; onChanged: () => void }) {
  const total = groups.reduce((t, g) => t + g.count, 0);
  const [said, setSaid] = useState("");
  const [gone, setGone] = useState<Record<string, boolean>>({});
  const hand = async (g: ElsewhereGroup) => {
    if (!g.handTo) return;
    let moved = 0;
    let error = "";
    for (const r of g.items) {
      const res = await handRecommendation(ctx.projectId, r.id, g.handTo.projectId);
      if (res.ok) moved += 1; else error = res.error ?? "It didn't go through.";
    }
    setSaid(error || `${moved} handed to ${g.project?.name ?? "its project"}.`);
    if (moved) { setGone((x) => ({ ...x, [g.app]: true })); onChanged(); }
  };
  return (
    <details className="px-card px-rm-elsewhere" data-testid="px-rm-elsewhere">
      <summary className="px-row" style={{ cursor: "pointer" }}>
        <CaretRight size={12} aria-hidden /><b>Elsewhere in this repo · {total}</b>
        <small className="px-faint px-hide-phone">other apps' ideas, kept apart from this project's</small>
      </summary>
      <div className="px-stack" style={{ gap: 10, marginTop: 8 }}>
        {groups.filter((g) => !gone[g.app]).map((g) => (
          <div key={g.app} className="px-stack" style={{ gap: 6 }} data-testid={`px-rm-elsewhere-${g.app}`}>
            <div className="px-row px-wrap">
              <span className="px-lbl">{g.project ? g.project.name : g.app}</span>
              {g.project && g.app !== g.project.name ? <small className="px-faint px-mono">{g.app}</small> : null}
              <Tag tone="idle">{g.count}</Tag>
              <span className="px-spacer" />
              {g.handTo ? (
                <button type="button" className="px-chip" data-testid={`px-rm-hand-${g.app}`} onClick={() => void hand(g)}>
                  <ArrowRight size={11} aria-hidden /> {g.handTo.label}</button>
              ) : null}
            </div>
            {g.items.map((r) => (
              <div key={r.id} className="px-rc" data-s="elsewhere" data-testid={`px-rec-elsewhere-${r.id}`}>
                <span className="px-rank"><MapTrifold size={12} aria-hidden /></span>
                <div style={{ minWidth: 0 }}><div className="px-rc-t">{r.title}</div>
                  {r.scope?.path ? <small className="px-faint px-mono">{r.scope.path}</small> : null}</div>
              </div>
            ))}
          </div>
        ))}
        <Said text={said} />
      </div>
    </details>
  );
}

/** A card's diagram (wireframe 07): the recommendation's CONCRETE evidence ("2 incidents", "sla.py
 *  hot") → the detector that read it ("Incident hotspot") → the agent who would do it ("Test + fix").
 *  The engine builds it from the record (`card`); never the provenance chain (Code graph → Roadmap
 *  → Onboarding), which read the same on every card — that stays in Why ›. */
export function recFlow(r: Recommendation, squad: { name: string; role_key?: string }[] = []) {
  const role = r.squad?.[0] ?? "";
  const who = squad.find((m) => m.role_key === role)?.name ?? CREW_NAME[role] ?? "Helm";
  if (r.card) {
    const c = r.card;
    const DI = c.detector ? DETECTOR_ICON[c.detector.icon] ?? DETECTOR_ICON[c.detector.label] ?? MagnifyingGlass : MagnifyingGlass;
    return [
      ...c.evidence.map((e) => { const I = DETECTOR_ICON[e.icon] ?? ChartLineUp; return { label: e.label, type: "signal", icon: <I size={11} aria-hidden /> }; }),
      ...(c.detector ? [{ label: c.detector.label, type: "decision", icon: <DI size={11} aria-hidden /> }] : []),
      { label: c.does || "Do it", type: "agent", icon: <Face agent={{ name: who, role_key: role || "engineering_captain" }} size={16} />, title: `${who} would do it` },
    ];
  }
  const signals = r.strip.filter((n) => n.type === "signal").slice(0, 2).map((n) => {
    const count = Number((n.badges.find((b) => b.startsWith("×")) ?? "").slice(1)) || 0;
    const I = SOURCE_ICON[n.label.toLowerCase()] ?? ChartLineUp;
    // One code signal names the file ("sla.mjs hot"), like the wireframe's evidence chip.
    const file = r.subject?.startsWith("file:") ? r.subject.split("/").pop() : "";
    const label = n.label === "Code graph" && count <= 1 && file ? `${file} hot` : count > 1 ? `${count} ${n.label.toLowerCase()}` : n.label;
    return { label, type: "signal", icon: <I size={11} aria-hidden /> };
  });
  const found = r.strip.find((n) => n.type === "agent");
  // A detector's own name (wireframe 07: "Incident hotspot", "Alert gap"…); a raw key (an engine
  // without its registry) reads as words, never `snake_case`.
  const detector = found && found.label !== "seed" ? { ...found, label: found.label.includes("_") ? found.label.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : found.label } : null;
  const DI = DETECTOR_ICON[detector?.icon ?? ""] ?? DETECTOR_ICON[detector?.label ?? ""] ?? MagnifyingGlass;
  const does = detector?.icon === "bell-slash" || detector?.label === "Alert gap" ? "Add alert"
    : r.proposed_shape === "prd" ? "Write PRD" : DOES[r.kind] ?? "Do it";
  return [
    ...signals,
    ...(detector ? [{ label: detector.label, type: "decision", icon: <DI size={11} aria-hidden /> }] : []),
    { label: does, type: "agent", icon: <Face agent={{ name: who, role_key: role || "engineering_captain" }} size={16} /> },
  ];
}

/** OPEN WORK OPENS THAT ITEM (07-18): the run it became, else its planned row on Work (the
 *  rec's own id), else the Work row of the same name — in the Work tab's drawer; the Work tab
 *  alone only when none is on the page yet. */
export function acceptedWorkId(items: { id: string; label: string; title?: string }[], r: { id: string; title: string; work_id?: string }): string | null {
  const key = (v: string | undefined) => (v ?? "").toLowerCase().replace(/\s+/g, " ").trim();
  const ids = [r.work_id, r.id].filter(Boolean) as string[];
  const hit = items.find((w) => ids.includes(w.id))
    ?? items.find((w) => key(w.title) === key(r.title) || key(w.label) === key(r.title));
  return hit ? hit.id : r.work_id ?? null;
}

function openAccepted(ctx: PxCtx, r: Recommendation) {
  const id = acceptedWorkId(ctx.work?.items ?? [], r);
  ctx.tab("work", id ? { drawer: `work:${id}` } : {});
}

function RecCard({ ctx, r, i, state, onDone }: { ctx: PxCtx; r: Recommendation; i: number; state?: "accepted" | "rejected" | "snoozed"; onDone: (s: "accepted" | "rejected" | "snoozed") => void }) {
  const [rejecting, setRejecting] = useState(false);
  // No reason is preselected (tester 07-14): a person picks one, or says one, before Reject.
  const [chip, setChip] = useState<string>("");
  const [snoozing, setSnoozing] = useState(false);
  const [days, setDays] = useState(14);
  const [snoozeWhy, setSnoozeWhy] = useState("");
  const until = new Date(Date.now() + days * 86_400_000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const [reason, setReason] = useState("");
  const [said, setSaid] = useState("");
  const [menu, setMenu] = useState(false);
  const [why, setWhy] = useState(false);
  const act = async (fn: () => Promise<{ ok: boolean; error: string | null; demo?: boolean }>, s: "accepted" | "rejected" | "snoozed") => {
    const res = await fn();
    if (res.ok) { setSaid(res.demo ? "Sample mode — nothing was sent." : ""); onDone(s); setRejecting(false); setSnoozing(false); setMenu(false); }
    else setSaid(res.error ?? "It didn't go through.");
  };
  if (state === "accepted") {
    return (
      <div className="px-rc" data-s="accepted" data-testid={`px-rec-${r.id}`}>
        <span className="px-rank" data-s="accepted"><Check size={14} weight="bold" /></span>
        <div style={{ minWidth: 0 }}><div className="px-rc-t">{r.title}</div>
          <Flow nodes={[{ label: "Accepted", state: "done" }, { label: "On Work · Next", type: "todo", icon: <Kanban size={11} aria-hidden /> }]} /><Said text={said} /></div>
        <a className="px-chip" href="#" data-testid={`px-rec-open-${r.id}`} onClick={(e) => { e.preventDefault(); openAccepted(ctx, r); }}>Open work <ArrowRight size={11} /></a>
      </div>
    );
  }
  if (state) {
    return (
      <div className="px-rc" data-s={state} data-testid={`px-rec-${r.id}`}>
        <span className="px-rank">{state === "rejected" ? <X size={13} /> : <Clock size={13} />}</span>
        <div className="px-faint">{r.title} — {state === "rejected" ? "rejected; the detector learns from it" : `not now — back on ${until}`}<Said text={said} /></div><span />
      </div>
    );
  }
  const valuePct = r.value === "high" ? 82 : r.value === "medium" ? 60 : 30;
  const category = REJECT_CHIPS.find((c) => c.label === chip)?.category ?? "other";
  const hasReason = Boolean(chip || reason.trim());
  const reject = () => { if (hasReason) void act(() => rejectRecommendation(ctx.projectId, r.id, reason.trim() || chip, category), "rejected"); };
  const snooze = () => void act(() => snoozeRecommendation(ctx.projectId, r.id, days, snoozeWhy.trim()), "snoozed");
  return (
    <div className="px-rc" data-s={rejecting ? "rejecting" : snoozing ? "snoozing" : undefined} data-testid={`px-rec-${r.id}`}>
      <span className="px-rank">{r.rank ?? i + 1}</span>
      <div style={{ minWidth: 0 }}>
        <div className="px-rc-t">{r.title}{r.scope?.where === "repo" ? (
          <> <span data-testid={`px-rec-whole-repo-${r.id}`}><Tag tone="idle" title="About the whole repository, not one app">{r.scope.label || "Whole repo"}</Tag></span></>) : null}</div>
        <Flow nodes={recFlow(r, ctx.overview.captain?.squad ?? [])} testid={`px-rec-flow-${r.id}`} />
        {rejecting ? (
          <form className="px-stack" style={{ gap: 8, marginTop: 8 }} onSubmit={(e) => { e.preventDefault(); reject(); }}>
            <span className="px-lbl">Reject because</span>
            <div className="px-row px-wrap" style={{ gap: 6 }}>
              <div className="px-filters" role="radiogroup" aria-label="Reason" style={{ margin: 0 }}>
                {REJECT_CHIPS.map((c) => (
                  <button key={c.label} type="button" className="px-filter" role="radio" aria-checked={chip === c.label}
                          onClick={() => setChip(c.label)} data-testid={`px-rec-why-${c.category}`}>{c.label}</button>
                ))}
              </div>
              <input className="px-input" style={{ flex: "1 1 200px" }} placeholder="Say more (optional)" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Say more" />
            </div>
          </form>
        ) : snoozing ? (
          <form className="px-stack" style={{ gap: 8, marginTop: 8 }} data-testid={`px-rec-snooze-form-${r.id}`} onSubmit={(e) => { e.preventDefault(); snooze(); }}>
            <span className="px-lbl">Not now — back on</span>
            <div className="px-row px-wrap" style={{ gap: 6 }}>
              <div className="px-filters" role="radiogroup" aria-label="Snooze for" style={{ margin: 0 }}>
                {[7, 14, 30].map((d) => (
                  <button key={d} type="button" className="px-filter" role="radio" aria-checked={days === d} data-testid={`px-rec-snooze-${d}`}
                          onClick={() => setDays(d)}>{d === 7 ? "1 week" : d === 14 ? "2 weeks" : "1 month"}</button>
                ))}
              </div>
              <span className="px-mono px-faint" data-testid={`px-rec-snooze-until-${r.id}`}>{until}</span>
              <input className="px-input" style={{ flex: "1 1 200px" }} placeholder="Why not now? (optional)" value={snoozeWhy}
                     onChange={(e) => setSnoozeWhy(e.target.value)} aria-label="Why not now" />
            </div>
          </form>
        ) : (
          <div className="px-row px-wrap px-rc-meta">
            <Tag>{KIND_TAG[r.kind] ?? r.kindLabel}</Tag>
            <span className="px-score">Value <span className="px-bar" role="img" aria-label={`Value ${valuePct}%`}><span style={{ width: `${valuePct}%`, background: "var(--green)" }} /></span></span>
            <span className="px-score">Effort <b>{r.effort === "low" ? "S" : r.effort === "medium" ? "M" : "L"}</b></span>
            {r.cost_usd ? <span className="px-tag px-mono" data-testid={`px-rec-cost-${r.id}`}
                                title={r.card?.cost_basis ? `About $${r.cost_usd.toFixed(2)}: ${r.card.cost_basis}` : "This item's estimate"}>≈ ${r.cost_usd < 1 ? r.cost_usd.toFixed(2) : Math.round(r.cost_usd)}</span> : null}
            <button type="button" className="px-linkbtn" aria-expanded={why} data-testid={`px-rec-whybtn-${r.id}`}
                    onClick={() => (why ? setWhy(false) : r.why ? setWhy(true) : ctx.open("rec", r.id))}>Why? <CaretRight size={10} /></button>
          </div>
        )}
        {why && !rejecting ? (
          <p className="px-faint" style={{ margin: "6px 0 0", fontSize: ".6rem" }} data-testid={`px-rec-why-text-${r.id}`}>{r.why}{" "}
            <a href="#" onClick={(e) => { e.preventDefault(); ctx.open("rec", r.id); }}>Full reasoning ›</a></p>
        ) : null}
        <Said text={said} tone="red" />
      </div>
      {snoozing ? (
        <div className="px-rc-act">
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-size="s"
                       data-testid={`px-rec-snooze-confirm-${r.id}`} onClick={snooze}><Clock size={12} /> Snooze</GatedButton>
          <button type="button" className="px-btn" data-kind="ghost" data-size="s" onClick={() => setSnoozing(false)} data-testid={`px-rec-snooze-cancel-${r.id}`}>Cancel</button>
        </div>
      ) : rejecting ? (
        <div className="px-rc-act">
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-size="s"
                       data-testid={`px-rec-reject-confirm-${r.id}`} disabled={!hasReason}
                       title={hasReason ? `Reject: ${reason.trim() || chip}` : "Pick a reason first"} onClick={reject}>Reject</GatedButton>
          <button type="button" className="px-btn" data-kind="ghost" data-size="s" onClick={() => setRejecting(false)} data-testid={`px-rec-cancel-${r.id}`}>Cancel</button>
        </div>
      ) : (
        <div className="px-rc-act">
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-size="s" data-testid={`px-rec-accept-${r.id}`}
                       onClick={() => void act(() => acceptRecommendation(ctx.projectId, r.id, "ticket"), "accepted")}><Check size={12} /> Accept</GatedButton>
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-rec-reject-${r.id}`}
                       onClick={() => setRejecting(true)}><X size={12} /> Reject</GatedButton>
          <span className="px-rc-more">
            <button type="button" className="px-icon-btn" aria-label={`More for ${r.title}`} aria-haspopup="menu" aria-expanded={menu}
                    data-testid={`px-rec-more-${r.id}`} onClick={() => setMenu((m) => !m)}><DotsThree size={16} weight="bold" /></button>
            {menu ? (
              <span className="px-menu" role="menu">
                <GatedButton permission="work_item:manage" projectId={ctx.projectId} role="menuitem" className="px-menu-i" data-testid={`px-rec-snooze-${r.id}`}
                             onClick={() => { setMenu(false); setSnoozing(true); }}><Clock size={12} /> Not now…</GatedButton>
                <button type="button" role="menuitem" className="px-menu-i" onClick={() => { setMenu(false); ctx.open("rec", r.id); }}><Sparkle size={12} /> Details</button>
              </span>
            ) : null}
          </span>
        </div>
      )}
    </div>
  );
}

/** WHY › FULL REASONING (tester 07-10): the recommendation's own record — its diagram, the ≈ cost
 *  and what it is based on, the whole why (never cut), every piece of evidence and how it ranks.
 *  GET /v1/ambient/roadmap/{id}. Accepting and rejecting stay on the Roadmap card. */
type RecFull = { rec: Recommendation; why: string; evidence: { kind: string; ref: string; note: string }[];
  value: number | null; effort: number | null; strength: number | null; work: string };
const parseRecFull = (raw: unknown): RecFull => {
  const o = (v: unknown) => (v && typeof v === "object" ? v as Record<string, unknown> : {});
  const d = o(o(raw).detail);
  const facts = o(d.facts);
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  return {
    rec: parseRec(raw), why: typeof d.why === "string" ? d.why : "",
    evidence: (Array.isArray(d.evidence) ? d.evidence : []).map((e) => ({ kind: String(o(e).kind ?? ""), ref: String(o(e).ref ?? ""), note: String(o(e).note ?? "") })),
    value: num(d.value), effort: num(d.effort_units), strength: num(d.evidence_strength),
    work: typeof facts.text === "string" ? facts.text : "",
  };
};

export function RecDrawer({ ctx, id, onClose }: { ctx: PxCtx; id: string; onClose: () => void }) {
  const read = usePxRead<RecFull>(recPath(ctx.projectId, id), parseRecFull);
  const f = read.data;
  const r = f?.rec;
  return (
    <Drawer title={r?.title || "Recommendation"} icon={<Ico icon={Lightbulb} c="violet" />} onClose={onClose} testid="px-drawer-rec"
            sub={r ? <>
              <Tag>{KIND_TAG[r.kind] ?? r.kindLabel}</Tag>
              {r.card?.detector ? <span>{r.card.detector.label}</span> : null}
              {r.cost_usd ? <span className="px-tag px-mono" data-testid="px-drawer-rec-cost" title={r.card?.cost_basis || "This item's estimate"}>
                ≈ ${r.cost_usd < 1 ? r.cost_usd.toFixed(2) : Math.round(r.cost_usd)}{r.card?.cost_basis ? ` · ${r.card.cost_basis}` : ""}</span> : null}
            </> : null}>
      {!f ? <p className="px-faint">{read.state === "loading" ? "Reading it…" : read.error ?? "This recommendation isn't here any more."}</p> : (
        <div className="px-stack" style={{ gap: 14 }}>
          <Flow nodes={recFlow(f.rec, ctx.overview.captain?.squad ?? [])} testid="px-drawer-rec-flow" />
          <section>
            <span className="px-lbl">Why</span>
            <p style={{ margin: "4px 0 0", whiteSpace: "pre-wrap", lineHeight: 1.5 }} data-testid="px-drawer-rec-why">{f.why || f.rec.why || "No reasoning was recorded."}</p>
          </section>
          {f.work && f.work !== f.why ? (
            <section><span className="px-lbl">The work</span>
              <p style={{ margin: "4px 0 0", whiteSpace: "pre-wrap", lineHeight: 1.5 }} data-testid="px-drawer-rec-work">{f.work}</p></section>
          ) : null}
          {f.evidence.length ? (
            <section><span className="px-lbl">Evidence · {f.evidence.length}</span>
              <ul style={{ margin: "4px 0 0", paddingLeft: 16, lineHeight: 1.5 }} data-testid="px-drawer-rec-evidence">
                {f.evidence.map((e, i) => <li key={`${e.kind}-${e.ref}-${i}`}>{e.note || `${e.kind.replace(/_/g, " ")} ${e.ref}`}</li>)}
              </ul></section>
          ) : null}
          <section className="px-row px-wrap" style={{ gap: 12 }} data-testid="px-drawer-rec-rank">
            {f.value !== null ? <span className="px-score">Value <b>{Math.round(f.value)}</b>/100</span> : null}
            {f.effort !== null ? <span className="px-score">Effort <b>{f.effort}</b> unit{f.effort === 1 ? "" : "s"}</span> : null}
            {f.strength !== null ? <span className="px-score">Evidence <b>{Math.round(f.strength * 100)}%</b></span> : null}
          </section>
          <button type="button" className="px-btn" data-kind="primary" onClick={() => { onClose(); ctx.tab("roadmap"); }}>Accept or reject on the Roadmap</button>
        </div>
      )}
    </Drawer>
  );
}
