"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CheckCircle } from "@phosphor-icons/react";

import {
  TZ_COOKIE,
  statusOf,
  useCommandCenterView,
  viewerZone,
  type CommandCenterView,
  type MotionMode,
  type RecentRun,
} from "../lib/command-center-view";
import { decisionsHref, runHref } from "../lib/command-center";
import { useConsoleLive } from "../lib/console-live";
import { AttentionCard } from "./agents/attention-card";
import { CARDS_SHOWN, OutcomeChip, ROWS_SHOWN, sortMissions, splitAttention } from "./agents/live-board";
import { RING_BUDGET, ago, linkTo, navigate, plainClick, reducedMotion, useBand, useNow } from "./agents/live-util";
import { MissionRow, rowAnimates } from "./agents/mission-row";
import { DayStrip } from "./command-center/day-strip";
import { Floor } from "./command-center/floor";
import { StatusLine } from "./command-center/status-line";
import { SystemsRow } from "./command-center/systems-row";
import { TodayChart, type TodayTab } from "./command-center/today-chart";
import { WeekBars } from "./command-center/week-bars";
import { HostAdviceBanner, useHostAdvice } from "./host-advice";
import { OnboardingHelpRows } from "./onboarding-help-card";

/**
 * THE COMMAND CENTER: THE BRIDGE (command-center spec §4).
 *
 * One calm screen where every mark that moves is something BOSS is really doing. Top to bottom:
 * the state in one sentence and four numbers; the composer; NOW — the Floor (your agents in a
 * ring of teams, You at the centre) beside what needs you; TODAY against yesterday; the day's
 * WORK as a state timeline over the shared run lanes; SYSTEMS as seven dots; THIS WEEK.
 *
 * COMPOSITION, NOT RESTATEMENT. One read, GET /v1/command-center/view, which IS Agents › Live's
 * board plus five bounded reads: the cards are Live's AttentionCard (honouring the server's
 * decide_inline — this page used to force it off), the lanes are Live's MissionRow, the chips
 * Live's OutcomeChip, and NEED YOU is the one needs-you number.
 *
 * MOTION ONLY ON A REAL CHANGE, and none at all while the console is Paused, the tab is hidden,
 * BOSS cannot be reached, or the host is struggling (the page must not compete for its CPU).
 *
 * It replaced three stacked lists under a composer (and before them a task-era page whose
 * "Live coordination" orbs animated from a field nothing populates). The parity table in the
 * spec (§9) maps everything that page showed or did to its place here, and
 * tests/command-center-parity.test.tsx holds it.
 */

const TAB_KEY = "cc-today-tab";
const SPEND_ONCE_KEY = "cc-spend-auto";

function readStore(store: "local" | "session", key: string): string | null {
  try { return (store === "local" ? window.localStorage : window.sessionStorage).getItem(key); } catch { return null; }
}
function writeStore(store: "local" | "session", key: string, value: string): void {
  try { (store === "local" ? window.localStorage : window.sessionStorage).setItem(key, value); } catch { /* none */ }
}

/** The viewport width (px), 1440 before mount so the server and first client render agree. */
function useWidth(): number {
  const [w, setW] = useState(1440);
  useEffect(() => {
    const read = () => setW(window.innerWidth || 1440);
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, []);
  return w;
}

function useVisible(): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const read = () => setVisible(document.visibilityState !== "hidden");
    read();
    document.addEventListener("visibilitychange", read);
    return () => document.removeEventListener("visibilitychange", read);
  }, []);
  return visible;
}

/** How much the page may move (§5.2). Exported for the tests. */
export function motionOf({ paused, hidden, reachable, hostCritical, reduced }: {
  paused: boolean; hidden: boolean; reachable: boolean; hostCritical: boolean; reduced: boolean;
}): MotionMode {
  if (paused || hidden || !reachable || hostCritical) return "none";
  return reduced ? "reduced" : "full";
}

function Head({ id, label, count, children, extra }: {
  id: string; label: string; count?: number; extra?: React.ReactNode; children?: React.ReactNode;
}) {
  return (
    <header className="cc-head">
      <h2 id={id} className="cc-head-label">{label}</h2>
      {count !== undefined && count > 0 ? <span className="cc-head-count">{count}</span> : null}
      {extra}
      <span className="cc-spacer" />
      {children}
    </header>
  );
}

function HeadLink({ patch, orgId, children }: {
  patch: Record<string, string | null>; orgId?: string | null; children: React.ReactNode;
}) {
  return (
    <a className="cc-head-link" href={linkTo(patch, orgId)}
       onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch); } }}>
      {children}
    </a>
  );
}

function localWhen(iso: string | null, now: number | null): string {
  if (!iso) return "";
  // Until the client clock exists, UTC: identical on the server and in the browser.
  if (now === null) return `${iso.slice(0, 16).replace("T", " ")} UTC`;
  const t = Date.parse(iso);
  return Number.isFinite(t)
    ? new Date(t).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : "";
}

function RunChip({ r, now, orgId }: { r: RecentRun; now: number | null; orgId?: string | null }) {
  const ok = r.outcome === "succeeded";
  const failed = r.outcome === "failed" || r.outcome === "deadlettered";
  const tip = failed && r.error
    ? `${r.workflowName} · ${r.error.split("\n")[0]}`
    : `${r.workflowName} · finished ${localWhen(r.finishedAt, now)}`;
  const patch = { area: "delivery_workflows", wfr: r.runId, role: null, view: null };
  return (
    <li className="ag-out">
      <a className="ag-out-chip cc-run-chip" data-outcome={ok ? "ok" : failed ? "failed" : "other"}
         href={runHref(r.runId, orgId)} title={tip} data-testid="cc2-recent-row"
         onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch); } }}>
        <span className="cc-run-glyph" aria-label={ok ? "Succeeded" : failed ? "Failed" : r.outcome}>
          {ok ? "✓" : failed ? "✕" : "–"}
        </span>
        <span className="ag-out-text" data-user-text>{r.name}</span>
        <span className="ag-out-age">{ago(r.finishedAt, now)}</span>
      </a>
    </li>
  );
}

export type CommandCenterProps = {
  projectId: string;
  orgId?: string;
  actor: string;
  /** The server's first paint (same view the poll reads), or null when it could not be read. */
  initial: CommandCenterView | null;
  /** Why the server could not read it. */
  initialError?: string | null;
  /** The zone the server read in (the `boss_tz` cookie, else UTC). */
  tz?: string;
  viewer?: string | null;
  /** Ask BOSS, laid out as the one-line composer (page.tsx mounts it). */
  composer?: React.ReactNode;
  /** What you can do here (the full help card) for the `?` popover. */
  help?: React.ReactNode;
};

export function CommandCenter({ projectId, orgId, actor, initial, initialError = null, tz = "UTC",
                                viewer, composer, help }: CommandCenterProps) {
  // THE VIEWER'S ZONE. The server painted in the cookie's zone (UTC on a first visit); the
  // browser knows the real one, so it remembers it for the next server paint and reads in it.
  const [zone, setZone] = useState(tz);
  useEffect(() => {
    const mine = viewerZone();
    if (mine !== tz) {
      try { document.cookie = `${TZ_COOKIE}=${encodeURIComponent(mine)}; path=/; max-age=31536000; samesite=lax`; } catch { /* none */ }
      setZone(mine);
    }
  }, [tz]);

  const polled = useCommandCenterView(projectId, zone, initial, viewer);
  // THE LAST GOOD VIEW STAYS. A new zone is a new url (the poll starts empty), and a failed read
  // must leave what BOSS last said on screen, dimmed — never an empty factory.
  const [view, setView] = useState<CommandCenterView | null>(initial);
  const [readAt, setReadAt] = useState<number | null>(null);
  useEffect(() => {
    if (polled.data) { setView(polled.data); setReadAt(Date.now()); }
  }, [polled.data]);
  const error = polled.error ?? (view ? null : initialError);
  const reachable = !error && !!view;

  const live = useConsoleLive();
  const visible = useVisible();
  const [reduced, setReduced] = useState(false);
  useEffect(() => { setReduced(reducedMotion()); }, []);
  const hostCritical = !!view?.host.show && view.host.severity === "critical";
  const motion = motionOf({ paused: !live, hidden: !visible, reachable, hostCritical, reduced });
  const motionWhy = hostCritical ? "Motion paused while the host struggles" : null;
  // The banner reads the view's host and polls nothing of its own (§6.3).
  const host = useHostAdvice(projectId, 60_000, view?.host ?? { show: false });

  const now = useNow(30_000);
  const width = useWidth();
  const band = useBand();
  const phone = width < 640;

  const status = statusOf(view, reachable, readAt, !live);
  const tip = !reachable && error ? `${status.tip} · ${error}` : status.tip;

  // TODAY'S TAB: remembered per viewer; a budget that stopped work shows Spend once a session.
  const [tab, setTab] = useState<TodayTab>("work");
  useEffect(() => {
    const kept = readStore("local", TAB_KEY);
    if (kept === "spend" || kept === "work") setTab(kept);
  }, []);
  const chooseTab = useCallback((t: TodayTab) => { setTab(t); writeStore("local", TAB_KEY, t); }, []);
  useEffect(() => {
    if (view?.status.key !== "budget_stopped" || readStore("session", SPEND_ONCE_KEY)) return;
    writeStore("session", SPEND_ONCE_KEY, "1");
    setTab("spend");
  }, [view?.status.key]);

  const [helpOpen, setHelpOpen] = useState(false);
  const [hoverRunId, setHoverRunId] = useState<string | null>(null);
  const [moreCards, setMoreCards] = useState(false);
  const [moreRows, setMoreRows] = useState(false);
  const [focus, setFocus] = useState<{ id: string; n: number } | null>(null);
  const [gone, setGone] = useState<Set<string>>(new Set());
  const firstIds = useRef<Set<string> | null>(null);
  if (view && firstIds.current === null) firstIds.current = new Set(view.attention.map((a) => a.id));
  useEffect(() => {
    if (!view) return;
    setGone((g) => {
      const ids = new Set(view.attention.map((a) => a.id));
      const next = new Set([...g].filter((id) => ids.has(id)));
      return next.size === g.size ? g : next;
    });
  }, [view]);

  const attention = useMemo(() => (view?.attention ?? []).filter((a) => !gone.has(a.id)), [view, gone]);
  const refresh = polled.refresh;

  const focusCard = useCallback((id: string) => {
    const item = attention.find((a) => a.id === id);
    if (!item) return;
    const parts = splitAttention(attention);
    if (item.kind === "approval" && parts.decisions.indexOf(item) >= Math.max(0, CARDS_SHOWN - parts.stopped.length)) setMoreCards(true);
    setFocus((f) => ({ id, n: (f?.n ?? 0) + 1 }));
  }, [attention]);

  const toNeeds = useCallback(() => {
    document.getElementById("cc2-needs-you")?.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
  }, [reduced]);
  const startWork = useCallback(() => {
    const box = document.querySelector<HTMLTextAreaElement>('[data-testid="askboss-input"]');
    box?.scrollIntoView({ block: "center" });
    box?.focus();
  }, []);

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

  const { stopped, decisions, setup } = splitAttention(attention);
  const room = Math.max(0, CARDS_SHOWN - stopped.length);
  const hiddenDecisions = Math.max(0, decisions.length - room);
  const cards = [...stopped, ...(moreCards ? decisions : decisions.slice(0, room))];
  const lanesAll = sortMissions((view?.missions ?? []).filter((m) => m.state !== "done"));
  const lanes = moreRows ? lanesAll : lanesAll.slice(0, ROWS_SHOWN);

  // THE RING BUDGET (agents-home §5.2): at most eight faces move; with any motion off, none.
  let budget = motion === "full" ? RING_BUDGET : 0;
  const stillCard = new Map<string, boolean>();
  for (const a of cards) {
    const moves = a.kind === "approval" || a.kind === "readiness";
    stillCard.set(a.id, moves && budget <= 0);
    if (moves && budget > 0) budget -= 1;
  }
  const stillRow = new Map<string, boolean>();
  for (const m of lanes) {
    const moves = rowAnimates(m);
    stillRow.set(m.runId, moves && budget <= 0);
    if (moves && budget > 0) budget -= 1;
  }

  const recentIds = new Set((view?.recent ?? []).map((r) => r.runId));
  // A run that finished is one chip: its run chip (with its outcome), not also an artifact chip.
  const outcomes = (view?.outcomes ?? []).filter((o) => !(o.kind === "run" && o.runId && recentIds.has(o.runId))).slice(0, 8);
  const recent = view?.recent ?? [];
  const firstRun = !!view && !view.today.everRan;

  // THE ORDER IS BY URGENCY (§4.4, §5.3), in the DOM so the tab order follows it: on a phone
  // Status → Needs you → Composer → Work → Floor → Today → Systems → Week; from 1024px the
  // composer leads and NOW pairs the Floor with Needs you (Needs you first while stacked).
  const statusSec = (
    <section className="cc-sec cc-sec-status" id="cc-status" aria-label="Status" data-testid="cc-status">
      <StatusLine status={{ ...status, tip }} counts={view?.counts ?? null} today={view?.today ?? null}
                  onSpend={() => chooseTab("spend")} help={help} helpOpen={helpOpen} onHelp={setHelpOpen} />
    </section>
  );
  const composeSec = (
    <section className="cc-sec cc-sec-compose" id="cc-compose" aria-label="Ask BOSS">
      {composer}
      {firstRun ? <OnboardingHelpRows onMore={() => setHelpOpen(true)} /> : null}
    </section>
  );
  const floorSec = (
    <section className="cc-sec cc-sec-floor" id="cc-floor" aria-labelledby="cc-floor-h" data-testid="cc-floor">
      <Head id="cc-floor-h" label="THE FLOOR">
        <HeadLink patch={{ area: "workforce", view: "organization", role: null }} orgId={orgId}>Open map ↗</HeadLink>
      </Head>
      {view ? (
        <Floor view={view} projectId={projectId} motion={motion} motionWhy={motionWhy} band={band}
               orgId={orgId} onYou={toNeeds} onThread={focusCard} />
      ) : <div className="cc-floor-empty" />}
    </section>
  );
  const needsSec = (
    <section className="cc-sec cc-sec-needs" id="cc2-needs-you" aria-labelledby="cc2-needs-you-h"
             onKeyDown={onCardsKey}>
      <Head id="cc2-needs-you-h" label="NEEDS YOU" count={view?.counts.needsYou}
            extra={stopped.length ? <span className="cc-chip red" data-testid="cc-stopped-chip">{stopped.length} stopped</span> : undefined}>
        <HeadLink patch={{ area: "decisions", decision_filter: "needs_action", role: null, view: null }} orgId={orgId}>
          All in Approvals ↗
        </HeadLink>
      </Head>
      {/* Can't reach BOSS: the cards stay (the last thing BOSS said) but nothing can be
          pressed — a decision sent into the void is worse than one not sent. */}
      <fieldset className="cc-needs-body" disabled={!reachable} title={reachable ? undefined : "Can't reach BOSS"}>
        {!view ? null : stopped.length + decisions.length === 0 ? (
          <div className="cc-quiet" data-testid="cc2-needs-you-empty">
            <CheckCircle size={14} weight="bold" aria-hidden />
            <span title="Gates, failed steps and setup gaps appear here.">Nothing needs you</span>
          </div>
        ) : (
          <div className="cc-cards ag-att-rows">
            {cards.map((a) => (
              <AttentionCard key={a.id} item={a} projectId={projectId} actor={actor} density="row"
                             enter={!firstIds.current?.has(a.id)} still={stillCard.get(a.id)}
                             focusKey={focus?.id === a.id ? focus.n : undefined} orgId={orgId}
                             onDone={() => { setGone((g) => new Set(g).add(a.id)); refresh(); }}
                             onRefresh={refresh} />
            ))}
            {hiddenDecisions > 0 ? (
              <button type="button" className="ag-zone-more" aria-expanded={moreCards} data-testid="cc-att-more"
                      onClick={() => setMoreCards((v) => !v)}>
                {moreCards ? "Show fewer" : `+${hiddenDecisions} more`}
              </button>
            ) : null}
          </div>
        )}
        {setup.length ? (
          <ul className="ag-live-setup" data-testid="cc2-setup">
            {setup.map((a) => {
              const patch = { area: a.fix?.area || "setup", view: null, role: null, focus: a.fix?.focus ?? null };
              return (
                <li key={a.id}>
                  <a className="ag-link" href={linkTo(patch, orgId)} title={a.fix?.label || a.whatToDo || undefined}
                     onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch); } }}>
                    Setup: <span data-user-text>{a.title}</span> ↗
                  </a>
                </li>
              );
            })}
          </ul>
        ) : null}
        {view && view.counts.expiredAnswerable > 0 ? (
          <a className="cc-expired ag-link" data-testid="cc2-expired-answerable"
             href={decisionsHref("expired", orgId)} title="Nothing was applied. You can still answer them.">
            {view.counts.expiredAnswerable} expired · still answerable ↗
          </a>
        ) : null}
      </fieldset>
    </section>
  );
  const todaySec = (
    <section className="cc-sec cc-sec-today" id="cc-today" aria-labelledby="cc-today-h" data-testid="cc-today">
      <Head id="cc-today-h" label="TODAY">
        <HeadLink patch={{ area: "budget", role: null, view: null }} orgId={orgId}>Budget ↗</HeadLink>
      </Head>
      {view ? (
        <TodayChart view={view} tab={tab} onTab={chooseTab} motion={motion} nowMs={now} tz={view.tz}
                    compact={phone} />
      ) : null}
    </section>
  );
  const workSec = (
    <section className="cc-sec cc-sec-work" id="cc2-running" aria-labelledby="cc-work-h">
      <Head id="cc-work-h" label="LAST 24H">
        <HeadLink patch={{ area: "delivery_workflows", role: null, view: null }} orgId={orgId}>Workflow ↗</HeadLink>
      </Head>
      {view ? (
        <DayStrip day={view.day} nowMs={now} generatedAt={view.generatedAt} windowHours={phone ? 12 : 24}
                  rowsMax={width >= 1440 ? 10 : 8} motion={motion} hoverRunId={hoverRunId}
                  onHover={setHoverRunId} orgId={orgId} onStartWork={startWork} />
      ) : null}

      <div className="cc-sub" aria-labelledby="cc-running-h">
        <Head id="cc-running-h" label="RUNNING" count={lanesAll.length} />
        {!view ? null : lanesAll.length === 0 ? (
          <div className="cc-quiet" data-testid="cc2-running-empty">
            <span>Nothing running</span>
            <button type="button" className="ag-btn" onClick={startWork}
                    data-href={linkTo({ area: "delivery_workflows" }, orgId)}>Start work ↗</button>
          </div>
        ) : (
          <>
            <div className="cc-lanes">
              {lanes.map((m) => (
                <div key={m.runId} className="cc-lane" data-run-id={m.runId}
                     data-hover={hoverRunId === m.runId ? "true" : undefined}
                     onMouseEnter={() => setHoverRunId(m.runId)} onMouseLeave={() => setHoverRunId(null)}>
                  <ul className="ag-mission-list">
                    <MissionRow mission={m} density="lane" band={band} now={now} still={stillRow.get(m.runId)}
                                onYou={focusCard} orgId={orgId} />
                  </ul>
                </div>
              ))}
            </div>
            {lanesAll.length > ROWS_SHOWN ? (
              <button type="button" className="ag-zone-more" aria-expanded={moreRows} data-testid="cc-lanes-more"
                      onClick={() => setMoreRows((v) => !v)}>
                {moreRows ? "Show fewer" : `+${lanesAll.length - ROWS_SHOWN} more running`}
              </button>
            ) : null}
          </>
        )}
      </div>

      <div className="cc-sub" id="cc2-recent" aria-labelledby="cc-done-h">
        <Head id="cc-done-h" label="DONE LATELY">
          <HeadLink patch={{ area: "workforce", view: "activity", role: null }} orgId={orgId}>Activity ↗</HeadLink>
        </Head>
        {!view ? null : outcomes.length + recent.length === 0 ? (
          <div className="cc-quiet" data-testid="cc-done-empty"><span>Nothing finished this week</span></div>
        ) : (
          <ul className="ag-out-row cc-done">
            {recent.map((r) => <RunChip key={r.runId} r={r} now={now} orgId={orgId} />)}
            {outcomes.map((o, i) => <OutcomeChip key={o.actId ?? `${o.runId}-${i}`} o={o} now={now} orgId={orgId} />)}
          </ul>
        )}
      </div>
    </section>
  );
  const systemsSec = (
    <section className="cc-sec cc-sec-systems" id="cc-systems" aria-labelledby="cc-systems-h">
      <Head id="cc-systems-h" label="SYSTEMS" />
      {view ? <SystemsRow systems={view.systems} reachable={reachable} motion={motion} orgId={orgId} /> : null}
    </section>
  );
  const weekSec = (
    <section className="cc-sec cc-sec-week" id="cc-week" aria-labelledby="cc-week-h" data-testid="cc-delivery">
      <Head id="cc-week-h" label="THIS WEEK">
        <HeadLink patch={{ area: "workforce", view: "activity", role: null }} orgId={orgId}>Activity ↗</HeadLink>
      </Head>
      <div className="cc-week" data-testid="cc-week">{view ? <WeekBars week={view.week} tz={view.tz} /> : null}</div>
    </section>
  );

  return (
    <div className="cc" data-testid="command-center" data-reachable={reachable ? "true" : "false"}
         data-stale={reachable ? undefined : "true"} data-motion={motion} data-band={band}
         data-layout={phone ? "phone" : width < 1280 ? "stacked" : width < 1440 ? "split" : "wide"}>
      {/* The only banner this page has: the host's own words, above the state (§4.11). */}
      <HostAdviceBanner advice={host} />
      {statusSec}
      {phone ? (
        <>{needsSec}{composeSec}{workSec}{floorSec}{todaySec}{systemsSec}{weekSec}</>
      ) : (
        <>
          {composeSec}
          <div className="cc-now">{width < 1280 ? <>{needsSec}{floorSec}</> : <>{floorSec}{needsSec}</>}</div>
          {todaySec}{workSec}{systemsSec}{weekSec}
        </>
      )}
    </div>
  );
}
