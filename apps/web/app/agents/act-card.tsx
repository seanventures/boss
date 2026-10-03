"use client";

/**
 * The Act card: one piece of work, signed by the agent that did it (spec §3.5).
 *
 * Who did it, who handed it over and how, why, with what scope, who is accountable, and what it
 * took. Opened from any face on a chain hop, a build stage or a dock citation (`ActFace`), and
 * shown inline in a step's drawer (`ActCard inline`).
 *
 * THE HOW IS THE SERVER'S WORDS. `act.edgeLabel` is rendered as it arrives; this file never
 * composes one (tests/act-labels-are-truthful.test.tsx feeds every `how` through and compares).
 * Labels, not sentences: every explanation is a `data-tip`.
 */
import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import type { Act } from "../../lib/agent-world-types";
import { openDockAbout, type DockSubject } from "../../lib/dock-subject";
import { shortLabel, whoDid, actState } from "./chain-model";
import { AiBadge, nameOf, RunFace, StateWord } from "./run-face";

/** A URL in this console with `patch` applied, keeping the org and project the page is on. */
export function consoleHref(patch: Record<string, string>): string {
  try {
    const url = new URL(window.location.href);
    for (const k of ["agent", "dock_ask", "dock_role", "dock_role_key", "dock_act", "dock_mode", "role", "tab", "wfr", "run"]) {
      url.searchParams.delete(k);
    }
    for (const [k, v] of Object.entries(patch)) url.searchParams.set(k, v);
    return `${url.pathname}?${url.searchParams.toString()}`;
  } catch {
    return `?${new URLSearchParams(patch).toString()}`;
  }
}

/** How long the work took; "running" while it still is (no clock read in render). */
export function tookLabel(act: Pick<Act, "startedAt" | "endedAt">): string {
  const start = Date.parse(act.startedAt);
  if (!Number.isFinite(start)) return "";
  if (!act.endedAt) return "running";
  const end = Date.parse(act.endedAt);
  if (!Number.isFinite(end)) return "";
  const s = Math.max(0, Math.round((end - start) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function kilo(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n);
}

/** What the dock is asked about for this work: its step, its build, or nothing in particular. */
export function subjectOf(act: Act): DockSubject | null {
  const l = act.links;
  if (l.workflow_run_id && l.step_id) return { kind: "workflow_step", id: l.workflow_run_id, step: l.step_id };
  if (l.code_task_run_id) return { kind: "code_task", id: l.code_task_run_id };
  if (l.workflow_run_id) return { kind: "workflow_run", id: l.workflow_run_id };
  if (l.decision_id) return { kind: "decision", id: l.decision_id };
  return null;
}

/** Open the dock addressed to the agent that did this work, scoped to the work itself. */
export function askAboutAct(act: Act, question = ""): void {
  const named = act.roleKey ? nameOf(act.roleKey) : null;
  openDockAbout({
    subject: subjectOf(act), question, role: named ? named.role : null,
    roleKey: act.roleKey, actId: act.actId, history: "push",
  });
}

function Row({ label, tip, children, testId }: { label: string; tip?: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="ag-act-row" data-testid={testId}>
      <dt data-tip={tip}>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** The card itself. `inline` drops the popover chrome for the step drawer. */
export function ActCard({ act, viewer, inline, onClose }: {
  act: Act; viewer: string; inline?: boolean; onClose?(): void;
}) {
  const who = whoDid(act, viewer);
  const named = nameOf(who.roleKey, who.variant, who.personId);
  const state = actState(act);
  const handedName = act.handedBy.kind === "boss" ? "BOSS"
    : act.handedBy.kind === "person"
      ? (act.handedBy.id && act.handedBy.id === viewer ? "You" : act.handedBy.id || "A person")
      : nameOf(act.handedBy.id ?? null).name;
  const handedVariant = act.handedBy.kind === "boss" ? "boss"
    : act.handedBy.kind === "person" ? (act.handedBy.id === viewer ? "you" : "person") : "agent";
  const considered = act.considered.map((k) => nameOf(k).name);
  const howTip = [considered.join(", "), act.why].filter(Boolean).join(" · ");
  const took = tookLabel(act);
  const cost = act.costUsd ? `$${act.costUsd.toFixed(act.costUsd >= 1 ? 2 : 3)}` : "";
  const turnsHref = act.links.code_task_run_id && act.kind === "stage"
    ? consoleHref({ area: "activity", run: act.links.code_task_run_id })
    : act.links.workflow_run_id ? consoleHref({ area: "activity", wfr: act.links.workflow_run_id }) : null;
  return (
    <section className="ag-act" data-inline={inline ? "true" : undefined} data-testid="ag-act-card"
             data-act-id={act.actId} aria-label={`${named.name}: ${shortLabel(act.label)}`}>
      <header className="ag-act-head">
        <RunFace roleKey={who.roleKey} variant={who.variant} personId={who.personId} size={48} state={state} />
        <span className="ag-act-who">
          <span className="ag-act-name">
            <strong data-testid="ag-act-name">{named.name}</strong>
            {who.roleKey ? <AiBadge /> : null}
          </span>
          {named.callsign && named.role !== named.name ? <span className="ag-act-role">{named.role}</span> : null}
          {named.tagline ? <span className="ag-act-tagline" data-tip={named.tagline}>{named.tagline}</span> : null}
        </span>
        <StateWord state={state} />
        {onClose ? (
          <button type="button" className="ag-act-close" onClick={onClose} aria-label="Close" data-tip="Close">×</button>
        ) : null}
      </header>
      <span className="ag-act-label" data-tip={act.label} data-user-text>{shortLabel(act.label)}</span>
      <dl className="ag-act-rows">
        <Row label="Handed by" testId="ag-act-handed">
          <span className="ag-act-inline">
            <RunFace roleKey={act.handedBy.kind === "agent" ? act.handedBy.id ?? null : null}
                     variant={handedVariant} size={20} />
            {handedName}
          </span>
        </Row>
        {act.edgeLabel ? (
          <Row label="How" testId="ag-act-how">
            <span className="ag-act-how" data-testid="ag-act-how-label" data-tip={howTip || undefined}>
              {act.edgeLabel}
            </span>
            {act.reconstructed ? (
              <span className="ag-act-recon" data-tip="Rebuilt from older records; the handoff order may be approximate.">
                Reconstructed
              </span>
            ) : null}
          </Row>
        ) : null}
        {act.why ? (
          <Row label="Why">
            <span className="ag-act-why" data-tip={act.why}>{act.why}</span>
          </Row>
        ) : null}
        {act.scope.skills.length || act.scope.mcp.length || act.scopeOk === false ? (
          <Row label="Scope" testId="ag-act-scope">
            <span className="ag-act-chips">
              {act.scope.skills.map((s) => <span className="ag-chip" key={`s:${s}`} data-tip="Skill">{s}</span>)}
              {act.scope.mcp.map((m) => <span className="ag-chip" data-kind="mcp" key={`m:${m}`} data-tip="MCP server">{m}</span>)}
              {act.scopeOk === false ? (
                <span className="ag-chip" data-kind="wide" data-testid="ag-act-wide"
                      data-tip="Had skills the step that handed it work didn't.">Wider than its parent</span>
              ) : null}
            </span>
          </Row>
        ) : null}
        {act.accountable ? (
          <Row label="Accountable" tip="The person this work answers to.">
            {act.accountable === viewer ? "You" : act.accountable}
          </Row>
        ) : null}
        {took || cost ? (
          <Row label="Time · cost">
            <span data-tip={act.tokensIn || act.tokensOut ? `${kilo(act.tokensIn)} in · ${kilo(act.tokensOut)} out` : undefined}>
              {[took, cost].filter(Boolean).join(" · ")}
            </span>
          </Row>
        ) : null}
        {act.version ? (
          <Row label="Version" tip="Changes when its voice, remit or skills change.">v{act.version}</Row>
        ) : null}
      </dl>
      <footer className="ag-act-foot">
        <button type="button" className="ag-act-btn" data-testid="ag-act-ask"
                data-tip={`Opens the dock with ${named.name}, about this work`}
                onClick={() => { askAboutAct(act, "What did you do here, and why?"); onClose?.(); }}>
          Ask about this
        </button>
        {turnsHref ? <a className="ag-act-link" href={turnsHref} data-testid="ag-act-turns">Turns ↗</a> : null}
        {who.roleKey ? (
          <a className="ag-act-link" href={consoleHref({ area: "workforce", role: who.roleKey })}
             data-testid="ag-act-agent">Open agent ↗</a>
        ) : null}
      </footer>
    </section>
  );
}

/** Where a popover goes: under its face, or over it near the bottom, never off the screen. */
function place(trigger: HTMLElement, card: HTMLElement): { left: number; top: number } {
  const r = trigger.getBoundingClientRect();
  const vw = window.innerWidth || 1024;
  const vh = window.innerHeight || 768;
  const w = card.offsetWidth || 320;
  const h = card.offsetHeight || 280;
  const left = Math.max(16, Math.min(vw - w - 16, r.left + r.width / 2 - w / 2));
  const below = r.bottom + 8;
  const top = below + h > vh - 8 && r.top - h - 8 > 8 ? r.top - h - 8 : below;
  return { left: Math.round(left), top: Math.round(top) };
}

/**
 * A face that opens its work's card. Fixed-positioned, so a lane or a strip that scrolls inside
 * itself cannot clip it. Escape and a click elsewhere close it.
 */
export function ActFace({ act, viewer, size = 24, label, children }: {
  act: Act; viewer: string; size?: 20 | 24 | 32 | 48; label?: string; children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const id = useId();
  const who = whoDid(act, viewer);
  const named = nameOf(who.roleKey, who.variant, who.personId);
  const close = useCallback(() => { setOpen(false); button.current?.focus(); }, []);
  useLayoutEffect(() => {
    if (open && button.current && card.current) setAt(place(button.current, card.current));
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!card.current?.contains(t) && !button.current?.contains(t)) setOpen(false);
    };
    const onMove = () => { if (button.current && card.current) setAt(place(button.current, card.current)); };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open, close]);
  return (
    <>
      <button type="button" ref={button} className="ag-face-btn" data-testid="ag-act-face"
              data-act-id={act.actId} aria-expanded={open} aria-controls={open ? id : undefined}
              aria-label={`${label ?? named.name}: ${shortLabel(act.label)}`}
              data-tip={`${named.name}${act.edgeLabel ? `\n${act.edgeLabel}` : ""}`}
              onClick={() => setOpen((v) => !v)}>
        <RunFace roleKey={who.roleKey} variant={who.variant} personId={who.personId} size={size} state={actState(act)} />
        {children}
      </button>
      {open ? (
        <div ref={card} id={id} role="dialog" aria-label={`${named.name}'s work`} className="ag-act-pop"
             style={at ? { left: at.left, top: at.top } : { visibility: "hidden" }}>
          <ActCard act={act} viewer={viewer} onClose={close} />
        </div>
      ) : null}
    </>
  );
}
