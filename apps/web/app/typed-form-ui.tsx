"use client";

/**
 * A TYPED QUESTION, DRAWN AS ITS FORM (BOSSAPP-832). An agent (or a workflow) asked a person
 * something with a shape — pick one, tick these, a verdict per finding, which follow-ups to
 * create — and the answer goes back as data. One component for every surface that shows the
 * question: the decision card in Needs you (DecisionActions) and the chat answer that asked it
 * (AskedQuestion, from the turn's "ask" citation). Native inputs only; Send answer is the card's
 * approve with `answer`, Decline its reject with a reason.
 */
import React, { useEffect, useState } from "react";

import { GatedButton } from "../components/gated";
import { aaRead } from "../lib/ambient";
import { answerLines, emptyDraft, missing, parseForm, toAnswer, type Draft, type TypedForm } from "../lib/typed-form";
import { obsWrite } from "./observability/obs-write";
import "./typed-form.css";

export function TypedDecisionForm({ decisionId, form, projectId, actorId, onAnswered }: {
  decisionId: string; form: TypedForm; projectId: string; actorId: string;
  onAnswered?: (status: "approved" | "rejected") => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => emptyDraft(form));
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const gap = missing(form, draft);
  const set = (key: string, value: unknown) => setDraft((d) => ({ ...d, [key]: value }));

  const send = async (action: "approve" | "reject") => {
    setBusy(true);
    setSaid(null);
    const body = action === "approve"
      ? { actor: actorId, answer: toAnswer(form, draft) }
      : { actor: actorId, note: reason.trim() };
    const r = await obsWrite(`/v1/decisions/${encodeURIComponent(decisionId)}/${action}`, body,
                             { scope: `decision:${decisionId}`, label: action === "approve" ? "Send answer" : "Decline" });
    setBusy(false);
    if (!r.ok) { setSaid(r.error ?? "Not sent."); return; }
    onAnswered?.(action === "approve" ? "approved" : "rejected");
  };

  return (
    <div className="tq-form" data-testid="typed-form">
      {form.fields.map((f) => (
        <div key={f.key} className="tq-field" data-testid={`typed-field-${f.key}`}>
          {f.type === "checkbox" ? (
            <label className="tq-check">
              <input type="checkbox" checked={Boolean(draft[f.key])} onChange={(e) => set(f.key, e.target.checked)} />
              {f.label}
            </label>
          ) : (
            <span className="tq-label">{f.label}{f.required && f.type !== "task_suggestions" ? "" : " · optional"}</span>
          )}
          {f.help ? <span className="tq-help">{f.help}</span> : null}
          {f.type === "text" ? (
            f.multiline
              ? <textarea className="textarea" rows={3} aria-label={f.label} value={String(draft[f.key] ?? "")}
                          onChange={(e) => set(f.key, e.target.value)} />
              : <input className="input" aria-label={f.label} value={String(draft[f.key] ?? "")}
                       onChange={(e) => set(f.key, e.target.value)} />
          ) : null}
          {f.type === "select" ? (
            <div className="tq-options" role="radiogroup" aria-label={f.label}>
              {(f.options ?? []).map((o) => (
                <label key={o.value} className="tq-chip" data-on={draft[f.key] === o.value ? "" : undefined}>
                  <input type="radio" name={`${decisionId}-${f.key}`} value={o.value}
                         checked={draft[f.key] === o.value} onChange={() => set(f.key, o.value)} />
                  {o.label}
                </label>
              ))}
            </div>
          ) : null}
          {f.type === "item_verdicts" ? (
            <ul className="tq-items">
              {(f.items ?? []).map((it) => {
                const given = (draft[f.key] ?? {}) as Record<string, string>;
                return (
                  <li key={it.id}>
                    <span className="tq-item">{it.label}</span>
                    <span className="tq-options">
                      {(f.verdicts ?? []).map((v) => (
                        <label key={v} className="tq-chip" data-on={given[it.id] === v ? "" : undefined}>
                          <input type="radio" name={`${decisionId}-${f.key}-${it.id}`} value={v}
                                 checked={given[it.id] === v}
                                 onChange={() => set(f.key, { ...given, [it.id]: v })} />
                          {v}
                        </label>
                      ))}
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : null}
          {f.type === "task_suggestions" ? (
            <ul className="tq-items">
              {(f.suggestions ?? []).map((s, i) => {
                const picks = (draft[f.key] ?? []) as boolean[];
                return (
                  <li key={`${s.title}-${i}`}>
                    <label className="tq-check" title={s.description || undefined}>
                      <input type="checkbox" checked={picks[i] === true}
                             onChange={(e) => set(f.key, picks.map((p, j) => (j === i ? e.target.checked : p)))} />
                      {s.title}
                    </label>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>
      ))}
      {declining ? (
        <div className="tq-decline">
          <textarea className="textarea" rows={2} aria-label="Why you will not answer" placeholder="Why not? The agent reads this."
                    value={reason} onChange={(e) => setReason(e.target.value)} data-testid="typed-decline-reason" />
        </div>
      ) : null}
      <div className="dq-actions tq-actions">
        {declining ? (
          <>
            <GatedButton permission="decision:approve" projectId={projectId} className="dq-btn reject"
                         disabled={busy || !reason.trim()} onClick={() => void send("reject")} data-testid="typed-decline-send">
              Decline
            </GatedButton>
            <button type="button" className="dq-btn neutral" onClick={() => setDeclining(false)}>Back</button>
          </>
        ) : (
          <>
            <GatedButton permission="decision:approve" projectId={projectId} className="dq-btn approve"
                         disabled={busy || Boolean(gap)} title={gap ?? "Send your answer; the agent carries on with it."}
                         onClick={() => void send("approve")} data-testid="typed-send">
              Send answer
            </GatedButton>
            <button type="button" className="dq-btn neutral" onClick={() => setDeclining(true)} data-testid="typed-decline">
              Decline
            </button>
          </>
        )}
        {gap && !declining ? <span className="tq-gap" data-testid="typed-gap">{gap}</span> : null}
      </div>
      {said ? <p className="tq-said" role="alert" data-testid="typed-said">{said}</p> : null}
    </div>
  );
}

/** What was answered, one line per field. */
export function TypedAnswer({ form, answer, by }: { form: TypedForm; answer: Record<string, unknown> | null | undefined; by?: string | null }) {
  const lines = answerLines(form, answer);
  if (!lines.length) return null;
  return (
    <ul className="tq-answer" data-testid="typed-answer" aria-label={by ? `Answered by ${by}` : "Answer"}>
      {lines.map((l) => <li key={l}>{l}</li>)}
    </ul>
  );
}

type DecisionWire = { id: string; question: string; status: string; form_schema?: unknown;
  answer?: Record<string, unknown> | null; answered_by?: string | null; resolution_note?: string | null };

/** THE QUESTION IN THE CHAT: the chat answer that asked it draws it here, live. */
export function AskedQuestion({ decisionId, projectId, actorId }: { decisionId: string; projectId: string; actorId: string }) {
  const [card, setCard] = useState<DecisionWire | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    void aaRead(`/v1/decisions/${encodeURIComponent(decisionId)}`, (w) => w as DecisionWire).then((r) => {
      if (live && r.ok && r.data) setCard(r.data);
    });
    return () => { live = false; };
  }, [decisionId, tick]);
  const form = parseForm(card?.form_schema);
  if (!card || !form) return null;
  return (
    <div className="tq-asked" data-testid={`asked-${decisionId}`}>
      <p className="tq-question">{card.question}</p>
      {card.status === "pending" || card.status === "delegated" ? (
        <TypedDecisionForm decisionId={decisionId} form={form} projectId={projectId} actorId={actorId}
                           onAnswered={() => setTick((t) => t + 1)} />
      ) : card.status === "approved" ? (
        <TypedAnswer form={form} answer={card.answer} by={card.answered_by} />
      ) : (
        <p className="tq-said">Declined{card.resolution_note ? `: ${card.resolution_note}` : ""}</p>
      )}
    </div>
  );
}
