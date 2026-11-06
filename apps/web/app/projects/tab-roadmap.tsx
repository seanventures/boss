"use client";

/**
 * ROADMAP (screen 7, contract §6): Now · Next · Later (from the work list, Marshal's rank), then the
 * ranked recommendations as evidence → detector → agent. Accept turns one into planned work;
 * Reject asks for a reason in one tap (it teaches the detector); Not now snoozes it. Reads the
 * existing GET /v1/ambient/roadmap — stored items, so it works with the ambient preview off.
 */
import React, { useState } from "react";
import { Check, ChatCircleDots, Lightbulb, MapTrifold, X, ArrowRight, Clock } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { REJECT_CATEGORIES, REJECT_WORDS, parseRoadmap, roadmapPath, type Recommendation, type RejectCategory, type RoadmapView } from "../../lib/ambient";
import { acceptRecommendation, rejectRecommendation, snoozeRecommendation, usePxRead } from "../../lib/project-experience";
import { FX_ROADMAP } from "../../lib/project-experience-fixtures";
import { Empty, Face, Flow, Ico, ReadGate, Said, Tag } from "./kit";
import type { PxCtx } from "./project-page";

export function RoadmapTab({ ctx }: { ctx: PxCtx }) {
  const read = usePxRead<RoadmapView>(roadmapPath(ctx.projectId), parseRoadmap, { everyMs: 30_000, demo: () => parseRoadmap(FX_ROADMAP) });
  const items = ctx.work?.items ?? [];
  const now = items.filter((w) => w.state === "running" || w.state === "needs_you");
  const planned = items.filter((w) => w.state === "planned");
  const cols = [{ key: "now", label: "Now", tone: "var(--teal)", rows: now }, { key: "next", label: "Next", tone: "var(--quiet)", rows: planned.slice(0, 3) },
                { key: "later", label: "Later", tone: "var(--quiet)", rows: planned.slice(3) }];
  return (
    <div className="px-stack" data-testid="px-roadmap">
      <section className="px-card" aria-labelledby="px-rm-plan">
        <div className="px-card-h"><Ico icon={MapTrifold} c="violet" /><h2 id="px-rm-plan">Plan</h2>
          <small className="px-faint px-hide-phone">Helm proposes · Marshal ranks · you decide</small>
          <a className="px-more" href="#" data-testid="px-rm-plan-ask" onClick={(e) => { e.preventDefault(); ctx.ask("Plan the next two weeks for this project."); }}>
            <ChatCircleDots size={12} /> Plan with {ctx.overview.captain?.name ?? "BOSS"}</a></div>
        <div className="px-cols">
          {cols.map((c) => (
            <div key={c.key} className="px-col" data-testid={`px-rm-${c.key}`}>
              <div className="px-row" style={{ marginBottom: 6 }}><span className="px-dot" style={{ background: c.tone }} aria-hidden /><b>{c.label}</b><span className="px-nbadge">{c.rows.length}</span></div>
              {c.rows.length ? c.rows.map((w) => (
                <button key={w.id} type="button" className="px-li" style={{ width: "100%", background: "none", border: 0, borderTop: "1px solid var(--border)", color: "inherit", font: "inherit", cursor: "pointer", textAlign: "left" }}
                        onClick={() => ctx.open("work", w.id)}>
                  <span className="px-ck-b" aria-hidden style={w.state === "needs_you" ? { background: "var(--amber)", borderColor: "var(--amber)", color: "#1b1300", borderRadius: "50%" } : undefined}>{w.state === "needs_you" ? "!" : ""}</span>
                  <b className="px-ell">{w.label}</b>
                  {w.agents[0] ? <Face agent={w.agents[w.agents.length - 1]} size={20} state={w.state === "running" ? "working" : "idle"} /> : <span />}
                </button>
              )) : <small className="px-faint">Nothing here</small>}
            </div>
          ))}
        </div>
      </section>
      <ReadGate read={read} what="Recommendations" loadingRows={2}>
        {(rm) => <Recommendations ctx={ctx} rm={rm} onChanged={read.refresh} />}
      </ReadGate>
    </div>
  );
}

function Recommendations({ ctx, rm, onChanged }: { ctx: PxCtx; rm: RoadmapView; onChanged: () => void }) {
  const [done, setDone] = useState<Record<string, "accepted" | "rejected" | "snoozed">>({});
  const recs = rm.recommendations;
  return (
    <section aria-labelledby="px-rm-recs" className="px-stack" style={{ gap: 10 }}>
      <div className="px-row px-wrap">
        <Ico icon={Lightbulb} c="violet" /><h2 id="px-rm-recs" style={{ margin: 0, fontFamily: "var(--display)", fontSize: ".86rem" }}>Recommendations</h2>
        <Tag tone="violet">{rm.counts.open ?? recs.length} new</Tag>
        <span className="px-spacer" />
        <span className="px-row px-wrap" style={{ gap: 6 }}>
          {Object.entries(rm.counts).filter(([k]) => k !== "open").map(([k, v]) => <Tag key={k}>{k[0].toUpperCase() + k.slice(1)} {v}</Tag>)}
        </span>
      </div>
      {recs.length ? recs.map((r, i) => <RecCard key={r.id} ctx={ctx} r={r} i={i} state={done[r.id]} onDone={(s) => { setDone((d) => ({ ...d, [r.id]: s })); onChanged(); }} />)
        : <div className="px-card"><Empty icon={Lightbulb} tone="violet" title="No recommendations yet" line="Helm proposes work from incidents, hot code, test gaps and your PRDs. Ask it to plan, or come back after the next run." testid="px-rm-empty">
            <button type="button" className="px-btn" onClick={() => ctx.ask("What should we work on next?")}><ChatCircleDots size={13} /> Ask {ctx.overview.captain?.name ?? "BOSS"}</button></Empty></div>}
    </section>
  );
}

function RecCard({ ctx, r, i, state, onDone }: { ctx: PxCtx; r: Recommendation; i: number; state?: "accepted" | "rejected" | "snoozed"; onDone: (s: "accepted" | "rejected" | "snoozed") => void }) {
  const [rejecting, setRejecting] = useState(false);
  const [category, setCategory] = useState<RejectCategory | null>(null);
  const [reason, setReason] = useState("");
  const [said, setSaid] = useState("");
  const act = async (fn: () => Promise<{ ok: boolean; error: string | null; demo?: boolean }>, s: "accepted" | "rejected" | "snoozed") => {
    const res = await fn();
    if (res.ok) { setSaid(res.demo ? "Sample mode — nothing was sent." : ""); onDone(s); setRejecting(false); }
    else setSaid(res.error ?? "It didn't go through.");
  };
  if (state === "accepted") {
    return (
      <div className="px-rc" data-s="accepted" data-testid={`px-rec-${r.id}`}>
        <span className="px-rank" style={{ background: "var(--green-soft)", color: "var(--green)" }}><Check size={14} weight="bold" /></span>
        <div><div className="px-rc-t">{r.title}</div><Flow nodes={[{ label: "Accepted", state: "done" }, { label: "On Work · Next", state: "todo" }]} /><Said text={said} /></div>
        <a className="px-chip" href="#" onClick={(e) => { e.preventDefault(); ctx.tab("work"); }}>Open work <ArrowRight size={11} /></a>
      </div>
    );
  }
  if (state) {
    return <div className="px-rc" data-testid={`px-rec-${r.id}`}><span className="px-rank">{i + 1}</span><div className="px-faint">{r.title} — {state === "rejected" ? "rejected; the detector learns from it" : "snoozed for two weeks"}<Said text={said} /></div><span /></div>;
  }
  const valuePct = r.value === "high" ? 85 : r.value === "medium" ? 55 : 25;
  return (
    <div className="px-rc" data-s={rejecting ? "rejecting" : undefined} data-testid={`px-rec-${r.id}`}>
      <span className="px-rank">{r.rank ?? i + 1}</span>
      <div style={{ minWidth: 0 }}>
        <div className="px-rc-t">{r.title}</div>
        <Flow nodes={r.strip.map((n) => ({ label: n.label, type: n.type === "agent" ? "agent" : n.type === "signal" ? "signal" : "boss" }))} />
        <div className="px-row px-wrap" style={{ marginTop: 6 }}>
          <Tag>{r.kindLabel || r.kind}</Tag>
          <span className="px-score">Value <span className="px-bar"><span style={{ width: `${valuePct}%`, background: "var(--green)" }} /></span></span>
          <span className="px-score">Effort <b>{r.effort === "low" ? "S" : r.effort === "medium" ? "M" : "L"}</b></span>
        </div>
        {rejecting ? (
          <form className="px-stack" style={{ gap: 6, marginTop: 10 }} onSubmit={(e) => { e.preventDefault(); if (category) void act(() => rejectRecommendation(ctx.projectId, r.id, reason.trim() || REJECT_WORDS[category].label, category), "rejected"); }}>
            <span className="px-lbl">Reject because</span>
            <div className="px-filters" role="group" aria-label="Reason">
              {REJECT_CATEGORIES.map((c) => (
                <button key={c} type="button" className="px-filter" aria-pressed={category === c} title={REJECT_WORDS[c].tip} onClick={() => setCategory(c)} data-testid={`px-rec-why-${c}`}>{REJECT_WORDS[c].label}</button>
              ))}
            </div>
            <input className="px-input" placeholder="Say more (optional)" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Say more" />
            <div className="px-row"><GatedButton permission="work_item:manage" projectId={ctx.projectId} type="submit" className="px-btn" data-kind="primary" data-size="s" disabled={!category}
                                                data-testid={`px-rec-reject-confirm-${r.id}`}>Reject</GatedButton>
              <button type="button" className="px-btn" data-kind="ghost" data-size="s" onClick={() => setRejecting(false)}>Cancel</button></div>
          </form>
        ) : null}
        <Said text={said} tone="red" />
      </div>
      {!rejecting ? (
        <div className="px-rc-act">
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-size="s" data-testid={`px-rec-accept-${r.id}`}
                       onClick={() => void act(() => acceptRecommendation(ctx.projectId, r.id, "ticket"), "accepted")}><Check size={12} /> Accept</GatedButton>
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid={`px-rec-reject-${r.id}`}
                       onClick={() => setRejecting(true)}><X size={12} /> Reject</GatedButton>
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-kind="ghost" data-size="s" data-testid={`px-rec-snooze-${r.id}`}
                       onClick={() => void act(() => snoozeRecommendation(ctx.projectId, r.id, 14), "snoozed")}><Clock size={12} /> Not now</GatedButton>
        </div>
      ) : <span />}
    </div>
  );
}
