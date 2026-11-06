"use client";

/**
 * THE RECOMMENDED ROADMAP (design §14; BOSSAPP-911). What the agents learned while working,
 * turned into ranked cards: the title, its kind, value / effort / risk as three dots each, the
 * evidence as chips (each opens what it cites), the squad that would do it as faces — and three
 * buttons: Accept (as a ticket, epic or PRD panel), Not now, Reject (a reason is required; it
 * teaches the detector). The "why" is one tap away (ⓘ), never on the card.
 *
 * The endpoints land with stream 911; until then the route answers 404 and this says so.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import {
  MISSING, acceptRec, parseRoadmap, polled, rejectRec, roadmapPath, snoozeRec, type Level3, type Recommendation,
} from "../../lib/ambient";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { evidenceHref } from "./drawer";

const DOTS: Record<Level3, number> = { low: 1, medium: 2, high: 3 };
const KIND_WORDS: Record<string, string> = {
  hardening: "Hardening", test_gap: "Test gap", refactor: "Refactor", feature: "Feature", design_fix: "Design fix", cost: "Cost", process: "Process",
};

function Dots({ label, level, invert = false }: { label: string; level: Level3; invert?: boolean }) {
  const n = DOTS[level];
  const tone = invert ? (n === 1 ? "ok" : n === 2 ? "warn" : "bad") : (n === 3 ? "ok" : n === 2 ? "warn" : "quiet");
  return (
    <span className="aa-dots" title={`${label}: ${level}`} data-testid={`aa-rec-${label.toLowerCase()}`} data-level={level}>
      <span className="aa-dots-k">{label}</span>
      {[1, 2, 3].map((i) => <span key={i} className="aa-dot" data-on={i <= n || undefined} data-tone={tone} />)}
    </span>
  );
}

function RecCard({ rec, projectId, orgId, onDone }: { rec: Recommendation; projectId: string; orgId?: string | null; onDone: (id: string) => void }) {
  const [mode, setMode] = useState<"idle" | "accept" | "reject">("idle");
  const [reason, setReason] = useState("");
  const [why, setWhy] = useState(false);
  const act = async (fn: () => Promise<{ ok: boolean; error: string | null }>, said: string) => {
    const r = await fn();
    showToast(r.ok ? "success" : "error", r.ok ? said : `Not done: ${r.error}`);
    if (r.ok) onDone(rec.id);
  };
  return (
    <article className="aa-rec" data-testid="aa-rec" data-rec={rec.id}>
      <header className="aa-rec-h">
        <span className="aa-badge" data-tone="type">{KIND_WORDS[rec.kind] ?? rec.kind}</span>
        <span className="aa-rec-score" title={`Score ${rec.score.toFixed(1)} · confidence ${Math.round(rec.confidence * 100)}%`}>{rec.score.toFixed(1)}</span>
        <button type="button" className="aa-why" aria-expanded={why} title="Why BOSS recommends it" onClick={() => setWhy((v) => !v)}>ⓘ</button>
      </header>
      <h3 data-user-text>{rec.title}</h3>
      {why ? <p className="aa-rec-why" data-user-text data-testid="aa-rec-why">{rec.why}</p> : null}
      <div className="aa-rec-dots">
        <Dots label="Value" level={rec.value} />
        <Dots label="Effort" level={rec.effort} invert />
        <Dots label="Risk" level={rec.risk} invert />
      </div>
      <div className="aa-rec-ev" data-testid="aa-rec-evidence">
        {rec.evidence.map((e, i) => {
          const href = evidenceHref(e, orgId);
          return href ? <a key={i} className="aa-ev-chip" href={href} title={`${e.kind}: ${e.ref}`}>{e.label}</a>
            : <span key={i} className="aa-ev-chip" title={`${e.kind}: ${e.ref}`}>{e.label}</span>;
        })}
        <span className="aa-rec-squad" title="Who would do it">
          {rec.squad.map((p) => <AgentAvatar key={p} agent={p.replace(/^agent:/, "")} size={20} aiBadge={false} />)}
        </span>
      </div>
      {mode === "accept" ? (
        <div className="aa-rec-actions" data-testid="aa-rec-accept-as">
          {(["ticket", "epic", "prd"] as const).map((as) => (
            <GatedButton key={as} permission="work_item:manage" projectId={projectId} className={`ag-btn${as === rec.proposed_shape ? " primary" : ""}`}
                         data-testid={`aa-rec-accept-${as}`} onClick={() => void act(() => acceptRec(projectId, rec.id, as), `Accepted as ${as === "prd" ? "a PRD panel" : as === "epic" ? "an epic" : "a ticket"}`)}>
              {as === "prd" ? "PRD panel" : as[0].toUpperCase() + as.slice(1)}
            </GatedButton>
          ))}
          <button type="button" className="ag-btn" onClick={() => setMode("idle")}>Cancel</button>
        </div>
      ) : mode === "reject" ? (
        <form className="aa-rec-actions" data-testid="aa-rec-reject-form"
              onSubmit={(e) => { e.preventDefault(); if (reason.trim()) void act(() => rejectRec(projectId, rec.id, reason.trim()), "Rejected — BOSS learns from it"); }}>
          <input aria-label="Reason" placeholder="Why not? (teaches BOSS)" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} required />
          <GatedButton permission="work_item:manage" projectId={projectId} type="submit" className="ag-btn danger" disabled={!reason.trim()}
                       data-testid="aa-rec-reject-confirm">Reject</GatedButton>
          <button type="button" className="ag-btn" onClick={() => setMode("idle")}>Cancel</button>
        </form>
      ) : (
        <div className="aa-rec-actions">
          <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn primary" data-testid="aa-rec-accept"
                       title="Make it real work" onClick={() => setMode("accept")}>Accept</GatedButton>
          <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn" data-testid="aa-rec-snooze"
                       title="Hide for two weeks" onClick={() => void act(() => snoozeRec(projectId, rec.id), "Snoozed for two weeks")}>Not now</GatedButton>
          <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn" data-testid="aa-rec-reject"
                       title="Not useful — say why" onClick={() => setMode("reject")}>Reject</GatedButton>
        </div>
      )}
    </article>
  );
}

export function Roadmap({ projectId, orgId }: { projectId: string; orgId?: string | null }) {
  const live = useConsoleLive();
  const [gone, setGone] = useState<Set<string>>(new Set());
  const read = usePolledRead({ key: roadmapPath(projectId), read: polled(roadmapPath(projectId), parseRoadmap), pollMs: 60_000, paused: !live, enabled: !!projectId });
  if (read.error === MISSING) {
    return <div className="aa-empty" data-testid="aa-roadmap-missing"><b>Roadmap is coming</b><span>Recommendations arrive with the learning stream (BOSSAPP-911).</span></div>;
  }
  const recs = (read.data?.recommendations ?? []).filter((r) => !gone.has(r.id) && r.status === "open");
  if (read.data && !recs.length) return <div className="aa-empty" data-testid="aa-roadmap-empty"><b>Nothing recommended</b><span>Agents add ideas here as they learn.</span></div>;
  return (
    <div className="aa-recs" data-testid="aa-roadmap">
      {recs.sort((a, b) => b.score - a.score).map((r) => (
        <RecCard key={r.id} rec={r} projectId={projectId} orgId={orgId} onDone={(id) => setGone((g) => new Set(g).add(id))} />
      ))}
    </div>
  );
}
