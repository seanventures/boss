"use client";

/**
 * Silence a rule for a set time, with a reason (BOSSAPP-598). Audited: who, until when and why
 * is kept with the rule's changes. A silenced alert is still recorded; it is not grouped into an
 * incident, not triaged and does not respond.
 *
 *   Silence Error rate
 *     For      [ 1 hour ▾ ]
 *     Why      [ load test until 9                 ]   (required)
 *                                       [ Cancel ]  [ Silence ]
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { silencePath, type AlertRule } from "../../lib/app-alerts";
import { showToast } from "../client-forms";
import { Sheet } from "../infra/kit";
import { obsWrite } from "./obs-write";

const DURATIONS = [
  { minutes: 60, label: "1 hour" }, { minutes: 240, label: "4 hours" }, { minutes: 1440, label: "24 hours" },
  { minutes: 10080, label: "7 days" },
];

export function SilenceSheet({ rule, targetId, projectId, actor, onClose, onSaved }: {
  rule: AlertRule; targetId: string; projectId: string; actor: string; onClose: () => void; onSaved: () => void;
}) {
  const [minutes, setMinutes] = useState(60);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    const r = await obsWrite(silencePath(targetId, rule.id), { project_id: projectId, actor, minutes, reason },
                             { scope: `obs:silence:${rule.id}`, label: "Silence" });
    setBusy(false);
    if (r.ok) { showToast("success", "Silenced"); onSaved(); } else setError(r.error ?? "Not silenced");
  };
  return (
    <Sheet title={`Silence ${rule.label}`} testId="obs-silence-sheet" onClose={onClose}>
      <div className="obs-rform">
        <label className="inf-field">
          <span>For</span>
          <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} data-testid="obs-silence-for">
            {DURATIONS.map((d) => <option key={d.minutes} value={d.minutes}>{d.label}</option>)}
          </select>
        </label>
        <label className="inf-field">
          <span>Why</span>
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} data-testid="obs-silence-reason"
                    placeholder="Load test until 9" />
        </label>
        <span className="inf-faint" title="Recorded with who silenced it and when">Kept with the rule&apos;s changes</span>
        {error ? <span className="inf-error" data-testid="obs-silence-error" data-copy="data">{error}</span> : null}
        <div className="inf-actions">
          <span className="ag-spacer" />
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-silence-save"
                       disabled={busy || reason.trim().length < 3} aria-busy={busy || undefined} onClick={save}>
            {busy ? "Silencing…" : "Silence"}
          </GatedButton>
        </div>
      </div>
    </Sheet>
  );
}
