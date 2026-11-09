"use client";

/**
 * THE RECOMMENDED ROADMAP (design §14; BOSSAPP-911). What the agents learned while working,
 * turned into ranked cards, each a small diagram (the API's strip: sources → detector →
 * recommendation): its kind, its rank, value / effort / risk as three dots each, and three
 * buttons: Accept (as a ticket, epic or PRD panel), Not now (7 / 14 / 30 days), Reject (a reason
 * and a category are required; they teach the detector). The why, the evidence chips and the
 * squad are one tap away (ⓘ, read from GET /v1/ambient/roadmap/{id}), never on the card.
 *
 * Routes: GET /v1/ambient/roadmap, POST /v1/ambient/roadmap/{id}/accept|reject|snooze.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { useConsoleLive } from "../../lib/console-live";
import { usePolledRead } from "../../lib/log-stream";
import {
  MISSING, REJECT_CATEGORIES, REJECT_WORDS, aaRead, acceptRec, parseRecDetail, parseRoadmap, polled, recPath, rejectRec,
  roadmapPath, snoozeRec, type Level3, type RecDetail, type Recommendation, type RejectCategory,
} from "../../lib/ambient";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { evidenceHref } from "./drawer";
import { FlowThumb } from "./flow-thumb";

const DOTS: Record<Level3, number> = { low: 1, medium: 2, high: 3 };
const SNOOZE_DAYS = [7, 14, 30] as const;

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

function Why({ projectId, id, orgId }: { projectId: string; id: string; orgId?: string | null }) {
  const [d, setD] = useState<RecDetail | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let live = true;
    void aaRead(recPath(projectId, id), parseRecDetail).then((r) => {
      if (!live) return;
      if (r.ok && r.data) setD(r.data); else if (!r.ok) setErr(r.error);
    });
    return () => { live = false; };
  }, [projectId, id]);
  if (err) return <p className="aa-rec-why" data-testid="aa-rec-why">Can't load why: {err}</p>;
  if (!d) return <p className="aa-rec-why" data-testid="aa-rec-why">…</p>;
  return (
    <div className="aa-rec-why" data-testid="aa-rec-why">
      <p data-user-text>{d.why}</p>
      <div className="aa-rec-ev" data-testid="aa-rec-evidence">
        {d.evidence.map((e, i) => {
          const href = evidenceHref(e, orgId);
          return href ? <a key={i} className="aa-ev-chip" href={href} title={`${e.kind}: ${e.ref}`}>{e.label}</a>
            : <span key={i} className="aa-ev-chip" title={`${e.kind}: ${e.ref}`}>{e.label}</span>;
        })}
        <span className="aa-rec-squad" title="Who would do it">
          {d.squad.map((p) => <AgentAvatar key={p} agent={p.replace(/^agent:/, "")} size={20} aiBadge={false} />)}
        </span>
      </div>
    </div>
  );
}

function RecCard({ rec, projectId, orgId, onDone }: { rec: Recommendation; projectId: string; orgId?: string | null; onDone: (id: string) => void }) {
  const [mode, setMode] = useState<"idle" | "accept" | "reject" | "snooze">("idle");
  const [reason, setReason] = useState("");
  const [category, setCategory] = useState<RejectCategory | null>(null);
  const [why, setWhy] = useState(false);
  const act = async (fn: () => Promise<{ ok: boolean; error: string | null }>, said: string) => {
    const r = await fn();
    showToast(r.ok ? "success" : "error", r.ok ? said : `Not done: ${r.error}`);
    if (r.ok) onDone(rec.id);
  };
  const canReject = !!reason.trim() && !!category;
  return (
    <article className="aa-rec" data-testid="aa-rec" data-rec={rec.id}>
      <header className="aa-rec-h">
        <span className="aa-badge" data-tone="type" title={`Found by ${rec.detectorTitle}`}>{rec.kindLabel}</span>
        <span className="aa-rec-score" data-testid="aa-rec-rank"
              title={`Rank ${rec.rank ?? "—"}, score ${rec.score.toFixed(2)}, ${rec.evidence} evidence from ${rec.sources} source${rec.sources === 1 ? "" : "s"}`}>
          #{rec.rank ?? "—"}
        </span>
        {rec.resurfaced ? <span className="aa-badge" data-tone="amber" title="Rejected before; the evidence grew">Back</span> : null}
        <button type="button" className="aa-why" aria-expanded={why} title="Why BOSS recommends it" onClick={() => setWhy((v) => !v)}>ⓘ</button>
      </header>
      <h3 data-user-text>{rec.title}</h3>
      <FlowThumb strip={rec.strip} />
      {why ? <Why projectId={projectId} id={rec.id} orgId={orgId} /> : null}
      <div className="aa-rec-dots">
        <Dots label="Value" level={rec.value} />
        <Dots label="Effort" level={rec.effort} invert />
        <Dots label="Risk" level={rec.risk} invert />
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
      ) : mode === "snooze" ? (
        <div className="aa-rec-actions" data-testid="aa-rec-snooze-for">
          {SNOOZE_DAYS.map((d) => (
            <GatedButton key={d} permission="work_item:manage" projectId={projectId} className={`ag-btn${d === 14 ? " primary" : ""}`}
                         data-testid={`aa-rec-snooze-${d}`} title={`Hide for ${d} days, then re-check the evidence`}
                         onClick={() => void act(() => snoozeRec(projectId, rec.id, d), `Hidden for ${d} days`)}>{d}d</GatedButton>
          ))}
          <button type="button" className="ag-btn" onClick={() => setMode("idle")}>Cancel</button>
        </div>
      ) : mode === "reject" ? (
        <form className="aa-rec-actions aa-rec-reject" data-testid="aa-rec-reject-form"
              onSubmit={(e) => { e.preventDefault(); if (canReject) void act(() => rejectRec(projectId, rec.id, reason.trim(), category!), "Rejected, BOSS learns from it"); }}>
          <span className="aa-rec-cats" role="radiogroup" aria-label="Why not">
            {REJECT_CATEGORIES.map((c) => (
              <button key={c} type="button" role="radio" aria-checked={category === c} className="aa-ev-chip aa-cat"
                      data-testid={`aa-rec-cat-${c}`} title={REJECT_WORDS[c].tip} onClick={() => setCategory(c)}>{REJECT_WORDS[c].label}</button>
            ))}
          </span>
          <input aria-label="Reason" placeholder="Why not? (teaches BOSS)" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} required />
          <GatedButton permission="work_item:manage" projectId={projectId} type="submit" className="ag-btn danger" disabled={!canReject}
                       data-testid="aa-rec-reject-confirm">Reject</GatedButton>
          <button type="button" className="ag-btn" onClick={() => setMode("idle")}>Cancel</button>
        </form>
      ) : (
        <div className="aa-rec-actions">
          <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn primary" data-testid="aa-rec-accept"
                       title="Make it real work" onClick={() => setMode("accept")}>Accept</GatedButton>
          <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn" data-testid="aa-rec-snooze"
                       title="Hide for a while" onClick={() => setMode("snooze")}>Not now</GatedButton>
          <GatedButton permission="work_item:manage" projectId={projectId} className="ag-btn" data-testid="aa-rec-reject"
                       title="Not useful, say why" onClick={() => setMode("reject")}>Reject</GatedButton>
        </div>
      )}
    </article>
  );
}

export function Roadmap({ projectId, orgId, refresh = 0 }: { projectId: string; orgId?: string | null; refresh?: number }) {
  const live = useConsoleLive();
  const [gone, setGone] = useState<Set<string>>(new Set());
  const read = usePolledRead({ key: roadmapPath(projectId), read: polled(roadmapPath(projectId), parseRoadmap), pollMs: 60_000, paused: !live, enabled: !!projectId });
  const { reload } = read;
  useEffect(() => { if (refresh) reload(); }, [refresh, reload]);
  if (read.error === MISSING) {
    return <div className="aa-empty" data-testid="aa-roadmap-missing"><b>Roadmap is coming</b><span>Recommendations arrive with the learning stream (BOSSAPP-911).</span></div>;
  }
  const recs = (read.data?.recommendations ?? []).filter((r) => !gone.has(r.id) && r.status === "open");
  if (read.data && !recs.length) return <div className="aa-empty" data-testid="aa-roadmap-empty"><b>Nothing recommended</b><span>Agents add ideas here as they learn.</span></div>;
  const order = (r: Recommendation) => r.rank ?? Number.MAX_SAFE_INTEGER;
  return (
    <div className="aa-recs" data-testid="aa-roadmap">
      {recs.sort((a, b) => order(a) - order(b) || b.score - a.score).map((r) => (
        <RecCard key={r.id} rec={r} projectId={projectId} orgId={orgId} onDone={(id) => setGone((g) => new Set(g).add(id))} />
      ))}
    </div>
  );
}
