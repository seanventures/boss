"use client";

/**
 * THE PARKED STEP SAYS WHAT BOSS IS DOING (docs/product/BOSS_FIXES_ITSELF.md §10.1).
 *
 *   ┌─ Fixing BOSS ⓘ ───────────────────────────────────────────────────────────┐
 *   │ ✓ Found  ✓ Reproduced  ● Fixing  ○ Checks  ○ Review  ○ You  ○ Install  ○ Resume │
 *   │ Mender: the new test fails on BOSS as it is (as expected). Making it pass.       │
 *   │ stack_deploy.py, 1 test added, $1.84 of $10, 6 min                            │
 *   │ [ Open self-fix ]  [ Stop ]                                                      │
 *   └──────────────────────────────────────────────────────────────────────────────────┘
 *
 * Its data is the step's own output (`self_fix`, written by the engine's step_view on every
 * tick), so the strip is as live as the step. When the stage is You, the one action is "Review
 * and approve". When it waits on another run's fix, it says so. When a self-fix did not start, one
 * line says why and links the recommendation. Never "waiting" without why.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import { consoleHref } from "../url-intent";
import {
  CHIP_TONE, STAGE_WORDS, chipOf, selfFixAreaHref, selfFixWrite, usd, type StepSelfFix,
} from "../../lib/self-fix";

const here = () => (typeof window === "undefined" ? "" : window.location.search);

export function stepSelfFixOf(output: Record<string, unknown> | null | undefined): StepSelfFix | null {
  const v = (output ?? {}).self_fix;
  return v && typeof v === "object" && typeof (v as StepSelfFix).id === "string" ? (v as StepSelfFix) : null;
}

/** The facts line: files, tests added, spent of budget, minutes. */
export function factsLine(f: StepSelfFix["facts"]): string {
  if (!f) return "";
  const files = (f.files ?? []).map((p) => p.split("/").pop() || p);
  const parts: string[] = [];
  if (files.length) parts.push(files.length > 2 ? `${files.slice(0, 2).join(", ")} +${files.length - 2}` : files.join(", "));
  if (f.tests_added) parts.push(`${f.tests_added} test${f.tests_added === 1 ? "" : "s"} added`);
  if (typeof f.cost_usd === "number") parts.push(f.budget_usd ? `${usd(f.cost_usd)} of ${usd(f.budget_usd)}` : usd(f.cost_usd));
  if (typeof f.minutes === "number" && f.minutes > 0) parts.push(`${Math.round(f.minutes)} min`);
  return parts.join(", ");
}

const MARK = { done: "✓", now: "●", next: "○" } as const;

export function SelfFixStrip({ output, projectId, actor }: {
  output: Record<string, unknown> | null | undefined; projectId: string; actor: string;
}) {
  const sfx = stepSelfFixOf(output);
  const skipped = typeof (output ?? {}).self_fix_skipped === "string" ? String((output ?? {}).self_fix_skipped) : "";
  const outcome = typeof (output ?? {}).self_fix_outcome === "string" ? String((output ?? {}).self_fix_outcome) : "";
  const recId = typeof (output ?? {}).boss_recommendation_id === "string" ? String((output ?? {}).boss_recommendation_id) : "";
  const [stopping, setStopping] = useState(false);
  const [stopped, setStopped] = useState(false);

  if (!sfx) {
    // A BOSS-side failure that BOSS did not fix itself: one line, why, and the recommendation.
    if (!skipped && !outcome) return null;
    const why = skipped || `the self-fix ended ${outcome.replace(/_/g, " ")}`;
    return (
      <div className="sfx-strip sfx-strip-off" data-testid="sfx-strip-skipped" role="status">
        <span className="sfx-strip-label" title="BOSS recorded this for its developers and waits for a BOSS update">
          BOSS did not fix itself
        </span>
        <span className="sfx-strip-why" title={why}>{why}</span>
        {recId ? (
          <a className="ag-link" data-testid="sfx-strip-recommendation"
             href={consoleHref(here(), { area: "self_fixes", rec: recId })}
             title="What BOSS recorded about this bug">Open recommendation</a>
        ) : null}
      </div>
    );
  }

  const chip = chipOf(sfx.chip);
  const building = !["Verified", "Rolled back", "Refused", "In release", "Sent upstream"].includes(chip)
    && !["installing", "health_window", "installed", "verifying", "rolling_back"].includes(sfx.status);
  const facts = factsLine(sfx.facts);
  const stop = async () => {
    setStopping(true);
    const got = await selfFixWrite("cancel", { project_id: projectId, actor, id: sfx.id, reason: "a person stopped it" });
    setStopping(false);
    if (got.ok) { setStopped(true); showToast("success", "Self-fix stopped"); }
    else showToast("error", got.error);
  };

  return (
    <div className="sfx-strip" data-testid="sfx-strip" data-status={sfx.status} role="status">
      <div className="sfx-strip-head">
        <span className="sfx-strip-label" data-testid="sfx-strip-label"
              title="BOSS found a bug in its own code and is changing it. Nothing changes here until you approve.">
          {sfx.label || "Fixing BOSS"}
        </span>
        <span className={`cap-badge tone-${CHIP_TONE[chip].tone}`} data-testid="sfx-strip-chip"
              title={CHIP_TONE[chip].tip}>{chip}</span>
      </div>
      <ol className="sfx-stages" aria-label="Self-fix stages" data-testid="sfx-stages">
        {sfx.stages.map((s) => {
          const w = STAGE_WORDS[s.stage] ?? { word: s.stage, tip: s.stage };
          return (
            <li key={s.stage} className="sfx-stage" data-state={s.state} data-testid={`sfx-stage-${s.stage}`}
                title={w.tip} aria-current={s.state === "now" ? "step" : undefined}>
              <span aria-hidden>{MARK[s.state] ?? "○"}</span> {w.word}
            </li>
          );
        })}
      </ol>
      <div className="sfx-strip-line" data-testid="sfx-strip-line" title={sfx.line}>
        {sfx.waiting_on ? `BOSS is already fixing this (${sfx.id}); this step waits for it.` : sfx.line}
      </div>
      {facts ? <div className="sfx-strip-facts" data-testid="sfx-strip-facts">{facts}</div> : null}
      <div className="sfx-strip-actions">
        {sfx.needs_you && sfx.decision_id ? (
          <a className="ag-btn sfx-primary" data-testid="sfx-strip-approve"
             href={consoleHref(here(), { area: "decisions", decision: sfx.decision_id })}
             title="One card: what broke, the change, the tests, the reviewers">Review and approve</a>
        ) : null}
        <a className="ag-link" data-testid="sfx-strip-open" href={selfFixAreaHref(sfx.id)}
           title="The fix from problem to release">Open self-fix</a>
        {building && !sfx.waiting_on ? (
          <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                       data-testid="sfx-strip-stop" disabled={stopping || stopped} onClick={stop}
                       title="Stop the fix; nothing is installed and the step waits for a BOSS update">
            {stopped ? "Stopped" : stopping ? "Stopping…" : "Stop"}
          </GatedButton>
        ) : null}
      </div>
    </div>
  );
}
