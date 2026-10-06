"use client";

/**
 * The monthly check-in (docs/product/RUN_SCORECARDS.md): at most once in 30 days per person,
 * dismissible, never in the way. "How disappointed would you be if you could no longer use
 * BOSS?" plus an optional one-liner, kept on this install; it is sent only if the person ticks
 * Send it (and Improve BOSS is on).
 */
import React, { useEffect, useState } from "react";

import { improveWrite, readImprove, type PromptsView } from "../../lib/improve";
import { showToast } from "../client-forms";
import "./improve.css";

const ANSWERS = [
  { key: "very", word: "Very" }, { key: "somewhat", word: "Somewhat" }, { key: "not", word: "Not really" },
];

export function CheckinCard({ projectId, actor }: { projectId: string; actor: string }) {
  const [due, setDue] = useState(false);
  const [answer, setAnswer] = useState<string | null>(null);
  const [line, setLine] = useState("");
  const [share, setShare] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!projectId) return undefined;
    let live = true;
    void readImprove<PromptsView>("prompts", { project_id: projectId, actor, subjects: "" }).then((got) => {
      if (!live || !got.ok || got.notModified || (got.data as { checkin_due?: unknown })?.checkin_due !== true) return;
      setDue(true);
      // Showing it is this month's ask, whether or not it is answered.
      void improveWrite("prompt_shown", { project_id: projectId, actor, prompt: "checkin" });
    });
    return () => { live = false; };
  }, [projectId, actor]);

  if (!due) return null;
  const done = async (dismiss: boolean) => {
    setBusy(true);
    const r = await improveWrite("checkin", { project_id: projectId, actor, dismiss, answer: dismiss ? null : answer,
                                              line: dismiss ? null : line, share_line: share });
    setBusy(false);
    if (r.ok) { setDue(false); if (!dismiss) showToast("success", "Thank you"); }
    else showToast("error", "Not saved");
  };
  return (
    <section className="im-checkin" aria-label="Monthly check-in" data-testid="checkin">
      <div className="im-checkin-head">
        <h3 className="im-checkin-q" title="How disappointed would you be if you could no longer use BOSS?">
          If BOSS went away tomorrow?
        </h3>
        <button type="button" className="im-x" aria-label="Not now" title="Not now. Asked again in a month at most."
                data-testid="checkin-dismiss" disabled={busy} onClick={() => void done(true)}>×</button>
      </div>
      <div className="im-checkin-row" role="radiogroup" aria-label="How disappointed would you be if you could no longer use BOSS?">
        {ANSWERS.map((a) => (
          <button key={a.key} type="button" role="radio" aria-checked={answer === a.key} className="im-rate-btn"
                  aria-pressed={answer === a.key} data-testid={`checkin-${a.key}`}
                  title={`${a.word} disappointed`} onClick={() => setAnswer(a.key)}>{a.word} disappointed</button>
        ))}
      </div>
      {answer ? (
        <div className="im-checkin-row">
          <input className="im-input" value={line} onChange={(e) => setLine(e.target.value)} maxLength={140}
                 placeholder="The one thing to fix? Optional" aria-label="The one thing to fix" data-testid="checkin-line" />
          <label className="im-muted" title="Only if Improve BOSS is on; otherwise it stays here">
            <input type="checkbox" checked={share} onChange={(e) => setShare(e.target.checked)} data-testid="checkin-share" /> Send it
          </label>
          <button type="button" className="ag-btn ag-btn-primary" disabled={busy} data-testid="checkin-save"
                  onClick={() => void done(false)}>Save</button>
        </div>
      ) : null}
    </section>
  );
}
