"use client";

/**
 * SETUP › EVENTS › DEAD LETTERS (P9a-7, wireframe E6): events that kept failing. Fix the cause,
 * then replay. Tick some (or all) → Reprocess N: only the rules that failed run again, with every
 * guard checked again. Force…: one new run, skipping limits, with a typed reason that is logged
 * with your name (POST /v1/intake/events/replay, mode reprocess | force). Each row says what
 * failed in words, which rule, how many tries and when it first failed, and opens in the stream.
 * A connection with nothing stuck says so.
 */
import React, { useMemo, useState } from "react";
import { ArrowSquareOut, CheckCircle, Lightning, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { Field } from "../../components/form-kit";
import { labelOfKind, readDeadLetters, replayEvents, type DeadLetter } from "../../lib/intake";
import { EvFrame, useEv } from "./frame";
import { Empty, ReadState, Said, SourceMark, goEvents, useRead } from "./parts";

const hhmm = (iso: string) => {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};
export const deadTitle = (d: DeadLetter) => d.text ? (d.actor.display ? `${d.actor.display}: ${d.text}` : d.text) : d.subject || d.type;
/** What failed, as a person says it: the last error without its Python class name. */
export const failWords = (err: string) => err.replace(/^[A-Za-z_.]+(Error|Exception|Refused): /, "").replace(/^parse refused: /, "BOSS could not read it: ") || "It kept failing.";

export function DeadLettersTab() {
  const { projectId, reloadConns } = useEv();
  const { read, reload } = useRead(() => readDeadLetters(projectId), [projectId]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [force, setForce] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" | "amber" } | null>(null);
  const items = useMemo(() => (read?.ok ? read.data.items : []), [read]);
  // "Teams: nothing stuck" beside the stuck ones; with nothing stuck at all, the empty state says it once.
  const calm = read?.ok && read.data.items.length ? read.data.per_connection.filter((c) => c.dead === 0) : [];
  const all = items.length > 0 && items.every((d) => picked.has(d.id));
  const chosen = items.filter((d) => picked.has(d.id));

  const flip = (id: string) => setPicked((cur) => { const next = new Set(cur); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const replay = async (mode: "reprocess" | "force") => {
    setBusy(true); setSaid(null);
    const r = await replayEvents(projectId, chosen.map((d) => d.id), mode, mode === "force" ? reason.trim() : "");
    setBusy(false);
    if (!r.ok) { setSaid({ text: r.error || "Not replayed.", tone: "red" }); return; }
    const b = (r.body ?? {}) as { replayed?: string[]; skipped?: { id: string; reason: string }[] };
    const n = b.replayed?.length ?? 0;
    const skipped = b.skipped ?? [];
    setSaid({ text: `${mode === "force" ? "Forced" : "Reprocessing"} ${n}${skipped.length ? ` · ${skipped.length} skipped (${skipped[0].reason})` : ""}. Watch them in the stream.`,
              tone: skipped.length && !n ? "amber" : "green" });
    setPicked(new Set()); setForce(false); setReason("");
    reload(); reloadConns();
  };

  return (
    <EvFrame tab="dead" title="Dead letters" sub="Events that kept failing. Fix the cause, then replay.">
      <Said said={said} testId="ev-dl-said" />
      <div className="ev-split">
        <section className="ev-card" aria-labelledby="ev-dl-h" data-testid="ev-dead-list">
          <ReadState read={read} what="Dead letters" onRetry={reload} testId="ev-dl" />
          {read?.ok && !items.length ? (
            <Empty icon={<CheckCircle size={30} aria-hidden />} title="Nothing is stuck" line="Every event reached its agent or was set aside on purpose." testId="ev-dl-empty" />
          ) : null}
          {items.length ? (
            <>
              <div className="ev-bulk">
                <input type="checkbox" aria-label="Select all" checked={all} onChange={() => setPicked(all ? new Set() : new Set(items.map((d) => d.id)))} data-testid="ev-dl-all" />
                <h2 id="ev-dl-h" className="ev-h2">{chosen.length ? `${chosen.length} of ${items.length} selected` : `${items.length} stuck`}</h2>
                <span className="ev-spacer" />
                <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-size="sm" disabled={!chosen.length || busy}
                             title="Run anyway, even if limits or a past run say no. Needs a reason." onClick={() => setForce(true)} data-testid="ev-dl-force">Force…</GatedButton>
                <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-size="sm" data-kind="primary" disabled={!chosen.length || busy}
                             title="Run only the rules that failed, with every guard checked again" onClick={() => void replay("reprocess")} data-testid="ev-dl-reprocess">
                  Reprocess {chosen.length || ""}</GatedButton>
              </div>
              {items.map((d) => (
                <div key={d.id} className="ev-dl" data-testid={`ev-dl-${d.id}`}>
                  <input type="checkbox" checked={picked.has(d.id)} onChange={() => flip(d.id)} aria-label={`Select ${deadTitle(d)}`} data-testid={`ev-dl-pick-${d.id}`} />
                  <SourceMark kind={d.kind} size="sm" />
                  <div>
                    <b>{deadTitle(d)}</b>
                    <span className="ev-dl-why"><Warning size={13} aria-hidden /> {failWords(d.last_error)}</span>
                    <span className="ev-faint">{d.connection_name || d.kind} · {d.routes.length ? `${d.routes.length} rule${d.routes.length === 1 ? "" : "s"}` : "no rule ran"} · {d.attempts} tr{d.attempts === 1 ? "y" : "ies"} · first failed {hhmm(d.first_failed_at)}</span>
                  </div>
                  <button type="button" className="ev-link" title="Open in the event stream" aria-label="Open in the event stream"
                          onClick={() => goEvents({ events: "stream", ev: d.id })} data-testid={`ev-dl-open-${d.id}`}><ArrowSquareOut size={15} aria-hidden /></button>
                </div>
              ))}
            </>
          ) : null}
        </section>
        <aside className="ev-form" aria-label="Force and connections">
          {force && chosen.length ? (
            <section className="ev-card" aria-labelledby="ev-force-h" data-testid="ev-force">
              <h2 id="ev-force-h" className="ev-h2">Force {chosen.length === 1 ? deadTitle(chosen[0]).slice(0, 40) : `${chosen.length} events`}</h2>
              <span className="ev-faint">Starts one new run each, skipping limits. Logged with your name.</span>
              <Field label="Reason *" tip="Logged with your name and the time"><textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} required aria-label="Reason" data-testid="ev-force-reason" /></Field>
              <div className="ev-row-line">
                <button type="button" className="ev-btn" data-kind="ghost" onClick={() => { setForce(false); setReason(""); }} data-testid="ev-force-cancel">Cancel</button>
                <span className="ev-spacer" />
                <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-kind="danger" disabled={!reason.trim() || busy}
                             title={reason.trim() ? undefined : "Say why first"} onClick={() => void replay("force")} data-testid="ev-force-go">
                  <Lightning size={14} aria-hidden /> Force run</GatedButton>
              </div>
            </section>
          ) : null}
          {calm.map((c) => {
            const kindLabel = labelOfKind(c.kind) || c.kind;
            return (
              <section key={c.connection_id} className="ev-card" data-kind={c.kind} aria-label={`${kindLabel} ${c.name || c.kind} has no dead letters`} data-testid={`ev-calm-${c.connection_id}`}>
                <span className="ev-calm">
                  <SourceMark kind={c.kind} size="sm" />
                  <CheckCircle size={18} aria-hidden />
                  <span className="ev-row-main">
                    <span className="ev-faint" data-testid={`ev-calm-kind-${c.connection_id}`}>{kindLabel}</span>
                    <b>{c.name || kindLabel}: nothing stuck</b>
                  </span>
                </span>
                <span className="ev-faint">Every event from it reached its agent.</span>
              </section>
            );
          })}
        </aside>
      </div>
    </EvFrame>
  );
}
