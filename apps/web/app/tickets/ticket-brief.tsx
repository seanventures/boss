"use client";

/**
 * T4 — ASK BOSS: THE TICKET BRIEF CARD (BOSSAPP-1047 Phase C, wireframe TicketAskBoss.dc.html).
 *
 * The chat's turn records which tools it ran (`tool_calls`: tool + args, not the result), so the
 * card is built from the turn's `ticket_get` call: its `ref` (a key or link) and `connection_id`
 * when given are read again through the ticket routes (the console never trusts a ticket shape a
 * reply carries). A chat that has no tool record (the per-agent chat) passes the reply text: the
 * first ticket key in it is resolved against the project's connections.
 *
 *   Ticket brief · LEND-142                                Open in Jira ↗
 *   Second reviewer when debt-to-income is over 45%
 *   [Minor feature] [Major feature (plan first)] [Bug fix]   ✓ Comment progress · ✓ Move to In Progress
 *   ▶ Start minor feature
 *   Ticket text is treated as outside input: BOSS screens it before any agent acts on it.
 */
import React, { useEffect, useState } from "react";
import { ChatCircleText, Play, ArrowsLeftRight } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import {
  importAndStart, readConnections, readResolve, readTicket, workflowFor, workflowWords,
  type Connection, type Read, type Ticket,
} from "../../lib/tickets";
import { TicketCard, TkReadState } from "./ticket-parts";
import "./ticket-run.css";

type Call = { tool?: string; ok?: boolean; args?: Record<string, unknown> };
const KEY = /\b[A-Z][A-Z0-9_]{1,9}-\d+\b/;

/** The ticket a turn read: its last successful ticket_get (ref + connection), else a key in the words. */
export function ticketRefOf(calls: Call[] | undefined, text = ""): { ref: string; connectionId: string } | null {
  const got = [...(calls ?? [])].reverse().find((c) => c.tool === "ticket_get" && c.ok !== false && typeof c.args?.ref === "string" && c.args.ref);
  if (got) return { ref: String(got.args!.ref), connectionId: typeof got.args?.connection_id === "string" ? String(got.args.connection_id) : "" };
  const key = KEY.exec(text || "")?.[0];
  return key ? { ref: key, connectionId: "" } : null;
}

const CHOICES = ["minor_feature", "major_feature", "fast_fix"] as const;
const CHOICE_WORDS: Record<string, string> = { minor_feature: "Minor feature", major_feature: "Major feature (plan first)", fast_fix: "Bug fix" };

export function TicketBrief({ projectId, calls, text, onStarted }: {
  projectId: string; calls?: Call[]; text?: string; onStarted?: (said: string) => void;
}) {
  const found = ticketRefOf(calls, text);
  const ref = found?.ref ?? "", conn = found?.connectionId ?? "";
  const [read, setRead] = useState<Read<Ticket | null> | null>(null);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [flow, setFlow] = useState("");
  const [said, setSaid] = useState<{ text: string; tone?: "red" | "green" }>({ text: "" });
  const [busy, setBusy] = useState(false);
  const [n, setN] = useState(0);
  const startGate = useGate("workflow_run:start", projectId);
  const ticketGate = useGate("work_tracker:write", projectId);

  useEffect(() => {
    if (!ref || !projectId) return;
    let live = true;
    setRead(null);
    const go = conn ? readTicket(conn, ref).then((r): Read<Ticket | null> => r)
      : readResolve(projectId, ref).then((r): Read<Ticket | null> => (r.ok ? { ok: true, data: r.data.tickets[0] ?? null } : r));
    void go.then((r) => { if (live) setRead(r); });
    return () => { live = false; };
  }, [ref, conn, projectId, n]);

  const t = read && read.ok ? read.data : null;
  useEffect(() => {
    if (!t?.connection_id) return;
    let live = true;
    void readConnections(projectId).then((r) => {
      if (live && r.ok) setConnection(r.data.find((c) => c.id === t.connection_id) ?? null);
    });
    return () => { live = false; };
  }, [projectId, t?.connection_id]);

  if (!found) return null;
  if (!read || !read.ok) {
    // A key that is no connected system's ticket is just a word: nothing to show.
    if (read && !read.ok && read.missing) return null;
    return <div className="tk-brief"><TkReadState read={read} what={`Reading ${ref}`} onRetry={() => setN((x) => x + 1)} rows={1} testid="tk-brief" /></div>;
  }
  if (!t) return null;
  const picked = flow || workflowFor(t.type);
  const wb = connection?.settings.write_back;
  const doing = connection?.settings.status_map.doing || "In Progress";
  const start = async () => {
    setBusy(true); setSaid({ text: "" });
    const r = await importAndStart(projectId, t, picked);
    setBusy(false);
    if (!r.ok) { setSaid({ text: r.error ?? "It didn't start.", tone: "red" }); return; }
    const words = `Started ${workflowWords(picked).toLowerCase()} from ${t.key}.${r.note ? ` ${r.note}` : ""}`;
    setSaid({ text: words, tone: "green" });
    onStarted?.(words);
  };
  const can = startGate.allowed && ticketGate.allowed;
  return (
    <div className="tk-brief" data-testid="tk-brief">
      <TicketCard ticket={t} testid="tk-brief-card" heading={<b className="tk-run-h">Ticket brief ·</b>}>
        <div className="tk-brief-chips" role="group" aria-label="Workflow">
          {CHOICES.map((k) => (
            <button key={k} type="button" className="tk-optchip" aria-pressed={picked === k} onClick={() => setFlow(k)} data-testid={`tk-brief-flow-${k}`}>
              {CHOICE_WORDS[k]}</button>
          ))}
        </div>
        <div className="tk-brief-chips" aria-label="What BOSS writes back">
          <span className="tk-optchip" aria-pressed={wb ? wb.comment : true} data-testid="tk-brief-wb-comment"
                title="Set per connection in Setup › Ticket systems"><ChatCircleText size={12} aria-hidden /> Comment progress on {t.key}</span>
          <span className="tk-optchip" aria-pressed={wb ? wb.transition : true} data-testid="tk-brief-wb-move"
                title="Set per connection in Setup › Ticket systems"><ArrowsLeftRight size={12} aria-hidden /> Move to {doing}</span>
        </div>
        <span className="tk-spacer" />
        <GatedButton permission="work_tracker:write" projectId={projectId} className="tk-btn" data-kind="primary"
                     disabled={busy || !can} title={can ? `Import ${t.key} and start it` : startGate.reason || ticketGate.reason}
                     onClick={() => void start()} data-testid="tk-brief-start">
          <Play size={12} weight="fill" aria-hidden /> {busy ? "Starting…" : `Start ${workflowWords(picked).toLowerCase()}`}</GatedButton>
      </TicketCard>
      {said.text ? <p role="status" className={said.tone === "red" ? "tk-error" : "tk-ok"} data-testid="tk-brief-said">{said.text}</p> : null}
      <p className="tk-brief-note">Ticket text is treated as outside input: BOSS screens it before any agent acts on it.</p>
    </div>
  );
}
