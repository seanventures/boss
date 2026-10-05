"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
import { Proof, TeamGrid, proofOf, systemsWord } from "./command-center/hero";
import { PulseRow } from "./command-center/pulse";
import { AppsShelf, TemplatesShelf } from "./command-center/rails";
import { HostAdviceBanner, useHostAdvice } from "./host-advice";
import { OnboardingHelpRows } from "./onboarding-help-card";
import { announcePainted } from "../lib/area-painted";

/**
 * THE COMMAND CENTER (cc v2): the approved look, on real data. Top to bottom:
 *
 *   HERO      what should your team build next? — the composer, the proof row, and the team as
 *             nine tiles of faces (amber ring: waiting on you; glow: working);
 *   NOW       Needs you · In flight · Health;
 *   SHELVES   Start from a template · Built by your team — one scrolling row each;
 *   PULSE     the team's last 14 weeks and its latest moves (served by the API).
 *
 * ONE READ, GET /v1/command-center/view, plus the Factory's apps (/v1/factory/apps) for the
 * hero's numbers and the shelf. Both through the shared poll (usePolled: ETag, paused while the
 * tab is hidden or the console is Paused). Nothing here counts what the server counts.
 *
 * MOTION (the aurora, the bobbing faces, the radar) only while the console is Live, the tab is
 * visible, BOSS answers, the host is fine and the viewer has not asked for less motion; stills
 * otherwise. The parity table that maps the previous page to this one is
 * tests/command-center-parity.test.tsx.
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

  const [helpOpen, setHelpOpen] = useState(false);
  const helpRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!helpOpen) return undefined;
    const outside = (e: PointerEvent) => { if (!helpRef.current?.contains(e.target as Node)) setHelpOpen(false); };
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape") setHelpOpen(false); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [helpOpen]);

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
  const refresh = useCallback(() => { polled.refresh(); apps.refresh(); }, [polled, apps]);

  const startWork = useCallback(() => {
    const box = document.querySelector<HTMLTextAreaElement>('[data-testid="askboss-input"]');
    box?.scrollIntoView({ block: "center" });
    box?.focus();
  }, []);

  const sys = systemsWord(view?.systems ?? [], reachable);
  const lastWork = view?.pulse?.lastActAt ?? view?.today.lastWorkAt ?? null;
  const lastWorkWord = ago(lastWork, now);
  const firstRun = !!view && !view.today.everRan;
  const appRows = apps.data;
  const proof = proofOf(appRows, view?.counts.agents ?? 0, (view?.teams ?? []).filter((t) => t.members.length).length);

  return (
    <div className="cc" ref={rootRef} data-testid="command-center" data-reachable={reachable ? "true" : "false"}
         data-stale={reachable ? undefined : "true"} data-motion={motion}>
      {/* The only banner: a critical host, in its own words. */}
      {host.show && host.severity === "critical" ? <HostAdviceBanner advice={host} /> : null}

      <section className="cc-hero" aria-label="What should your team build next?" data-testid="cc-hero">
        <div className="cc-hero-bg" aria-hidden><div className="cc-aurora" /></div>
        <div className="cc-hero-l">
          <div className="cc-eyebrow" data-tone={sys.tone} data-key={status.key} data-testid="cc-status" title={statusTip}>
            <span className="cc-dot" data-tone={sys.tone} aria-hidden />
            <span {...(status.key === "unreachable" ? { "data-testid": "cc2-load-error" } : {})}>{sys.text}</span>
            {status.key !== "unreachable" && status.key !== "quiet" ? (
              <><span aria-hidden>·</span><span data-testid="cc-status-text">{status.text}</span></>
            ) : null}
            {lastWorkWord && reachable ? <><span aria-hidden>·</span><span>last work {lastWorkWord} ago</span></> : null}
            {status.paused ? (
              <span className="cc-chip" data-testid="cc-paused" title="Live updates are paused. Press Live in the top bar.">Paused</span>
            ) : null}
            <span className="ag-sr" role="status" aria-live="polite">{said}</span>
            {help ? (
              <div className="cc-help" ref={helpRef}>
                <button type="button" className="cc-help-btn" aria-expanded={helpOpen} aria-haspopup="dialog"
                        aria-label="What you can do here" title="What you can do here" data-testid="cc-help"
                        onClick={() => setHelpOpen((v) => !v)}>?</button>
                {helpOpen ? <div className="cc-help-pop" role="dialog" aria-label="What you can do here">{help}</div> : null}
              </div>
            ) : null}
          </div>
          <h1 className="cc-headline">What should your team <em>build next?</em></h1>
          <div className="cc-compose" id="cc-compose">
            {/* Keyed: it is an element the server page made, rendered here among siblings. */}
            <React.Fragment key="composer">{composer}</React.Fragment>
            {firstRun ? <OnboardingHelpRows onMore={() => setHelpOpen(true)} /> : null}
          </div>
          <Proof n={proof} orgId={orgId} />
        </div>
        {view ? (
          <TeamGrid teams={view.teams} agents={view.agents} waiting={waiting} orgId={orgId} still={motion !== "full"} />
        ) : <div className="cc-hero-r" aria-hidden />}
      </section>

      <div className="cc-now">
        <NeedsCard view={view} lead={lead} olderStopped={olderStopped} needsYou={view ? view.counts.needsYou : null}
                   projectId={projectId} actor={actor} orgId={orgId} now={now} reachable={reachable}
                   onGone={(id) => setGone((g) => new Set(g).add(id))} onRefresh={refresh} />
        <FlightCard view={view} projectId={projectId} actor={actor} orgId={orgId} now={now}
                    onRefresh={refresh} onStart={startWork} />
        <HealthCard view={view} reachable={reachable} orgId={orgId} />
      </div>

      <TemplatesShelf />
      <AppsShelf apps={appRows} orgId={orgId} />
      <PulseRow pulse={view?.pulse ?? null} week={view?.week ?? null} prsOpen={proof ? proof.prs : null} now={now} orgId={orgId} />
    </div>
  );
}
