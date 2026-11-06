"use client";

/**
 * NOW, in three cards (cc v2): Needs you · In flight · Health.
 *
 * NEEDS YOU says each thing in a sentence with the face of the agent it is about, and a Review
 * button that opens the shared AttentionCard in place — the same buttons (approve, reject,
 * retry, waive, the doctor's options) the old page and Agents › Live draw, nothing re-made.
 * Its number is `counts.needs_you`, the one number the nav badge and Approvals say.
 *
 * IN FLIGHT is only what is really moving. A run that has not moved for over a week is
 * `stalled` (the server's word) and folds into one row; the doctor's old stuck-build questions
 * fold into another, with its safe fix one press away.
 *
 * HEALTH is money against the cap and the seven systems, each a link to the area that fixes it.
 */
import { withoutIds } from "../../lib/actor-words";
import React, { useState } from "react";

import type { AttentionItem, Mission } from "../../lib/agents-live-board";
import type { CommandCenterView, RecentRun, SystemDot } from "../../lib/command-center-view";
import { callAction } from "../../lib/call-action";
import { decisionsHref, runHref } from "../../lib/command-center";
import { AgentAvatar } from "../agent-avatar";
import { AttentionCard } from "../agents/attention-card";
import { nowParts } from "../agents/mission-row";
import { nameOf } from "../agents/run-face";
import { ago, linkTo, navigate, plainClick, runPatch } from "../agents/live-util";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { cleanUpOldAction } from "./actions";
import { GatedButton } from "../../components/gated";
import { Go } from "./hero";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import type { ListView } from "../../lib/ambient";
import { AmbientNeedRows } from "./agents-at-work";

/** Rows a card shows before `+n more` (the copy and row budgets of the approved look). */
export const NEEDS_ROWS = 3;
export const FLIGHT_ROWS = 3;

/** `/?area=setup&focus=models` from the server's `?area=setup&focus=models`, keeping the org. */
export function consoleHref(query: string, orgId?: string | null): string {
  const out = new URLSearchParams();
  if (orgId) out.set("org", orgId);
  new URLSearchParams(query.replace(/^[/?]+/, "")).forEach((v, k) => out.set(k, v));
  return `/?${out.toString()}`;
}

// ── Needs you ───────────────────────────────────────────────────────────────────────────────

export type NeedRow = {
  item: AttentionItem;
  /** The agent it is about (its face), or null for an icon. */
  roleKey: string | null;
  icon: string;
  title: string;
  sub: string;
  /** The primary (gradient) Review: work an agent is holding for you. */
  primary: boolean;
};

const cut = (s: string) => s.replace(/[?？]\s*$/, "");

/** One item as a sentence: what to do, about what, who raised it and how long it has waited. */
export function needRow(item: AttentionItem, now: number | null): NeedRow {
  const who = item.roleKey ? nameOf(item.roleKey).name : null;
  const waited = ago(item.at, now);
  const app = item.build?.app;
  if (item.kind === "approval" && item.build && (item.build.planUrl || /plan/i.test(item.title))) {
    return { item, roleKey: item.roleKey, icon: "✎", primary: true,
             title: `Approve the plan${app ? ` · ${app}` : ""}`,
             sub: [who ? `${who} drafted it` : "BOSS drafted it", waited && `waiting ${waited}`].filter(Boolean).join(" · ") };
  }
  if (item.kind === "approval" && item.build) {
    return { item, roleKey: item.roleKey, icon: "⇡", primary: true,
             title: `${cut(withoutIds(item.title))}${app ? ` · ${app}` : ""}`,
             sub: [who ? `${who} built it` : "Factory", waited && `waiting ${waited}`].filter(Boolean).join(" · ") };
  }
  if (item.kind === "approval") {
    const source = item.source || item.run?.subject || (item.decisionKind === "refusal" ? "Budget" : "");
    const money = /spend|budget|cost/i.test(source) || item.decisionKind === "refusal";
    return { item, roleKey: item.roleKey, icon: money ? "$" : "?", primary: !!item.roleKey,
             title: cut(withoutIds(item.title)),
             sub: [source, waited && (item.run ? `waiting ${waited}` : `raised ${waited} ago`)].filter(Boolean).join(" · ") };
  }
  if (item.kind === "readiness") {
    return { item, roleKey: null, icon: "⚙", primary: false, title: `Setup: ${withoutIds(item.title)}`,
             sub: item.detail || "Builds can't start until this is done" };
  }
  if (item.kind === "system_health") {
    return { item, roleKey: null, icon: "↺", primary: false, title: withoutIds(item.title),
             sub: [item.detail, waited && `${waited} ago`].filter(Boolean).join(" · ") };
  }
  // A failed step, a build stopped at its checks or its budget, the budget refusing at the door.
  return { item, roleKey: item.roleKey, icon: item.kind === "budget_refused" ? "$" : "!", primary: false,
           title: withoutIds(item.title),
           sub: [item.detail, waited && `${waited} ago`].filter(Boolean).join(" · ") };
}

function NeedLine({ row, projectId, actor, orgId, open, onToggle, onDone, onRefresh }: {
  row: NeedRow; projectId: string; actor: string; orgId?: string | null; open: boolean;
  onToggle: () => void; onDone: () => void; onRefresh: () => void;
}) {
  const fix = row.item.kind === "readiness" ? row.item.fix : undefined;
  const fixPatch = fix ? { area: fix.area || "setup", view: null, role: null, focus: fix.focus ?? null } : null;
  return (
    <div className="cc-need" data-open={open ? "true" : undefined} data-testid="cc-need" data-kind={row.item.kind}>
      <div className="cc-need-row">
        {row.roleKey ? (
          <span className="cc-need-face"><AgentAvatar agent={row.roleKey} size={36} aiBadge={false} state="needs_you" /></span>
        ) : <span className="cc-need-icon" aria-hidden>{row.icon}</span>}
        <div className="cc-need-text">
          <b data-user-text title={row.title}>{row.title}</b>
          <span title={row.sub}>{row.sub}</span>
        </div>
        {fixPatch ? (
          <Go className="cc-btn sm" patch={fixPatch} orgId={orgId} title={fix?.label} testId="cc-need-fix">Fix</Go>
        ) : (
          <button type="button" className={`cc-btn sm${row.primary ? " primary" : ""}`} aria-expanded={open}
                  data-testid="cc-need-review" onClick={onToggle}>
            {open ? "Close" : "Review"}
          </button>
        )}
      </div>
      {open ? (
        <div className="cc-need-card">
          <AttentionCard item={row.item} projectId={projectId} actor={actor} density="row" orgId={orgId}
                         onDone={onDone} onRefresh={onRefresh} />
        </div>
      ) : null}
    </div>
  );
}

export function NeedsCard({ view, lead, olderStopped, needsYou, projectId, actor, orgId, now, reachable,
                            onGone, onRefresh, ambient = null }: {
  view: CommandCenterView | null; lead: AttentionItem[]; olderStopped: AttentionItem[];
  needsYou: number | null; projectId: string; actor: string; orgId?: string | null; now: number | null;
  reachable: boolean; onGone: (id: string) => void; onRefresh: () => void;
  /** Agents at work's items (autonomous agents): one waiting on a person is a row here too. */
  ambient?: ListView | null;
}) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const [openId, setOpenId] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const all = lead.map((i) => needRow(i, now));
  // SETUP IS NEVER FOLDED: a gap stops every build, so its line is always on screen.
  const setup = all.filter((r) => r.item.kind === "readiness");
  const rows = all.filter((r) => r.item.kind !== "readiness");
  const shown = [...(more ? rows : rows.slice(0, NEEDS_ROWS)), ...setup];
  // ONE QUEUE: an ambient item whose decision is already a row above is not listed twice.
  const listed = new Set(lead.map((i) => i.decisionId ?? i.id));
  const ambientRows = <AmbientNeedRows view={ambient} listedDecisionIds={listed} />;
  const hasAmbient = (ambient?.items ?? []).some((i) => i.badges.outcome === "needs_you" && !(i.needs_you_decision_id && listed.has(i.needs_you_decision_id)));
  return (
    <section className="cc-card cc-needs cc-sec" id="cc2-needs-you" aria-labelledby="cc2-needs-you-h"
             data-quiet={view && all.length === 0 ? "true" : undefined}>
      <header className="cc-card-h">
        <h2 id="cc2-needs-you-h">Needs you</h2>
        {needsYou ? <span className="cc-n amber" data-testid="cc2-kpi-needs-you"
                          title="Decisions waiting. Same as Approvals and the top bar.">{needsYou}</span> : null}
        {/* THE ROWS AGREE WITH THE NUMBER (independent test): the number is decisions — the
            one needs-you rule — and anything else listed is said apart. */}
        {view && all.length > (needsYou ?? 0) ? (
          <span className="cc-n-more" data-testid="cc-needs-more"
                title="Also listed: stopped work, setup and system findings. Not decisions.">
            {needsYou ? `+${all.length - (needsYou ?? 0)} other` : `${all.length} to look at`}
          </span>
        ) : null}
        {view && view.counts.stopped > 0 ? (
          <span className="cc-chip red" data-testid="cc-stopped-chip" title="Failed steps or builds waiting for you">
            {view.counts.stopped} stopped
          </span>
        ) : null}
        <span className="cc-spacer" />
        <Go className="cc-link" patch={{ area: "decisions", decision_filter: "needs_action", role: null, view: null }}
            orgId={orgId} testId="cc-approvals-link">Approvals ↗</Go>
      </header>
      {/* Can't reach BOSS: the rows stay (the last thing BOSS said) but nothing can be pressed. */}
      <fieldset className="cc-card-body" disabled={!reachable} title={reachable ? undefined : "Can't reach BOSS"}>
        {!view ? null : all.length === 0 && hasAmbient ? (
          <div className="cc-needs-list">{ambientRows}</div>
        ) : all.length === 0 ? (
          <div className="cc-quiet-line" data-testid="cc2-needs-you-empty">
            <span className="cc-ok" aria-hidden>✓</span>
            <span title="Gates, plans, failed steps and setup gaps appear here.">Nothing needs you</span>
          </div>
        ) : (
          <div className="cc-needs-list">
            {shown.map((r) => (
              <NeedLine key={r.item.id} row={r} projectId={projectId} actor={actor} orgId={orgId}
                        open={openId === r.item.id}
                        onToggle={() => setOpenId((o) => (o === r.item.id ? null : r.item.id))}
                        onDone={() => { setOpenId(null); onGone(r.item.id); onRefresh(); }}
                        onRefresh={onRefresh} />
            ))}
            {ambientRows}
            {rows.length > NEEDS_ROWS ? (
              <button type="button" className="cc-more" aria-expanded={more} data-testid="cc-att-more"
                      onClick={() => setMore((v) => !v)}>
                {/* ONE NUMBER (re-check): the head says decisions + other = every row; this
                    opens to that same total. */}
                {more ? "Show fewer" : `Show all ${all.length}`}
              </button>
            ) : null}
          </div>
        )}
        {olderStopped.length || (view && view.counts.expiredAnswerable > 0) ? (
          <div className="cc-card-foot">
            {olderStopped.length ? (
              <Go className="cc-link-quiet" patch={{ area: "delivery_workflows", role: null, view: null }} orgId={orgId}
                  title="Failed steps from before today. Retry or skip them in Workflows." testId="cc-stopped-earlier">
                {olderStopped.length} stopped earlier ↗
              </Go>
            ) : null}
            {view && view.counts.expiredAnswerable > 0 ? (
              <a className="cc-link-quiet" data-testid="cc2-expired-answerable" href={decisionsHref("expired", orgId)}
                 title="Nothing was applied. You can still answer them.">
                {view.counts.expiredAnswerable} expired · still answerable ↗
              </a>
            ) : null}
          </div>
        ) : null}
      </fieldset>
    </section>
  );
}

// ── In flight ───────────────────────────────────────────────────────────────────────────────

/** The agent at a run's current hop, else null (a gate waits on you). */
function currentAgent(m: Mission): string | null {
  for (let i = m.hops.length - 1; i >= 0; i -= 1) {
    const h = m.hops[i];
    if (h.kind === "hop" && h.roleKey) return h.roleKey;
  }
  return null;
}

function FlightRow({ m, now, orgId }: { m: Mission; now: number | null; orgId?: string | null }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  // Before the client clock exists (the server's paint), the server's own elapsed time.
  const since = now === null ? (m.stepElapsedSeconds ?? null)
    : m.stepSince ? Math.max(0, (now - Date.parse(m.stepSince)) / 1000) : null;
  const parts = nowParts(m, since);
  const role = currentAgent(m);
  return (
    <a className="cc-fly" href={runHref(m.runId, orgId)} data-testid="cc-flight-row" data-state={m.state}
       onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(runPatch(m.runId)); } }}>
      {role ? <span className="cc-fly-face"><AgentAvatar agent={role} size={30} aiBadge={false}
                                                       state={m.state === "waiting_you" ? "needs_you" : "working"} /></span>
            : <span className="cc-need-icon sm" aria-hidden>{m.state === "waiting_you" ? "!" : "·"}</span>}
      <span className="cc-fly-text">
        <b data-user-text>{m.subject}</b>
        <span>
          {role ? `${nameOf(role).name} · ` : ""}
          {parts.map((p) => p.text.replace(/^step (\d+) of (\d+)$/, "step $1/$2")).join(" · ")}
        </span>
      </span>
    </a>
  );
}

function daysSince(iso: string | undefined, now: number | null): number {
  if (!iso || now === null) return 0;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? Math.floor((now - t) / 86_400_000) : 0;
}

export function FlightCard({ view, projectId, actor, orgId, now, onRefresh, onStart }: {
  view: CommandCenterView | null; projectId: string; actor: string; orgId?: string | null;
  now: number | null; onRefresh: () => void; onStart: () => void;
}) {
  const [more, setMore] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [cleaned, setCleaned] = useState<string>("");
  const missions = view?.missions ?? [];
  const active = missions.filter((m) => m.state !== "done" && m.state !== "stopped" && m.state !== "stalled");
  const stalled = missions.filter((m) => m.state === "stalled");
  const shown = more ? active : active.slice(0, FLIGHT_ROWS);
  const minDays = stalled.length
    ? Math.min(...stalled.map((m) => daysSince(m.lastProgressAt ?? m.stepSince ?? m.startedAt, now))) : 0;
  const cleanup = view?.cleanup ?? null;
  const recent: RecentRun[] = view?.recent ?? [];

  const cleanUp = async () => {
    setCleaning(true);
    const form = new FormData();
    form.set("project_id", projectId);
    form.set("actor", actor);
    try {
      const r = await callAction(cleanUpOldAction, undefined, "system:operate")(form);
      if (r.ok) {
        setCleaned(r.result || "Cleaned up");
        showToast("success", r.result || "Cleaned up");
        notifyChanged("decisions");
        onRefresh();
      } else showToast("error", r.error || "Could not clean up");
    } finally {
      setCleaning(false);
    }
  };

  return (
    <section className="cc-card cc-sec" id="cc2-running" aria-labelledby="cc2-running-h">
      <header className="cc-card-h">
        <h2 id="cc2-running-h">In flight</h2>
        <span className="cc-n blue" data-testid="cc2-kpi-running" title="Workflow runs moving now">
          {view ? view.counts.running : "—"}
        </span>
        <span className="cc-spacer" />
        <Go className="cc-link" patch={{ area: "delivery_workflows", role: null, view: null }} orgId={orgId}
            testId="cc-workflows-link">Workflows ↗</Go>
      </header>
      {!view ? null : active.length === 0 ? (
        <div className="cc-quiet" data-testid="cc2-running-empty">
          <span className="cc-radar" aria-hidden />
          <div>
            <b>Nothing running right now</b>
            <span>
              Start a <button type="button" className="cc-inline-link" onClick={onStart} data-testid="cc-start-work"
                              data-href={linkTo({ area: "delivery_workflows" }, orgId)}>build above</button>, or pick a template below.
            </span>
          </div>
        </div>
      ) : (
        <div className="cc-flights">
          {shown.map((m) => <FlightRow key={m.runId} m={m} now={now} orgId={orgId} />)}
          {active.length > FLIGHT_ROWS ? (
            <button type="button" className="cc-more" aria-expanded={more} data-testid="cc-lanes-more"
                    onClick={() => setMore((v) => !v)}>
              {more ? "Show fewer" : `+${active.length - FLIGHT_ROWS} more running`}
            </button>
          ) : null}
        </div>
      )}
      {stalled.length ? (
        <div className="cc-stall" data-testid="cc-stalled">
          <span className="cc-stall-ico red" aria-hidden>!</span>
          <div>
            <b>{stalled.length} run{stalled.length === 1 ? "" : "s"} stalled {minDays >= 7 ? `${minDays}+ days` : "over a week"}</b>
            <span title={stalled.map((m) => m.subject).join(" · ")}>{stalled.slice(0, 3).map((m) => m.subject).join(" · ")}</span>
          </div>
          <Go className="cc-btn sm" patch={{ area: "delivery_workflows", role: null, view: null }} orgId={orgId}
              title="Open them in Workflows: retry the step, or stop the run" testId="cc-stalled-open">
            Resume or close
          </Go>
        </div>
      ) : null}
      {cleanup ? (
        <div className="cc-stall" data-testid="cc-cleanup">
          <span className="cc-stall-ico" aria-hidden>↺</span>
          <div>
            <b>{cleaned || cleanup.title}</b>
            <span>{cleaned ? "Done. It drops off on the next read." : cleanup.detail}</span>
          </div>
          {cleaned ? null : (
            <GatedButton permission="system:operate" type="button" className="cc-btn sm" disabled={cleaning} data-testid="cc-cleanup-go"
                    title="Closes these questions. A build that has not moved in over a week is stopped."
                    onClick={() => void cleanUp()}>
              {cleaning ? "Cleaning…" : "Clean up"}
            </GatedButton>
          )}
        </div>
      ) : null}
      {recent.length || (view?.today.runsFinished ?? 0) > 0 || (view?.today.runsFailed ?? 0) > 0 ? (
        <ul className="cc-done" aria-label="Finished lately" data-testid="cc-done">
          {view && (view.today.runsFinished > 0 || view.today.runsFailed > 0) ? (
            <li className="cc-done-n" data-testid="cc-kpi-done-today" title="Runs that finished today">
              {view.today.runsFinished} done today
              {view.today.runsFailed > 0 ? <em className="cc-failed"> · {view.today.runsFailed} failed</em> : null}
            </li>
          ) : null}
          {recent.slice(0, 3).map((r) => {
            const ok = r.outcome === "succeeded";
            const failed = r.outcome === "failed" || r.outcome === "deadlettered";
            return (
              <li key={r.runId}>
                <a href={runHref(r.runId, orgId)} data-testid="cc2-recent-row" data-outcome={ok ? "ok" : failed ? "failed" : "other"}
                   title={failed && r.error ? `${r.workflowName} · ${r.error.split("\n")[0]}` : `${r.workflowName} · ${r.outcome}`}
                   onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(runPatch(r.runId)); } }}>
                  <span aria-label={ok ? "Succeeded" : failed ? "Failed" : r.outcome}>{ok ? "✓" : failed ? "✕" : "–"}</span>
                  <span data-user-text>{r.name}</span>
                  <em>{ago(r.finishedAt, now)}</em>
                </a>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}

// ── Health ──────────────────────────────────────────────────────────────────────────────────

const PERIOD_WORD: Record<string, string> = { day: "Today", week: "This week", month: "This month" };

function usd(micros: number): string {
  const v = Math.max(0, micros) / 1_000_000;
  if (v >= 100 && Math.abs(v - Math.round(v)) < 0.005) return `$${Math.round(v).toLocaleString("en-US")}`;
  return `$${v.toFixed(2)}`;
}

/** Today's money, as the old SPENT number said it: metered spend, or Included with its API value. */
export function todaySpend(view: CommandCenterView | null): string {
  if (!view) return "";
  const t = view.today;
  if (t.moneyMicros === 0 && t.includedMicros > 0) return `Today: included · ${usd(t.includedMicros)} API value`;
  return t.includedMicros > 0 ? `Today: ${usd(t.moneyMicros)} · ${usd(t.includedMicros)} included`
    : `Today: ${usd(t.moneyMicros)}`;
}

export function HealthCard({ view, reachable, orgId }: {
  view: CommandCenterView | null; reachable: boolean; orgId?: string | null;
}) {
  const b = view?.budget;
  const cap = b?.capUsdMicros ?? 0;
  const spent = b?.actualUsdMicros ?? 0;
  const pct = cap > 0 ? Math.min(100, (100 * spent) / cap) : 0;
  const tone = b?.state === "red" ? "red" : b?.state === "orange" || b?.state === "yellow" ? "amber" : "teal";
  const systems: SystemDot[] = view?.systems ?? [];
  return (
    <section className="cc-card cc-sec cc-health" id="cc-systems-card" aria-labelledby="cc-health-h">
      <header className="cc-card-h">
        <h2 id="cc-health-h">Health</h2>
        <span className="cc-spacer" />
        <Go className="cc-link" patch={{ area: "infra", role: null, view: null }} orgId={orgId} testId="cc-infra-link">Infra ↗</Go>
      </header>
      <Go className="cc-gauge" patch={{ area: "budget", role: null, view: null }} orgId={orgId} testId="cc-budget-link"
          title={[b?.resetsAtLabel ? `Resets ${b.resetsAtLabel}` : "", "Opens Budget"].filter(Boolean).join(". ")}>
        <span className="cc-ring" data-tone={tone} style={{ ["--p" as string]: `${cap > 0 ? Math.max(1.5, pct) : 0}` }} aria-hidden />
        <span className="cc-gauge-text">
          {view ? (
            cap > 0 ? <b>{usd(spent)} <small>of {usd(cap)}</small></b> : <b>{usd(spent)} <small>no cap</small></b>
          ) : <b>—</b>}
          <span>
            {PERIOD_WORD[b?.period ?? "month"] ?? "This period"}
            {b?.subscription ? " · subscription calls free" : ""}
          </span>
        </span>
      </Go>
      {/* Today's money, when the ring is not already today's (a day-period budget). */}
      {view && (b?.period !== "day" || view.today.includedMicros > 0)
        ? <div className="cc-today-spend" data-testid="cc-today-spend">{todaySpend(view)}</div> : null}
      <ul className="cc-systems" data-testid="cc-systems" aria-label="Systems">
        {systems.map((s) => {
          const t = reachable ? s.tone : "grey";
          const state = reachable ? s.state : "Unknown";
          const tip = reachable ? s.tip : "Can't reach BOSS";
          return (
            <li key={s.key} className="cc-sys" data-tone={t}>
              <a href={consoleHref(s.href, orgId)} title={`${state} · ${tip}`} data-testid={`cc-dot-${s.key}`}
                 aria-label={`${s.word}: ${state}. ${tip}`}>
                <i aria-hidden />{s.word}
              </a>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
