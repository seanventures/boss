"use client";

import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import {
  TZ_COOKIE,
  parseCommandCenterView,
  statusOf,
  useCommandCenterView,
  viewerZone,
  type CommandCenterView,
  type MotionMode,
} from "../lib/command-center-view";
import { useCcApps } from "../lib/command-center-apps";
import { useConsoleLive } from "../lib/console-live";
import type { AttentionItem } from "../lib/agents-live-board";
import { splitAttention } from "./agents/live-board";
import { ago, reducedMotion, useNow } from "./agents/live-util";
import { FlightCard, HealthCard, NeedsCard } from "./command-center/cards";
import { Go, Proof, TeamGrid, proofOf } from "./command-center/hero";
import { PulseRow } from "./command-center/pulse";
import { AppsShelf, TemplatesShelf } from "./command-center/rails";
import { ConnectorsShelf } from "./command-center/connectors-shelf";
import { HostAdviceBanner, useHostAdvice } from "./host-advice";
import { OnboardingHelpRows } from "./onboarding-help-card";
import { HelpPopover } from "./command-center/help-popover";
import { nextAction } from "./command-center/next-action";
import { announcePainted } from "../lib/area-painted";
import { ReturningBrief } from "./command-center/returning-brief";

/**
 * Existing Command Center records lead with the next useful action. The same composer stays
 * mounted; Needs you, In flight, Health, app results, templates and team details remain.
 * No readiness, completion, spend or execution authority is created by this presentation.
 * Reads and motion retain the shared polling, visibility and console Live rules.
 */

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

/** How much the page may move. Exported for the tests. */
export function motionOf({ paused, hidden, reachable, hostCritical, reduced }: {
  paused: boolean; hidden: boolean; reachable: boolean; hostCritical: boolean; reduced: boolean;
}): MotionMode {
  if (paused || hidden || !reachable || hostCritical) return "none";
  return reduced ? "reduced" : "full";
}

/** Needs you's order. Pure: the refusal at the door first, then decisions, then what stopped
 *  today (split at the viewer's midnight, `today.hours[24].start`, the server's), then the
 *  doctor's recent findings, then setup. Stopped work from before today is one line. */
export function needsOrder(view: CommandCenterView | null, items: AttentionItem[]):
    { lead: AttentionItem[]; olderStopped: AttentionItem[] } {
  const { stopped, decisions, setup } = splitAttention(items);
  const system = items.filter((a) => a.kind === "system_health");
  const r = view?.budgetRefusal;
  const refusalCard: AttentionItem[] = r && !stopped.some((a) => a.kind === "budget_refused") ? [{
    id: "budget:refusal", kind: "budget_refused", title: r.title, detail: r.detail,
    roleKey: null, actions: [], answers: [], decideInline: false,
    fix: { area: "budget", label: "Raise budget" },
  }] : [];
  const midnight = Date.parse(view?.today.hours[24]?.start ?? "");
  const today = (a: AttentionItem) => !Number.isFinite(midnight) || !a.at || Date.parse(a.at) >= midnight;
  return {
    lead: [...refusalCard, ...decisions, ...stopped.filter(today), ...system, ...setup],
    olderStopped: stopped.filter((a) => !today(a)),
  };
}

/** Agents on a live run's current step — the same missions In flight draws — so a face glows
 *  exactly while its run moves (independent test: Juno worked, her face sat still). */
export function workingOn(missions: { state: string; hops: { kind: string; roleKey?: string | null; state?: string }[] }[]): Set<string> {
  const out = new Set<string>();
  for (const m of missions) {
    if (m.state !== "running") continue;
    for (let i = m.hops.length - 1; i >= 0; i -= 1) {
      const h = m.hops[i];
      if (h.kind === "hop" && h.roleKey) {
        if (h.state === "working" || h.state === "thinking" || h.state === "speaking") out.add(h.roleKey);
        break;
      }
    }
  }
  return out;
}

/** Agents something waits on you for: the face an approval or plan is attributed to. */
export function waitingOn(items: AttentionItem[]): Set<string> {
  return new Set(items.filter((a) => a.kind === "approval" && a.roleKey).map((a) => a.roleKey as string));
}

export type CommandCenterProps = {
  projectId: string;
  orgId?: string;
  actor: string;
  /** The server's first paint (same view the poll reads), or null when it could not be read. */
  initial: CommandCenterView | null;
  /** The same first paint as the API sent it (page.tsx may not import the parser). */
  initialWire?: unknown;
  /** Why the server could not read it. */
  initialError?: string | null;
  /** The zone the server read in (the `boss_tz` cookie, else UTC). */
  tz?: string;
  viewer?: string | null;
  /** Ask BOSS, laid out as the hero composer (page.tsx mounts it). */
  composer?: React.ReactNode;
  /** What you can do here (the full help card) for the `?` popover. */
  help?: React.ReactNode;
  /** The Factory's apps as the server read them (wire + ETag), or null. */
  initialApps?: { wire: unknown; etag: string | null } | null;
};

export function CommandCenter({ projectId, orgId, actor, initial: given, initialWire, initialError = null,
                                tz = "UTC", viewer, composer, help, initialApps }: CommandCenterProps) {
  const [initial] = useState<CommandCenterView | null>(
    () => given ?? (initialWire ? parseCommandCenterView(initialWire) : null));
  // THE VIEWER'S ZONE: the browser knows it; remembered for the next server paint.
  const [zone, setZone] = useState(tz);
  useEffect(() => {
    const mine = viewerZone();
    if (mine !== tz) {
      try { document.cookie = `${TZ_COOKIE}=${encodeURIComponent(mine)}; path=/; max-age=31536000; samesite=lax`; } catch { /* none */ }
      setZone(mine);
    }
  }, [tz]);

  const polled = useCommandCenterView(projectId, zone, initial, viewer);
  // THE LAST GOOD VIEW STAYS: a failed read leaves what BOSS last said on screen, dimmed.
  const [view, setView] = useState<CommandCenterView | null>(initial);
  const [readAt, setReadAt] = useState<number | null>(null);
  useEffect(() => {
    if (polled.data) { setView(polled.data); setReadAt(Date.now()); }
  }, [polled.data]);
  const error = polled.error ?? (view ? null : initialError);
  const reachable = !error && !!view;
  const apps = useCcApps(projectId, initialApps);
  const [templatesOpen, setTemplatesOpen] = useState(false);

  const live = useConsoleLive();
  const visible = useVisible();
  const [reduced, setReduced] = useState(false);
  useEffect(() => { setReduced(reducedMotion()); }, []);
  const hostCritical = !!view?.host.show && view.host.severity === "critical";
  const motion = motionOf({ paused: !live, hidden: !visible, reachable, hostCritical, reduced });
  const host = useHostAdvice(projectId, 60_000, view?.host ?? { show: false });
  const now = useNow(30_000);

  const status = statusOf(view, reachable, readAt, !live);
  const statusTip = !reachable && error ? `${status.tip} · ${error}` : status.tip;

  const rootRef = useRef<HTMLDivElement>(null);
  // THE LOADING BAR ENDS WHEN THIS IS ON SCREEN: once, when the instance you see has a view.
  const announced = useRef(false);
  useEffect(() => {
    const root = rootRef.current;
    if (announced.current || !view || !root) return undefined;
    const say = () => {
      if (announced.current || root.getClientRects().length === 0) return announced.current;
      announced.current = true;
      announcePainted("overview");
      return true;
    };
    if (say() || typeof ResizeObserver === "undefined") return undefined;
    const watch = new ResizeObserver(() => { if (say()) watch.disconnect(); });
    watch.observe(root);
    return () => watch.disconnect();
  }, [view]);

  // Announced on a change of state, never on a poll.
  const [said, setSaid] = useState("");
  const lastKey = useRef(status.key);
  useEffect(() => {
    if (status.key !== lastKey.current) { lastKey.current = status.key; setSaid(status.text); }
  }, [status.key, status.text]);

  const [helpOpener, setHelpOpener] = useState<HTMLElement | null>(null);
  const helpOpen = helpOpener !== null;
  const helpId = useId();
  const closeHelp = useCallback(() => setHelpOpener(null), []);

  const [gone, setGone] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!view) return;
    setGone((g) => {
      const ids = new Set(view.attention.map((a) => a.id));
      const next = new Set([...g].filter((id) => ids.has(id)));
      return next.size === g.size ? g : next;
    });
  }, [view]);
  const attention = useMemo(() => (view?.attention ?? []).filter((a) => !gone.has(a.id)), [view, gone]);
  const { lead, olderStopped } = needsOrder(view, attention);
  const waiting = useMemo(() => waitingOn(attention), [attention]);
  const working = useMemo(() => new Set<string>(), []);
  const refresh = useCallback(() => { polled.refresh(); apps.refresh(); }, [polled, apps]);

  const composeRef = useRef<HTMLDetailsElement>(null);
  const startWork = useCallback(() => {
    if (composeRef.current) composeRef.current.open = true;
    const box = document.querySelector<HTMLTextAreaElement>('[data-testid="askboss-input"]');
    box?.scrollIntoView({ block: "center" });
    box?.focus();
  }, []);

  const action = nextAction(view, attention, apps.error ? null : apps.data, reachable, apps.error);
  const sys = { tone: reachable ? "green" : "grey", text: reachable ? "BOSS connected" : "Can't reach BOSS" };
  const lastWork = view?.pulse?.lastActAt ?? view?.today.lastWorkAt ?? null;
  // WORKING NOW while anything moves (independent test: "last work 22d ago" beside a run
  // that was working): open work on any agent, or a live run that is not stalled.
  const workingNow = !!view && (view.counts.working > 0 || view.missions.some((m) => m.state === "running"));
  const lastWorkWord = workingNow ? "working now" : ago(lastWork, now) ? `last work ${ago(lastWork, now)} ago` : "";
  const firstRun = !!view && !view.today.everRan;
  const appRows = apps.data;
  // NO HISTORY: no app built, no agent step in 14 weeks.
  const fresh = !!view && (view.pulse?.total ?? 0) === 0 && (appRows?.length ?? 0) === 0;
  const proof = proofOf(appRows, view?.counts.agents ?? 0, (view?.teams ?? []).filter((t) => t.members.length).length);

  const nowCards = (
      <div className="cc-now" data-fresh={fresh ? "true" : undefined}>
        <NeedsCard view={view} lead={lead} olderStopped={olderStopped} needsYou={view ? view.counts.needsYou : null}
                   projectId={projectId} actor={actor} orgId={orgId} now={now} reachable={reachable}
                   onGone={(id) => setGone((g) => new Set(g).add(id))} onRefresh={refresh} />
        <FlightCard view={view} projectId={projectId} actor={actor} orgId={orgId} now={now}
                    onRefresh={refresh} onStart={startWork} />
        <HealthCard view={view} reachable={reachable} orgId={orgId} />
      </div>
  );

  return (
    <div className="cc" ref={rootRef} data-testid="command-center" data-reachable={reachable ? "true" : "false"}
         data-stale={reachable ? undefined : "true"} data-motion={motion}>
      {/* The only banner: a critical host, in its own words. */}
      {host.show && host.severity === "critical" ? <HostAdviceBanner advice={host} /> : null}

      <section className="cc-hero cc-workspace" aria-label="Next action" data-testid="cc-hero" data-next={action.kind}>
        <div className="cc-hero-bg" aria-hidden><div className="cc-aurora" /></div>
        <div className="cc-hero-l">
          <div className="cc-eyebrow" data-tone={sys.tone} data-key={status.key} data-testid="cc-status" title={statusTip}>
            <span className="cc-dot" data-tone={sys.tone} aria-hidden />
            <span {...(status.key === "unreachable" ? { "data-testid": "cc2-load-error" } : {})}>{sys.text}</span>
            {status.key !== "unreachable" && status.key !== "quiet" ? (
              <><span aria-hidden>·</span><span data-testid="cc-status-text">{status.text}</span></>
            ) : null}
            {lastWorkWord && reachable ? <><span aria-hidden>·</span><span data-testid="cc-last-work">{lastWorkWord}</span></> : null}
            {status.paused ? (
              <span className="cc-chip" data-testid="cc-paused" title="Live updates are paused. Press Live in the top bar.">Paused</span>
            ) : null}
            <span className="ag-sr" role="status" aria-live="polite">{said}</span>
            {help ? (
              <div className="cc-help">
                <button type="button" className="cc-help-btn" aria-expanded={helpOpen} aria-haspopup="dialog"
                        aria-controls={helpOpen ? helpId : undefined}
                        aria-label="What you can do here" title="What you can do here" data-testid="cc-help"
                        onClick={(event) => setHelpOpener(helpOpen ? null : event.currentTarget)}>?</button>
                {helpOpener ? <HelpPopover id={helpId} opener={helpOpener} onClose={closeHelp}>{help}</HelpPopover> : null}
              </div>
            ) : null}
          </div>
          <div className="cc-next" data-testid="cc-next-action" data-kind={action.kind}>
            <span className="cc-next-label">{action.kind === "active" ? "Current work" : action.kind === "result" ? "Ready to review" : "Next action"}</span>
            <h1 className="cc-headline" data-user-text>{action.title}</h1>
            <p className="cc-next-detail" data-user-text>{action.detail}</p>
            {action.patch ? <Go className="cc-btn primary" patch={action.patch} orgId={orgId}
              testId="cc-next-go">{action.label}</Go> : action.label ? (
              <button type="button" className="cc-btn primary" data-testid="cc-next-go" onClick={refresh}>{action.label}</button>
            ) : null}
          </div>
          <details className="cc-compose-disclosure" ref={composeRef} open={action.kind === "empty"} data-testid="cc-compose-disclosure">
            <summary>{action.kind === "empty" ? "Your task" : "Describe another task"}</summary>
          <div className="cc-compose" id="cc-compose">
            {/* Keyed: it is an element the server page made, rendered here among siblings. */}
            <React.Fragment key="composer">{composer}</React.Fragment>
            {firstRun ? <OnboardingHelpRows onMore={(opener) => setHelpOpener(opener)} expanded={helpOpen} controls={helpId} /> : null}
          </div>
          </details>
        </div>
      </section>

      <ReturningBrief view={view} projectId={projectId} orgId={orgId} viewer={viewer ?? actor}
                      reachable={reachable} visible={visible} container={rootRef} />
      {nowCards}
      <AppsShelf apps={appRows} attention={attention} orgId={orgId} />
      <details className="cc-secondary" data-testid="cc-template-details"
               onToggle={(e) => setTemplatesOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary title="Brief templates start a new app; app connectors add a feature such as sign-in or payments">Browse templates</summary>
        {/* BOTH KINDS, GROUPED: the briefs (a whole app) and the app connectors (code templates
            such as login-oauth or Stripe), which were only under New build › From a template. */}
        <TemplatesShelf />
        {/* Read only once opened: a closed section fetches nothing (console latency budget). */}
        {templatesOpen ? <ConnectorsShelf projectId={projectId} /> : null}
      </details>
      {view ? (
        <details className="cc-secondary" data-testid="cc-team-details">
          <summary>{view.counts.agents} configured roles · {reachable ? `${view.counts.working} observed working` : "current activity unavailable"}</summary>
          <p className="cc-secondary-note">Configured roles; activity comes from records.</p>
          <Proof n={proof} orgId={orgId} />
          <TeamGrid teams={view.teams} agents={view.agents} waiting={waiting} working={working} orgId={orgId}
                    still={motion !== "full"} />
        </details>
      ) : null}
      {fresh ? null : (
        <PulseRow pulse={view?.pulse ?? null} week={view?.week ?? null} prsOpen={proof ? proof.prs : null}
                  now={now} orgId={orgId} />
      )}
    </div>
  );
}
