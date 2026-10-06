"use client";

/**
 * Factory › Floor (factory spec §4.2): the same sections as Agents › Live, for builds.
 *
 *   NEEDS YOU   cards you can act on (AttentionCard — plan approvals, failed checks, PRs, budget)
 *   BUILDING    one row per build, drawn as its line of stations (BuildRow)
 *   SHIPPED     outcome chips: PR opened / merged, preview live, deployed
 *   STOPPED     failed or stopped builds with nothing newer, each with Retry
 *
 * One read (`/v1/factory/floor`, polled by the shell) feeds every section. An empty section says
 * so in one line and keeps its place, so the page shape never jumps.
 */
import { CheckCircle, WarningCircle } from "@phosphor-icons/react";
import React, { useEffect, useMemo, useRef, useState } from "react";

import type { BuildRow as Build, Floor, FloorOutcome, PreBuild, StoppedRow } from "../../lib/factory-floor";
import { callAction } from "../../lib/call-action";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { AttentionCard } from "../agents/attention-card";
import { splitAttention } from "../agents/live-board";
import { AgFaceStack } from "../agents/agent-face";
import { RunFace, nameOf } from "../agents/run-face";
import { RING_BUDGET, ago, linkTo, navigate, plainClick, useBand, useNow, type Band } from "../agents/live-util";
import { notifyChanged } from "../changed";
import { showToast } from "../client-forms";
import { BuildRow, buildAnimates } from "./build-row";
import { retryBuildAction } from "./floor-actions";

export const CARDS_SHOWN = 4;
export const ROWS_SHOWN = 8;
export const GROUP_AT = 20;
export const OUTCOMES_SHOWN = 8;
export const STOPPED_SHOWN = 5;

const RANK: Record<string, number> = {
  needs_plan: 0, needs_waiver: 0, budget_refused: 0, blocked: 1, quiet: 1,
  building: 2, budget_wait: 2, queued: 3,
};

/** Waiting on you → quiet or blocked → building → queued. Stable inside a rank (the server's order). */
export function sortBuilds(builds: Build[]): Build[] {
  return builds.map((b, i) => ({ b, i }))
    .sort((x, y) => (RANK[x.b.state] ?? 2) - (RANK[y.b.state] ?? 2) || x.i - y.i)
    .map(({ b }) => b);
}

/** Does a build match what was typed in Find a build: its app, title or an agent on its line. */
export function matches(q: string, ...texts: (string | null | undefined)[]): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return texts.some((t) => (t ?? "").toLowerCase().includes(needle));
}

function agentsOf(b: Build): string[] {
  const keys = new Set<string>();
  for (const s of b.stations) {
    if (s.roleKey) keys.add(s.roleKey);
    for (const seat of s.seats) if (seat.roleKey) keys.add(seat.roleKey);
  }
  return [...keys].map((k) => nameOf(k).name);
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
    <div className="ag-live-skeleton" aria-busy="true" data-testid="fx-loading">
      <span className="ag-sr">Loading</span>
      <div className="ag-live-skel-cards"><i /><i /></div>
      <div className="ag-live-skel-rows"><i /><i /><i /></div>
      <div className="ag-live-skel-chips"><i /></div>
    </div>
  );
}

function OutcomeChip({ o, now, onOpen }: { o: FloorOutcome; now: number | null; onOpen: (runId: string) => void }) {
  useIdentityVersion();
  const by = o.roleKey ? nameOf(o.roleKey).name : "BOSS";
  const external = !!o.url && /^https?:/i.test(o.url);
  return (
    <li className="ag-out">
      <a className="ag-out-chip" href={external ? o.url : undefined} title={`${o.app} · by ${by}`} data-testid="fx-outcome"
         data-kind={o.kind} {...(external ? { target: "_blank", rel: "noreferrer" } : { role: "button", tabIndex: 0 })}
         onClick={(e) => { if (!external) { e.preventDefault(); onOpen(o.runId); } }}
         onKeyDown={(e) => { if (!external && e.key === "Enter") onOpen(o.runId); }}>
        <RunFace roleKey={o.roleKey} variant={o.roleKey ? "agent" : "boss"} size={20} state="done" />
        <span className="ag-out-text" data-user-text>{o.text}</span>
        <span className="ag-out-age">{ago(o.at, now)}</span>
      </a>
    </li>
  );
}

function StoppedLine({ s, projectId, actor, onOpen, onRetried }: {
  s: StoppedRow; projectId: string; actor: string;
  onOpen: (runId: string, from: HTMLElement | null) => void; onRetried: () => void;
}) {
  useIdentityVersion();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function retry() {
    if (busy || !s.retryable) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    form.set("run_id", s.runId);
    form.set("project_id", projectId);
    form.set("actor", actor);
    try {
      const r = await callAction(retryBuildAction, undefined, "code_task:retry")(form);
      if (r.ok) {
        showToast("success", r.attempt ? `Attempt ${r.attempt} started` : "Retrying");
        notifyChanged("code_tasks");
        onRetried();
      } else setError(r.error ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className="fx-row fx-stopped" data-testid={`fx-stopped-${s.runId}`} data-state={s.state}
        onClick={(e) => { if (!(e.target as HTMLElement).closest("button, a")) onOpen(s.runId, e.currentTarget); }}>
      <span className="fx-row-name">
        <button type="button" className="fx-row-open ag-trunc" title={s.title}
                onClick={(e) => onOpen(s.runId, e.currentTarget.closest("li"))}>
          <span data-user-text>{s.app}</span>
        </button>
      </span>
      <span className="fx-stopped-face" data-still>
        {s.roleKey ? <RunFace roleKey={s.roleKey} size={24} state="stuck" standalone />
          : <RunFace roleKey={null} variant={s.state === "cancelled" ? "you" : "boss"} size={24} state="stuck" standalone />}
      </span>
      <span className="fx-row-now">
        <span className={`fx-word fx-word-${s.state}`} title={s.tip}>{s.word}</span>
        {error ? <span className="ag-att-error" role="alert" data-user-text title={error}> · {error.slice(0, 40)}</span> : null}
      </span>
      <button type="button" className="ag-btn fx-retry" disabled={!s.retryable || busy} data-testid={`fx-retry-${s.runId}`}
              title={!s.retryable ? s.retryReason : "A new attempt on the same branch"} aria-busy={busy || undefined}
              onClick={() => void retry()}>Retry</button>
      <button type="button" className="ag-icon-btn fx-row-go" aria-label={`Open ${s.app}`} title="Open the build"
              onClick={(e) => onOpen(s.runId, e.currentTarget.closest("li"))}>↗</button>
    </li>
  );
}

function PreBuildRow({ p, orgId }: { p: PreBuild; orgId?: string | null }) {
  useIdentityVersion();
  const patch = { area: "meetings", meeting: p.meetingRunId, view: null, role: null };
  return (
    <li className="fx-row fx-prebuild" data-testid={`fx-prebuild-${p.meetingRunId}`} data-state="planning">
      <span className="fx-row-name"><span className="ag-trunc" data-user-text title={p.title}>{p.title}</span></span>
      <span className="fx-line" role="list" aria-label="Stations">
        <span role="listitem" className="fx-bay" data-key="plan" data-status="working" title="Debating the PRD">
          <AgFaceStack roleKeys={p.seats} size={24} max={3} />
        </span>
      </span>
      <span className="fx-row-now"><span className="fx-word" title="Debating the PRD">Planning</span></span>
      <span className="fx-row-cost" aria-hidden />
      <a className="ag-link" href={linkTo(patch, orgId)}
         onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch); } }}>Watch ↗</a>
    </li>
  );
}

export type FloorViewProps = {
  projectId: string;
  actor: string;
  floor: Floor | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
  paused?: boolean;
  query?: string;
  orgId?: string | null;
  band?: Band;
  onOpen: (runId: string, from: HTMLElement | null) => void;
  onPlan: () => void;
  onDirect: () => void;
  onApps: () => void;
};

export function FloorView({ projectId, actor, floor, error, loading, refresh, paused, query = "", orgId, band: bandProp, onOpen, onPlan, onDirect, onApps }: FloorViewProps) {
  const now = useNow(1000);
  const measured = useBand();
  const band = bandProp ?? measured;
  const [moreCards, setMoreCards] = useState(false);
  const [moreRows, setMoreRows] = useState(false);
  const [moreStopped, setMoreStopped] = useState(false);
  const [gone, setGone] = useState<Set<string>>(new Set());
  const firstIds = useRef<Set<string> | null>(null);
  if (floor && firstIds.current === null) firstIds.current = new Set(floor.attention.map((a) => a.id));

  // Find a build filters the cards too (walk D17): a card is about a build (its app, its title).
  const attention = useMemo(() => (floor?.attention ?? []).filter((a) => !gone.has(a.id)
    && matches(query, a.build?.app, a.title, a.run?.subject, a.detail)), [floor, gone, query]);
  const box = useRef<HTMLDivElement>(null);
  // J / K move between cards and rows from anywhere on the Floor — no click first (walk D17).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const key = e.key.toLowerCase();
      if ((key !== "j" && key !== "k") || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
      const t = e.target as HTMLElement | null;
      if (t?.closest?.("input, textarea, select, [contenteditable='true'], [role='dialog']")) return;
      const root = box.current;
      if (!root || !root.isConnected) return;
      const all = [...root.querySelectorAll<HTMLElement>("[data-att-card], .fx-row-open")];
      if (!all.length) return;
      const at = all.findIndex((el) => el.contains(document.activeElement) || el === document.activeElement);
      const next = at < 0 ? (key === "j" ? 0 : all.length - 1)
        : key === "j" ? Math.min(all.length - 1, at + 1) : Math.max(0, at - 1);
      e.preventDefault();
      all[next]?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  useEffect(() => {
    if (!floor) return;
    setGone((g) => {
      const ids = new Set(floor.attention.map((a) => a.id));
      const next = new Set([...g].filter((id) => ids.has(id)));
      return next.size === g.size ? g : next;
    });
  }, [floor]);

  if (error && !floor) {
    return (
      <div className="fx-floor" data-testid="fx-floor">
        <div className="ag-live-alert" role="alert" data-testid="fx-unreachable">
          <WarningCircle size={14} weight="bold" aria-hidden />
          <span title={error}>Can&apos;t reach BOSS</span>
          <button type="button" className="ag-btn" onClick={refresh}>Retry</button>
        </div>
      </div>
    );
  }
  if (loading || !floor) {
    return <div className="fx-floor" data-testid="fx-floor"><Skeleton /></div>;
  }

  const nothingEver = floor.builds.length === 0 && floor.outcomes.length === 0 && floor.stopped.length === 0
    && floor.preBuild.length === 0 && attention.length === 0 && floor.counts.apps === 0;
  if (nothingEver) {
    return (
      <div className="fx-floor" data-testid="fx-floor">
        <div className="ag-empty fx-empty" data-testid="fx-no-builds">
          <span>No builds yet</span>
          <button type="button" className="ag-btn ag-btn-primary" onClick={onPlan} data-testid="fx-empty-plan">Plan it with Juno</button>
          <button type="button" className="ag-btn" onClick={onDirect} data-testid="fx-empty-direct">Direct change</button>
        </div>
      </div>
    );
  }

  const builds = sortBuilds(floor.builds).filter((b) => matches(query, b.app, b.title, ...agentsOf(b)));
  const stopped = floor.stopped.filter((s) => matches(query, s.app, s.title));
  const outcomes = floor.outcomes.filter((o) => matches(query, o.app, o.text)).slice(0, OUTCOMES_SHOWN);
  const preBuild = floor.preBuild.filter((p) => matches(query, p.title));
  // As Agents › Live (walk D2, D12): the number is DECISIONS, the pill's number; a stopped build
  // is a card too, first, and never folded under `+n more`.
  const split = splitAttention(attention);
  const decisions = split.decisions;
  const cards = [...split.stopped, ...(moreCards ? decisions : decisions.slice(0, CARDS_SHOWN))];
  const grouped = builds.length > GROUP_AT;
  const rows = grouped || moreRows ? builds : builds.slice(0, ROWS_SHOWN);
  const stoppedShown = moreStopped ? stopped : stopped.slice(0, STOPPED_SHOWN);

  // THE RING BUDGET (spec §5.2): at most eight faces move on the Floor; past that they hold still.
  let budget = paused ? 0 : RING_BUDGET;
  const stillCard = new Map<string, boolean>();
  for (const a of cards) {
    const moves = a.kind === "approval" || a.kind === "readiness";
    stillCard.set(a.id, !!paused || (moves && budget <= 0));
    if (moves && budget > 0) budget -= 1;
  }
  const stillRow = new Map<string, boolean>();
  for (const b of rows) {
    const moves = buildAnimates(b);
    stillRow.set(b.runId, !!paused || (moves && budget <= 0));
    if (moves && budget > 0) budget -= 1;
  }

  const renderRow = (b: Build) => (
    <BuildRow key={b.runId} build={b} band={band} now={paused ? null : now} still={stillRow.get(b.runId)} onOpen={onOpen} />
  );
  const groups = grouped ? groupByApp(rows) : [];
  const approvalsPatch = { area: "decisions", decision_filter: "needs_action", role: null, view: null };

  return (
    <div ref={box} className="fx-floor" data-testid="fx-floor" data-band={band}>
      {paused ? (
        <span className="ag-live-paused" data-testid="fx-paused"
              title="Live updates are paused. Press Live in the top bar.">Paused</span>
      ) : null}

      <section className="ag-live-sec" data-sec="needs" role="region" aria-labelledby="fx-needs-h">
        <SectionHead id="fx-needs-h" label="NEEDS YOU" count={decisions.length}
                     extra={split.stopped.length ? `· ${split.stopped.length} stopped` : undefined}>
          {attention.length ? (
            <a className="ag-link" href={linkTo(approvalsPatch, orgId)}
               onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(approvalsPatch); } }}>
              All in Approvals ↗
            </a>
          ) : null}
        </SectionHead>
        {attention.length === 0 ? (
          <div className="ag-live-quiet" data-testid="fx-nothing-needs">
            <CheckCircle size={14} weight="bold" aria-hidden />
            <span title="Agents ask here before anything risky.">Nothing needs you</span>
          </div>
        ) : (
          <>
            <div className="ag-att-grid">
              {cards.map((a) => (
                <AttentionCard key={a.id} item={a} projectId={projectId} actor={actor}
                               enter={!firstIds.current?.has(a.id)} still={stillCard.get(a.id)} orgId={orgId}
                               onDone={() => { setGone((g) => new Set(g).add(a.id)); refresh(); }}
                               onRefresh={refresh} />
              ))}
            </div>
            {decisions.length > CARDS_SHOWN ? (
              <button type="button" className="ag-zone-more" aria-expanded={moreCards} onClick={() => setMoreCards((v) => !v)}>
                {moreCards ? "Show fewer" : `+${decisions.length - CARDS_SHOWN} more`}
              </button>
            ) : null}
          </>
        )}
      </section>

      <section className="ag-live-sec" data-sec="building" role="region" aria-labelledby="fx-building-h">
        <SectionHead id="fx-building-h" label="BUILDING" count={builds.length + preBuild.length} />
        {builds.length === 0 && preBuild.length === 0 ? (
          <div className="ag-live-quiet" data-testid="fx-nothing-building"><span>Nothing building</span></div>
        ) : (
          <>
            <ul className="fx-rows" aria-label="Building">
              {preBuild.map((p) => <PreBuildRow key={p.meetingRunId} p={p} orgId={orgId} />)}
              {grouped ? groups.map(([app, list]) => (
                <GroupRows key={app} app={app} list={list} render={renderRow} />
              )) : rows.map(renderRow)}
            </ul>
            {!grouped && builds.length > ROWS_SHOWN ? (
              <button type="button" className="ag-zone-more" aria-expanded={moreRows} data-testid="fx-more-building"
                      onClick={() => setMoreRows((v) => !v)}>
                {moreRows ? "Show fewer" : `+${builds.length - ROWS_SHOWN} more building`}
              </button>
            ) : null}
          </>
        )}
      </section>

      <section className="ag-live-sec" data-sec="shipped" role="region" aria-labelledby="fx-shipped-h">
        <SectionHead id="fx-shipped-h" label="SHIPPED">
          <button type="button" className="ag-link" onClick={onApps} data-testid="fx-shipped-apps">Apps ↗</button>
        </SectionHead>
        {outcomes.length === 0 ? (
          <div className="ag-live-quiet" data-testid="fx-nothing-shipped"><span>Nothing shipped this week</span></div>
        ) : (
          <ul className="ag-out-row">
            {outcomes.map((o, i) => <OutcomeChip key={`${o.runId}-${o.kind}-${i}`} o={o} now={now} onOpen={(id) => onOpen(id, null)} />)}
          </ul>
        )}
      </section>

      <section className="ag-live-sec" data-sec="stopped" role="region" aria-labelledby="fx-stopped-h">
        <SectionHead id="fx-stopped-h" label="STOPPED" count={stopped.length} />
        {stopped.length === 0 ? (
          <div className="ag-live-quiet" data-testid="fx-nothing-stopped"><span>Nothing stopped</span></div>
        ) : (
          <>
            <ul className="fx-rows" aria-label="Stopped">
              {stoppedShown.map((s) => (
                <StoppedLine key={s.runId} s={s} projectId={projectId} actor={actor} onOpen={onOpen} onRetried={refresh} />
              ))}
            </ul>
            {stopped.length > STOPPED_SHOWN ? (
              <button type="button" className="ag-zone-more" aria-expanded={moreStopped} onClick={() => setMoreStopped((v) => !v)}>
                {moreStopped ? "Show fewer" : `+${stopped.length - STOPPED_SHOWN} more`}
              </button>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}

function groupByApp(builds: Build[]): [string, Build[]][] {
  const out = new Map<string, Build[]>();
  for (const b of builds) out.set(b.app, [...(out.get(b.app) ?? []), b]);
  return [...out.entries()];
}

function GroupRows({ app, list, render }: { app: string; list: Build[]; render: (b: Build) => React.ReactNode }) {
  const [open, setOpen] = useState(false);
  if (list.length === 1) return <>{render(list[0])}</>;
  return (
    <li className="fx-group" data-testid="fx-group">
      <button type="button" className="fx-group-head" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span data-user-text>{app}</span> ×{list.length}
      </button>
      {open ? <ul className="fx-rows">{list.map(render)}</ul> : null}
    </li>
  );
}
