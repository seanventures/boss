"use client";

/**
 * Agents › Live (agents-home spec §4.1-§4.7): what needs you, what is moving, what got done,
 * and who is idle, top to bottom, in that order, at every width (the doc's own phone order,
 * used everywhere, so there is one layout).
 *
 *   NEEDS YOU      cards you can act on (AttentionCard)
 *   LIVE WORK      one lane per running workflow (MissionRow)
 *   DONE LATELY    outcome chips: a PR opened, a report ready, a run finished
 *   AGENT FORCE    the teams, compact (TeamZones)
 *
 * One read (`/v1/agents/live-board`, polled by the area shell) feeds every section. A section
 * with nothing in it says so in one line and keeps its place, so the page never jumps.
 */
import { GatedButton } from "../../components/gated";
import { CaretDown, CaretRight, CheckCircle, WarningCircle } from "@phosphor-icons/react";
import React, { useEffect, useMemo, useRef, useState } from "react";

import { openPlan } from "../../lib/converse-doors";
import type { AttentionItem, LiveBoardState, Mission, Outcome } from "../../lib/agents-live-board";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AttentionCard } from "./attention-card";
import { MissionRow, faceMatches, rowAnimates } from "./mission-row";
import { RunFace, nameOf } from "./run-face";
import { TeamZones } from "./team-zones";
import { BuildRow } from "../factory/build-row";
import { RING_BUDGET, ago, linkTo, navigate, plainClick, runPatch, useBand, useNow } from "./live-util";

/** Cards shown before `+n more`. */
export const CARDS_SHOWN = 4;
/** Rows shown before `+n more running`. */
export const ROWS_SHOWN = 6;
/** Past this many runs, rows group by workflow. */
export const GROUP_AT = 20;
/** Outcome chips shown. */
export const OUTCOMES_SHOWN = 8;

const MISSION_RANK: Record<string, number> = {
  waiting_you: 0, stopped: 1, blocked: 2, running: 3, waiting: 3, waiting_person: 3, stalled: 4, done: 5,
};

/** What NEEDS YOU draws (walk D2, D12). The number is DECISIONS, the same one the pill, the
 *  Command Center and Approvals say. A step that failed is a card too, and is never folded away
 *  under `+n more`: it has stopped a run, so it comes first. Setup gaps are not cards here; they
 *  are one quiet line each, to Setup, which has its own badge. */
export function splitAttention(items: AttentionItem[]): {
  stopped: AttentionItem[]; decisions: AttentionItem[]; setup: AttentionItem[];
} {
  return {
    // A build stopped at its checks or its budget has stopped too (factory spec §6.5).
    stopped: items.filter((a) => a.kind === "failed_step" || a.kind === "gate_override" || a.kind === "budget_refused"),
    decisions: items.filter((a) => a.kind === "approval"),
    setup: items.filter((a) => a.kind === "readiness"),
  };
}

/** The spec's order: waiting on you, failed, blocked, running, just finished. Stable inside. */
export function sortMissions(missions: Mission[]): Mission[] {
  return missions.map((m, i) => ({ m, i }))
    .sort((a, b) => (MISSION_RANK[a.m.state] ?? 3) - (MISSION_RANK[b.m.state] ?? 3) || a.i - b.i)
    .map(({ m }) => m);
}

function SectionHead({ id, label, count, extra, children }: { id: string; label: string; count?: number; extra?: string; children?: React.ReactNode }) {
  return (
    <header className="ag-live-head">
      <h2 id={id} className="ag-zone-label">{label}</h2>
      {count !== undefined && count > 0 ? <span className="ag-zone-count" data-testid={`${id}-count`}>{count}</span> : null}
      {extra ? <span className="ag-zone-extra" data-testid={`${id}-extra`}>{extra}</span> : null}
      <span className="ag-spacer" />
      {children}
    </header>
  );
}

function Skeleton() {
  return (
    <div className="ag-live-skeleton" aria-busy="true" data-testid="ag-live-loading">
      <span className="ag-sr">Loading</span>
      <div className="ag-live-skel-cards"><i /><i /></div>
      <div className="ag-live-skel-rows"><i /><i /><i /></div>
      <div className="ag-live-skel-chips"><i /></div>
      <div className="ag-live-skel-zones">{Array.from({ length: 8 }, (_, i) => <i key={i} />)}</div>
    </div>
  );
}

export function OutcomeChip({ o, now, orgId }: { o: Outcome; now: number | null; orgId?: string | null }) {
  useIdentityVersion();
  const by = o.roleKey ? nameOf(o.roleKey).name : "";
  const tip = [o.label, by ? `by ${by}` : ""].filter(Boolean).join(", ");
  const href = o.url ? o.url : o.runId ? linkTo(runPatch(o.runId), orgId) : undefined;
  const external = !!o.url && /^https?:/i.test(o.url);
  return (
    <li className="ag-out">
      <a className="ag-out-chip" href={href} title={tip || undefined} data-testid="ag-outcome"
         {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
         onClick={(e) => {
           if (!external && o.runId && plainClick(e)) { e.preventDefault(); navigate(runPatch(o.runId)); }
         }}>
        <RunFace roleKey={o.roleKey} variant={o.roleKey ? "agent" : "boss"} size={20} state="done" />
        {/* A finished run reads `<subject> finished` (spec §4.4); an artifact reads as sent. */}
        {o.kind === "run" && o.label
          ? <span className="ag-out-text"><span data-user-text>{o.label}</span> finished</span>
          : <span className="ag-out-text" data-user-text>{o.text}</span>}
        <span className="ag-out-age">{ago(o.at, now)}</span>
      </a>
    </li>
  );
}

export type LiveBoardProps = {
  projectId: string;
  actor: string;
  live: LiveBoardState;
  /** The console's Live/Paused chip says Paused. */
  paused?: boolean;
  /** `?focus=<attention id>`: open on that card. */
  focusId?: string | null;
  search?: string;
  onOpenMap: (team?: string) => void;
  onActivity: () => void;
  onDirectory: () => void;
  onNewAgent: () => void;
  orgId?: string | null;
};

export function LiveBoardView({ projectId, actor, live, paused, focusId, search, onOpenMap, onActivity, onDirectory, onNewAgent, orgId }: LiveBoardProps) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const { board, error, loading, refresh } = live;
  const now = useNow(1000);
  const band = useBand();
  const [moreCards, setMoreCards] = useState(false);
  const [moreRows, setMoreRows] = useState(false);
  const [openGroups, setOpenGroups] = useState<Set<string>>(new Set());
  const [focus, setFocus] = useState<{ id: string; n: number } | null>(null);
  const [gone, setGone] = useState<Set<string>>(new Set());
  // Cards on screen at first paint do not rise in; only one that arrives while you watch does.
  const firstIds = useRef<Set<string> | null>(null);
  if (board && firstIds.current === null) firstIds.current = new Set(board.attention.map((a) => a.id));

  const attention = useMemo(() => (board?.attention ?? []).filter((a) => !gone.has(a.id)), [board, gone]);
  // A card decided here (or elsewhere) leaves for good once the server stops sending it.
  useEffect(() => {
    if (!board) return;
    setGone((g) => {
      const ids = new Set(board.attention.map((a) => a.id));
      const next = new Set([...g].filter((id) => ids.has(id)));
      return next.size === g.size ? g : next;
    });
  }, [board]);

  function focusCard(id: string) {
    const item = attention.find((a) => a.id === id);
    if (!item) return;
    const parts = splitAttention(attention);
    if (item.kind === "approval" && parts.decisions.indexOf(item) >= Math.max(0, CARDS_SHOWN - parts.stopped.length)) setMoreCards(true);
    setFocus((f) => ({ id, n: (f?.n ?? 0) + 1 }));
  }
  useEffect(() => {
    if (focusId && board) focusCard(focusId);
  }, [focusId, Boolean(board)]); // eslint-disable-line react-hooks/exhaustive-deps, once per arrival

  if (error && !board) {
    return (
      <div className="ag-live" data-testid="ag-live">
        <div className="ag-live-alert" role="alert" data-testid="ag-live-unreachable">
          <WarningCircle size={14} weight="bold" aria-hidden />
          <span title={error}>Can&apos;t reach BOSS</span>
          <button type="button" className="ag-btn" onClick={refresh}>Retry</button>
        </div>
      </div>
    );
  }
  if (loading || !board) {
    return <div className="ag-live" data-testid="ag-live"><Skeleton /></div>;
  }
  if (board.counts.agents === 0 && board.agents.length === 0) {
    return (
      <div className="ag-live" data-testid="ag-live">
        <div className="ag-empty" data-testid="ag-live-no-agents">
          <span>No agents yet</span>
          <button type="button" className="ag-btn ag-btn-primary" onClick={onNewAgent}>+ New agent</button>
        </div>
      </div>
    );
  }

  const { stopped, decisions, setup } = splitAttention(attention);
  const room = Math.max(0, CARDS_SHOWN - stopped.length);
  const overflow = decisions.length > room;
  const hiddenDecisions = decisions.length - room;
  const cards = [...stopped, ...(moreCards ? decisions : decisions.slice(0, room))];
  const asking = stopped.length + decisions.length;
  const missions = sortMissions(board.missions);
  // Every build the Floor draws under BUILDING, with the Floor's row: same rows, same count.
  const builds = board.builds ?? [];
  const grouped = missions.length > GROUP_AT;
  const rows = grouped ? missions : moreRows ? missions : missions.slice(0, ROWS_SHOWN);

  // THE RING BUDGET (spec §5.2): at most eight faces move on Live; past that they hold still.
  let budget = RING_BUDGET;
  const stillCard = new Map<string, boolean>();
  for (const a of cards) {
    const moves = a.kind === "approval" || a.kind === "readiness";
    stillCard.set(a.id, moves && budget <= 0);
    if (moves && budget > 0) budget -= 1;
  }
  const stillRow = new Map<string, boolean>();
  for (const m of rows) {
    const moves = rowAnimates(m);
    stillRow.set(m.runId, moves && budget <= 0);
    if (moves && budget > 0) budget -= 1;
  }

  function onCardsKey(e: React.KeyboardEvent<HTMLElement>) {
    const key = e.key.toLowerCase();
    if ((key !== "j" && key !== "k") || e.metaKey || e.ctrlKey || e.altKey) return;
    if ((e.target as HTMLElement).closest("input, textarea, select")) return;
    const all = [...e.currentTarget.querySelectorAll<HTMLElement>("[data-att-card]")];
    if (!all.length) return;
    const at = all.findIndex((el) => el.contains(document.activeElement));
    const next = key === "j" ? Math.min(all.length - 1, at + 1) : Math.max(0, at - 1);
    e.preventDefault();
    all[next]?.focus();
  }

  const renderRow = (m: Mission) => (
    <MissionRow key={m.runId} mission={m} band={band} now={now} still={stillRow.get(m.runId)}
                onYou={(id) => focusCard(id)} orgId={orgId} search={search} />
  );

  const groups = grouped ? groupBy(rows) : [];
  const outcomes = board.outcomes.slice(0, OUTCOMES_SHOWN);

  return (
    <div className="ag-live" data-testid="ag-live" data-band={band}>
      {paused ? (
        <span className="ag-live-paused" data-testid="ag-live-paused"
              title="Live updates are paused. Press Live in the top bar.">Paused</span>
      ) : null}

      <section className="ag-live-sec" data-sec="needs" role="region" aria-labelledby="ag-live-needs-h"
               id="ag-live-needs" onKeyDown={onCardsKey}>
        <SectionHead id="ag-live-needs-h" label="NEEDS YOU" count={decisions.length}
                     extra={stopped.length ? `· ${stopped.length} stopped` : undefined}>
          {decisions.length ? (
            <a className="ag-link" href={linkTo({ area: "decisions", decision_filter: "needs_action", role: null, view: null }, orgId)}
               onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate({ area: "decisions", decision_filter: "needs_action", role: null, view: null }); } }}>
              All in Approvals 
            </a>
          ) : null}
        </SectionHead>
        {asking === 0 ? (
          <div className="ag-live-quiet" data-testid="ag-live-nothing-needs">
            <CheckCircle size={14} weight="bold" aria-hidden />
            <span title="Agents will ask here before anything risky.">Nothing needs you</span>
          </div>
        ) : (
          <>
            <div className="ag-att-grid">
              {cards.map((a) => (
                <AttentionCard key={a.id} item={a} projectId={projectId} actor={actor}
                               enter={!firstIds.current?.has(a.id)} still={stillCard.get(a.id)}
                               focusKey={focus?.id === a.id ? focus.n : undefined} orgId={orgId}
                               dim={!faceMatches(a.roleKey, search)}
                               onDone={() => { setGone((g) => new Set(g).add(a.id)); refresh(); }}
                               onRefresh={refresh} />
              ))}
            </div>
            {overflow ? (
              <button type="button" className="ag-zone-more" aria-expanded={moreCards} data-testid="ag-att-more"
                      onClick={() => setMoreCards((v) => !v)}>
                {moreCards ? "Show fewer" : `+${hiddenDecisions} more`}
              </button>
            ) : null}
          </>
        )}
        {setup.length ? (
          <ul className="ag-live-setup" data-testid="ag-live-setup">
            {setup.map((a) => {
              const patch = { area: a.fix?.area || "setup", view: null, role: null, focus: a.fix?.focus ?? null };
              return (
                <li key={a.id}>
                  <a className="ag-link" href={linkTo(patch, orgId)} data-testid={`ag-live-setup-${a.id}`}
                     title={a.fix?.label || a.whatToDo || undefined}
                     onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch); } }}>
                    Setup: <span data-user-text>{a.title}</span></a>
                </li>
              );
            })}
          </ul>
        ) : null}
      </section>

      <section className="ag-live-sec" data-sec="work" role="region" aria-labelledby="ag-live-work-h" id="ag-live-work">
        <SectionHead id="ag-live-work-h" label="LIVE WORK" count={missions.length + builds.length} />
        {builds.length ? (
          <ul className="ag-mission-list fx-rows" aria-label="Builds" data-testid="ag-live-builds">
            {builds.map((b) => (
              <li key={b.runId}>
                <BuildRow build={b} band={band} now={now}
                          onOpen={(id) => navigate({ area: "code_tasks", build: id, view: null, role: null })} />
              </li>
            ))}
          </ul>
        ) : null}
        {missions.length === 0 && builds.length ? null : missions.length === 0 ? (
          <div className="ag-live-quiet" data-testid="ag-live-nothing-running">
            <span>Nothing running</span>
            <a className="ag-btn" href={linkTo({ area: "delivery_workflows", tab: "library", view: null, role: null }, orgId)}
               onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate({ area: "delivery_workflows", tab: "library", view: null, role: null }); } }}>
              Start work 
            </a>
            <GatedButton permission="meeting:convene" type="button" className="ag-link" data-testid="ag-live-plan"
                    onClick={() => openPlan("pm", { from: "roster" })}>Plan it with Juno</GatedButton>
          </div>
        ) : grouped ? (
          <ul className="ag-mission-list">
            {groups.map(([key, list]) => {
              const open = openGroups.has(key);
              const label = list[0].workflowName || key;
              return (
                <li key={key} className="ag-mission-group">
                  <button type="button" className="ag-mission-group-head" aria-expanded={open}
                          title={`${list.length} runs of ${label}`} data-testid={`ag-mission-group-${key}`}
                          onClick={() => setOpenGroups((g) => { const n = new Set(g); if (n.has(key)) n.delete(key); else n.add(key); return n; })}>
                    {open ? <CaretDown size={10} weight="bold" aria-hidden /> : <CaretRight size={10} weight="bold" aria-hidden />}
                    <span data-user-text>{label}</span> ×{list.length}
                  </button>
                  {open ? <ul className="ag-mission-list">{list.map(renderRow)}</ul> : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <>
            <ul className="ag-mission-list">{rows.map(renderRow)}</ul>
            {missions.length > ROWS_SHOWN ? (
              <button type="button" className="ag-zone-more" aria-expanded={moreRows} data-testid="ag-mission-more"
                      onClick={() => setMoreRows((v) => !v)}>
                {moreRows ? "Show fewer" : `+${missions.length - ROWS_SHOWN} more running`}
              </button>
            ) : null}
          </>
        )}
      </section>

      <section className="ag-live-sec" data-sec="done" role="region" aria-labelledby="ag-live-done-h" id="ag-live-done">
        <SectionHead id="ag-live-done-h" label="DONE LATELY">
          <button type="button" className="ag-link" onClick={onActivity}>Activity </button>
        </SectionHead>
        {outcomes.length === 0 ? (
          <div className="ag-live-quiet" data-testid="ag-live-nothing-done"><span>Nothing finished today</span></div>
        ) : (
          <ul className="ag-out-row">{outcomes.map((o, i) => <OutcomeChip key={o.actId ?? `${o.runId}-${i}`} o={o} now={now} orgId={orgId} />)}</ul>
        )}
      </section>

      <section className="ag-live-sec" data-sec="force" role="region" aria-labelledby="ag-live-force-h" id="ag-live-force">
        <SectionHead id="ag-live-force-h" label="AGENT FORCE" count={board.counts.agents || board.agents.length}>
          <button type="button" className="ag-link" onClick={() => onOpenMap()}>Open map </button>
        </SectionHead>
        <TeamZones projectId={projectId} board={board} density="compact" search={search}
                   onOpenMap={(team) => onOpenMap(team)} />
      </section>

      <div className="ag-live-sec ag-live-phone-only" data-sec="directory">
        <button type="button" className="ag-link" onClick={onDirectory}>Directory </button>
      </div>
    </div>
  );
}

function groupBy(missions: Mission[]): [string, Mission[]][] {
  const out = new Map<string, Mission[]>();
  for (const m of missions) {
    const key = m.workflowKey || m.workflowName;
    out.set(key, [...(out.get(key) ?? []), m]);
  }
  return [...out.entries()];
}

/** The tab dot (spec §3.1): amber with attention, accent while agents work, none when idle. */
export function liveDot(needs: number, working: number): { tone: "amber" | "accent"; tip: string } | null {
  if (needs > 0) return { tone: "amber", tip: needs === 1 ? "1 needs you" : `${needs} need you` };
  if (working > 0) return { tone: "accent", tip: "Agents working" };
  return null;
}
