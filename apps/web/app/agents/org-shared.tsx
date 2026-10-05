"use client";

/**
 * Organization's shared parts (org-view spec §4): names, the links every `↗` uses, a drawer that
 * traps focus and returns it, and the small menus. One place each, so Teams, Flow and the two
 * panels say a thing the same way.
 */
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "@phosphor-icons/react";

import type { AvatarState } from "../../lib/agent-world-types";
import { identityOf } from "../../lib/agent-world";
import type { LiveMap } from "../../lib/agents-live";
import { TEAM_LABELS } from "../../lib/agents-live-board";
import { ageWords, openStatusWord, type OrgBoard, type OrgOpenItem } from "../../lib/agents-org-board";

/** An agent's short name: its callsign, else its name; an unregistered key, humanised. */
export function nameOf(roleKey: string): string {
  if (roleKey === "person") return "You";
  const who = identityOf(roleKey);
  return who.callsign || who.name || humanise(roleKey);
}

export function humanise(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : key;
}

export function teamLabel(key: string, board?: OrgBoard | null): string {
  if (key === "person") return "You";
  return TEAM_LABELS[key]?.label ?? board?.teams.find((t) => t.key === key)?.label ?? humanise(key);
}

export function teamTip(key: string, board?: OrgBoard | null): string | undefined {
  return TEAM_LABELS[key]?.tip ?? board?.teams.find((t) => t.key === key)?.tip ?? undefined;
}

/** A face's state from the 4s presence store; no presence is idle. */
export function liveState(live: LiveMap, roleKey: string): AvatarState {
  const p = live.get(roleKey);
  if (!p) return "idle";
  return p.inPanel ? "speaking" : p.state;
}

export const LIVE_ORDER: Partial<Record<AvatarState, number>> = {
  needs_you: 0, stuck: 1, working: 2, speaking: 3, thinking: 4,
};

export function liveFirst(roles: string[], live: LiveMap): string[] {
  return [...roles].sort((a, b) => (LIVE_ORDER[liveState(live, a)] ?? 9) - (LIVE_ORDER[liveState(live, b)] ?? 9));
}

/** The console URL with these parameters (keeps `?org=` and everything else). */
export function consoleHref(patch: Record<string, string | null>): string {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    return `${url.pathname}${url.search}`;
  } catch {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(patch)) if (v !== null) q.set(k, v);
    return `?${q.toString()}`;
  }
}

const RESET = { view: null, layout: null, days: null, isolate: null, run: null, focus: null, role: null } as const;

export function runHref(runId: string): string {
  return consoleHref({ ...RESET, area: "delivery_workflows", wfr: runId, wf: null, step: null });
}

export function reviewHref(attentionId: string): string {
  return consoleHref({ ...RESET, area: "workforce", focus: attentionId });
}

/** The Kit, opened on one ability: where an ability that must be set up first is set up. */
export function kitHref(ability: string): string {
  return consoleHref({ ...RESET, area: "skills", tab: null, ability });
}

export function workflowHref(workflowKey: string, stepId: string): string {
  return consoleHref({ ...RESET, area: "delivery_workflows", wf: workflowKey, step: stepId || null, wfr: null });
}

/** The one link an open item offers: Review a decision, else Open the run. */
export function OpenItemLink({ item, testid }: { item: OrgOpenItem; testid?: string }) {
  if (item.attentionId) {
    return <a className="ag-link" href={reviewHref(item.attentionId)} data-testid={testid}
              title="Open the decision on Live">Review ↗</a>;
  }
  if (item.runId) {
    return <a className="ag-link" href={runHref(item.runId)} data-testid={testid} title="Open the run">Open run ↗</a>;
  }
  return null;
}

/**
 * A right drawer (a bottom sheet under 720px). Focus moves in on open, Tab stays inside, and
 * focus returns to whatever opened it. Esc is the shell's (one order for the whole view).
 *
 * PORTALLED TO <body> (walk D1). The console's panel stack has a transform, which makes it the
 * containing block for `position: fixed`: the sheet sat 800px down a phone and the drawer
 * scrolled away on a desktop. In <body>, fixed means the screen — the Kit's overlays do the same
 * (app/skills/overlay.tsx).
 */
export function Drawer({ label, testid, onClose, children }: {
  label: string; testid: string; onClose: () => void; children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const opener = typeof document !== "undefined" ? document.activeElement as HTMLElement | null : null;
    const box = ref.current;
    box?.querySelector<HTMLElement>("button, a[href], [tabindex='0']")?.focus();
    return () => { if (opener && opener.isConnected) opener.focus?.(); };
  }, []);
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "Tab" || !ref.current) return;
    const items = [...ref.current.querySelectorAll<HTMLElement>("button:not(:disabled), a[href], [tabindex='0']")];
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  const drawer = (
    <div className="org-drawer" role="dialog" aria-modal="false" aria-label={label} data-testid={testid}
         ref={ref} onKeyDown={onKeyDown}>
      <button type="button" className="ag-icon-btn org-drawer-close" aria-label="Close" title="Close"
              onClick={onClose}><X size={14} weight="bold" aria-hidden /></button>
      {children}
    </div>
  );
  return typeof document === "undefined" ? drawer : createPortal(drawer, document.body);
}

/**
 * One stuck item, as two lines (walk on boss-1, 2026-10-04: a long run name ran out of its card
 * and over the next one). Line 1 is who, how long, the state and the one action — the action
 * never shrinks and never sits on text; line 2 is what (`step · run`), cut with an ellipsis and
 * whole in its tooltip. Every part may shrink to nothing but the action.
 */
export function StuckItem({ roleKey, age, ageTip, state, word, label, action, testid }: {
  roleKey: string | null; age: string; ageTip?: string; state: string; word: string; label: string;
  action: React.ReactNode; testid?: string;
}) {
  return (
    <span className="org-wait" data-testid={testid}>
      <span className="org-wait-head">
        {roleKey ? <span className="org-wait-who" data-user-text title={nameOf(roleKey)}>{nameOf(roleKey)}</span> : null}
        <span className="org-wait-age" title={ageTip}>{age}</span>
        <span className="org-wait-state" data-state={state}>{word}</span>
        <span className="org-wait-act">{action}</span>
      </span>
      <span className="org-wait-what" title={label} data-user-text>{label}</span>
    </span>
  );
}

/** An open item (a wait) as a StuckItem: Review ↗ or Open run ↗. */
export function WaitItem({ item, who = true, testid }: { item: OrgOpenItem; who?: boolean; testid?: string }) {
  return (
    <StuckItem roleKey={who ? item.roleKey : null} age={ageWords(item.since)}
               ageTip={`Open since ${item.since ? new Date(item.since).toLocaleString() : "unknown"}`}
               state={item.status} word={openStatusWord(item.status)} label={item.label}
               action={<OpenItemLink item={item} />} testid={testid} />
  );
}

/** A small menu under a button. Esc closes it before anything else (it stops the key here).
 *
 *  PORTALLED, FIXED UNDER ITS BUTTON: cards clip what overflows them (so a long name can never
 *  spill into the next card), and a menu inside a card would be clipped with it. The menu opens
 *  in <body> at its button's place, on the side with room, and follows it on scroll. */
export function MenuPop({ label, onClose, children, testid }: {
  label: string; onClose: () => void; children: React.ReactNode; testid?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const anchor = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<React.CSSProperties | null>(null);
  const place = () => {
    const wrap = anchor.current?.parentElement;
    if (!wrap || typeof window === "undefined") return;
    const r = wrap.getBoundingClientRect();
    setPos(r.left > window.innerWidth / 2
      ? { position: "fixed", top: r.bottom + 4, right: Math.max(8, window.innerWidth - r.right) }
      : { position: "fixed", top: r.bottom + 4, left: Math.max(8, r.left) });
  };
  useLayoutEffect(() => {
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => { window.removeEventListener("scroll", place, true); window.removeEventListener("resize", place); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- once per open
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>("[role='menuitem']:not(:disabled)")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopImmediatePropagation(); onClose(); }
    };
    // Capture: the menu hears Esc before the view's own order does.
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  const menu = (
    <div className="org-menu" role="menu" aria-label={label} ref={ref} data-testid={testid} style={pos ?? undefined}>
      {children}
    </div>
  );
  return (
    <>
      <span ref={anchor} hidden />
      {typeof document === "undefined" ? menu : createPortal(menu, document.body)}
    </>
  );
}
