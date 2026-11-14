"use client";

/**
 * T3 — NEW WORK FROM A TICKET (BOSSAPP-1047 Phase C, wireframe TicketNewWork.dc.html). A key
 * (LEND-142), a ticket link, or #311 in the words is read through GET /v1/tickets/resolve (every
 * connected system of the project), and the first ticket shows as its brief card under the box:
 * type · priority · status, the acceptance criteria, what else was read, and the workflow picked
 * from its type. The modal's Start then imports it (when BOSS does not have it yet) and starts it
 * from the ticket, one press (lib/tickets importAndStart).
 */
import React, { useEffect, useRef, useState } from "react";
import { CheckCircle, MagnifyingGlass } from "@phosphor-icons/react";

import { TYPE_WORDS, readResolve, workflowFor, type Ticket, type TypedError } from "../../lib/tickets";
import { TicketCard, TkReadState, WorkflowPick } from "../tickets/ticket-parts";
import "../tickets/ticket-run.css";

/** A ticket-ish reference in the words: a KEY-12, a link, or #311. */
export const TICKET_REF = /\b[A-Z][A-Z0-9_]{1,9}-\d+\b|https?:\/\/\S+\/\S+|(?:^|\s)#\d+\b/;
export const hasTicketRef = (words: string) => TICKET_REF.test(words || "");

export type Resolved = { state: "idle" | "reading" | "ready" | "failed"; ticket: Ticket | null; errors: TypedError[]; error: string; denied: boolean };
const IDLE: Resolved = { state: "idle", ticket: null, errors: [], error: "", denied: false };

/** Read the words' first ticket, ~600 ms after typing stops, only when they name one. */
export function useResolvedTicket(projectId: string, words: string, enabled = true, ms = 600): Resolved {
  const [r, setR] = useState<Resolved>(IDLE);
  const turn = useRef(0);
  useEffect(() => {
    const text = words.trim();
    if (!enabled || !projectId || !hasTicketRef(text)) { setR(IDLE); return; }
    const mine = ++turn.current;
    const t = setTimeout(() => {
      setR((x) => ({ ...x, state: "reading" }));
      void readResolve(projectId, text).then((res) => {
        if (mine !== turn.current) return;
        if (!res.ok) { setR({ state: res.missing ? "idle" : "failed", ticket: null, errors: [], error: res.missing ? "" : res.error, denied: res.denied }); return; }
        setR({ state: "ready", ticket: res.data.tickets[0] ?? null, errors: res.data.errors, error: "", denied: false });
      });
    }, ms);
    return () => clearTimeout(t);
  }, [projectId, words, enabled, ms]);
  return r;
}

/** The card under the box: the ticket BOSS read, with the workflow its type calls for. */
export function NewWorkTicket({ resolved, workflow, onWorkflow }: {
  resolved: Resolved; workflow: string; onWorkflow: (v: string) => void;
}) {
  if (resolved.state === "idle") return null;
  if (resolved.state === "reading" && !resolved.ticket) {
    return <TkReadState read={null} what="the ticket" rows={1} testid="px-nw-ticket" />;
  }
  if (resolved.state === "failed") {
    return <TkReadState read={{ ok: false, error: resolved.error, code: "", status: 0, denied: resolved.denied, missing: false }} what="Reading the ticket" testid="px-nw-ticket" />;
  }
  const t = resolved.ticket;
  if (!t) {
    return resolved.errors.length ? (
      <p className="tk-error" role="alert" data-testid="px-nw-ticket-errors">
        {resolved.errors.map((e) => `${e.ref ? `${e.ref}: ` : ""}${e.message || "could not be read."}`).join(" · ")}</p>
    ) : null;
  }
  const picked = workflow || workflowFor(t.type);
  return (
    <TicketCard ticket={t} testid="px-nw-ticket">
      <span className="tk-row" style={{ gap: 6 }}>
        <span className="tk-muted">Workflow:</span>
        <WorkflowPick value={picked} onChange={onWorkflow} testid="px-nw-ticket-workflow" />
        <span className="tk-faint">· picked from {TYPE_WORDS[t.type]}</span>
      </span>
      <span className="tk-spacer" />
      {t.in_boss ? <span className="tk-tag" data-tone="green" data-testid="px-nw-ticket-in-boss" title="BOSS already has this ticket; Start starts its work item">Already in BOSS</span> : null}
      <span className="tk-ok" style={{ fontSize: "var(--fs-meta)" }} data-testid="px-nw-ticket-updates"><CheckCircle size={12} weight="fill" aria-hidden /> Updates {t.key}</span>
      {resolved.errors.length ? <span className="tk-faint" data-testid="px-nw-ticket-errors">{resolved.errors.length} other reference{resolved.errors.length === 1 ? "" : "s"} could not be read</span> : null}
    </TicketCard>
  );
}

/** "BOSS recognises: LEND-142 · …/browse/LEND-142 · #311" and the way to browse instead. */
export function RecognisesLine({ onBrowse }: { onBrowse: () => void }) {
  return (
    <p className="tk-recog" data-testid="px-nw-recognises">
      <span>BOSS recognises</span>
      <code>LEND-142</code><code>…/browse/LEND-142</code><code>BOSSAPP-812</code><code>#311</code>
      <span className="tk-spacer" />
      <button type="button" className="tk-linkbtn" onClick={onBrowse} data-testid="px-nw-browse"
              title="Browse your ticket system's projects and epics, and import from there">
        <MagnifyingGlass size={12} aria-hidden /> Don&apos;t know the key? Browse tickets</button>
    </p>
  );
}
