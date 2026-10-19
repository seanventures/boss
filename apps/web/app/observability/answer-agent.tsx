"use client";

/**
 * ANSWER THE AGENT (BOSSAPP-598): when the on-call agent is not sure enough to act, it asks —
 * and the person answers here, with one button per option it offered. The answer runs the same
 * path as the agent deciding (POST …/triage/answer → app_alerts.answer_triage): an incident is
 * opened or joined, and Respond automatically starts exactly as if the agent had decided.
 *
 * One drawing, used by the alert's details (alerts-tab.tsx) and the incident card's Who did
 * what (responders.tsx).
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { answerPath, type TriageOption } from "../../lib/app-alerts";
import { notifyChanged } from "../changed";
import { showToast } from "../client-forms";
import { obsWrite } from "./obs-write";

export function AnswerAgent({ question, options, projectId, actor, targetId, alertId, onDone, agent = "The agent" }: {
  question: string | null; options: TriageOption[]; projectId: string; actor: string;
  targetId: string; alertId: string; onDone?: () => void; agent?: string;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  if (!options.length) return null;
  const answer = async (o: TriageOption) => {
    if (busy) return;
    setBusy(o.action);
    const r = await obsWrite<{ word?: string; response?: { word?: string } }>(
      answerPath(targetId, alertId), { project_id: projectId, actor, action: o.action },
      { scope: `obs:answer:${alertId}`, label: o.label });
    setBusy(null);
    if (r.ok) {
      showToast("success", [o.label, r.body?.response?.word].filter(Boolean).join(" · "));
      notifyChanged("observability", { alert_id: alertId });
      onDone?.();
    } else showToast("error", r.error ?? "Not answered");
  };
  return (
    <div className="obs-answer" data-testid="obs-answer-agent" role="group" aria-label={`${agent} asks`}>
      {question ? <span className="obs-answer-q" data-user-text data-testid="obs-answer-question">{question}</span> : null}
      <span className="obs-answer-opts">
        {options.map((o) => (
          <GatedButton key={o.action} permission="reliability:respond" type="button" className="ag-btn ag-btn-sm"
                       data-testid="obs-answer-option" data-action={o.action} disabled={!!busy}
                       aria-busy={busy === o.action || undefined}
                       title={`Answer: ${o.label}. BOSS acts on it as if ${agent} had decided.`}
                       onClick={() => void answer(o)}>
            {busy === o.action ? "Answering…" : o.label}
          </GatedButton>
        ))}
      </span>
    </div>
  );
}
