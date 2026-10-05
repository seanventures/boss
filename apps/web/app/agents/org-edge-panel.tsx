"use client";

/**
 * The Edge panel (org-view spec §4.3): what is behind one Flow edge. Every edge opens its
 * evidence (the Honeycomb lesson): the agent pairs it folds, with how the work moved, and the
 * newest handoffs along it — each with its status word and `Open run ↗`.
 */
import React, { useEffect, useState } from "react";

import {
  ageWords, listEdgeHandoffs, type OrgBoard, type OrgDays, type OrgEdge, type OrgHandoff,
} from "../../lib/agents-org-board";
import { AgFace } from "./agent-face";
import { Drawer, consoleHref, nameOf, runHref, teamLabel } from "./org-shared";

export type EdgeFocus = { from: string; to: string; level: "team" | "agent" };

const STATUS: Record<string, string> = {
  complete: "Done", error: "Failed", awaiting_input: "Needs you", active: "Working", pending: "Not started",
  stale: "Stale", skipped: "Skipped",
};

export function teamOfRole(board: OrgBoard, roleKey: string): string {
  if (roleKey === "person") return "person";
  return board.agents.find((a) => a.roleKey === roleKey)?.team
    ?? board.teams.find((t) => t.members.includes(roleKey))?.key ?? "yours";
}

/** Activity, filtered the way Activity can be (walk D9): the receiving agent when the edge has
 *  one, and the window — Activity reads 24h or 7d, so 30d opens on 7d. */
export function activityHref(targets: string[], days: OrgDays): string {
  return consoleHref({ area: "workforce", view: "activity", layout: null, isolate: null, run: null, focus: null,
                       role: null, agent: targets.length === 1 ? targets[0] : null, days: days >= 7 ? "7" : "1" });
}

export function edgeSummary(e: Pick<OrgEdge, "handoffs" | "failed" | "waiting" | "stale">): string {
  return [`${e.handoffs} ${e.handoffs === 1 ? "handoff" : "handoffs"}`, e.failed ? `${e.failed} failed` : "",
          e.waiting ? `${e.waiting} waiting` : "", e.stale ? `${e.stale} stale` : ""].filter(Boolean).join(" · ");
}

export function OrgEdgePanel({ projectId, board, focus, days, onClose }: {
  projectId: string; board: OrgBoard; focus: EdgeFocus; days: OrgDays; onClose: () => void;
}) {
  const team = focus.level === "team";
  const label = (k: string) => (team ? teamLabel(k, board) : nameOf(k));
  const edge: OrgEdge | undefined = team
    ? board.teamEdges.find((e) => e.from === focus.from && e.to === focus.to)
    : board.agentEdges.find((e) => e.from === focus.from && e.to === focus.to);
  const pairs = team
    ? board.agentEdges.filter((e) => teamOfRole(board, e.from) === focus.from && teamOfRole(board, e.to) === focus.to)
    : (edge ? [edge] : []);
  const sources = team ? (focus.from === "person" ? ["person"] : board.teams.find((t) => t.key === focus.from)?.members ?? [])
    : [focus.from];
  const targets = team ? board.teams.find((t) => t.key === focus.to)?.members ?? [] : [focus.to];
  const [rows, setRows] = useState<OrgHandoff[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const key = `${sources.join(",")}>${targets.join(",")}:${days}`;
  useEffect(() => {
    let alive = true;
    setRows(null);
    setError(null);
    void listEdgeHandoffs(projectId, days, sources, targets, 10).then((r) => {
      if (!alive) return;
      if (r.ok) setRows(r.items); else setError(r.error);
    });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the edge and window, not every poll
  }, [projectId, key]);

  return (
    <Drawer label={`${label(focus.from)} to ${label(focus.to)}`} testid="ag-org-edge" onClose={onClose}>
      <header className="ag-org-edge-head">
        <span className="ag-org-edge-title"><span data-user-text>{label(focus.from)}</span> → <span data-user-text>{label(focus.to)}</span></span>
        {edge ? <span className="ag-muted" data-testid="ag-org-edge-summary">{edgeSummary(edge)}</span> : null}
      </header>
      {team && pairs.length ? (
        <ul className="ag-org-list ag-org-edge-pairs" aria-label="Agent pairs">
          {pairs.map((p) => (
            <li key={`${p.from}>${p.to}`} data-testid={`ag-org-edge-pair-${p.from}-${p.to}`}>
              <span data-user-text>{nameOf(p.from)}</span> → <span data-user-text>{nameOf(p.to)}</span>
              <span>×{p.handoffs}</span>
              {p.label ? <span className="ag-muted" data-user-text>{p.label}</span> : null}
              {p.failed ? <span className="ag-bad">{p.failed} failed ✕</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
      <ul className="ag-org-list ag-org-edge-rows" aria-label="Last handoffs" data-testid="ag-org-edge-rows">
        {rows === null && !error ? <li className="ag-muted">Loading</li> : null}
        {error ? <li className="ag-bad" title={error}>Can&apos;t reach BOSS</li> : null}
        {rows?.map((r) => (
          <li key={r.actId} data-status={r.status}>
            <span className="ag-muted" title={r.startedAt ? new Date(r.startedAt).toLocaleString() : undefined}>
              {ageWords(r.startedAt)}
            </span>
            {r.from === "person" ? <span className="ag-org-you" aria-hidden>●</span> : <AgFace roleKey={r.from} size={24} decorative />}
            <span aria-hidden>→</span>
            <AgFace roleKey={r.to} size={24} decorative />
            <span className="ag-sr">{nameOf(r.from)} to {nameOf(r.to)}</span>
            <span className="ag-trunc" title={r.label} data-user-text>{r.label}</span>
            <span data-status={r.status}>{STATUS[r.status] ?? r.status}</span>
            {r.runId ? <a className="ag-link" href={runHref(r.runId)}>Open run ↗</a> : null}
          </li>
        ))}
      </ul>
      <footer className="ag-org-edge-foot">
        <a className="ag-link" data-testid="ag-org-edge-activity" href={activityHref(targets, days)}
           title={targets.length === 1 ? `${nameOf(targets[0])}'s work in Activity` : "Everything agents did, newest first"}>
          All in Activity ↗
        </a>
      </footer>
    </Drawer>
  );
}
