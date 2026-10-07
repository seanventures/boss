"use client";

/**
 * ③ Try it: one sentence becomes a pull request on the default repository. The finish line of
 * setup, and on the board a one-line Quick PR for small work.
 */
import { GatedButton } from "../../components/gated";
import React, { forwardRef, useEffect, useRef, useState } from "react";

import { SetupFailure, setupWrite } from "../../lib/setup-client";
import type { SetupView } from "../../lib/setup-types";
import { toast, useBoard } from "./parts";
import { Pill } from "./pill";
import { SetupRow } from "./setup-row";

export const TryRow = forwardRef<HTMLElement, {
  mode: string; step: number | null; focused: boolean;
}>(function TryRow({ mode, step, focused }, ref) {
  const { projectId, view, apply } = useBoard();
  const [directive, setDirective] = useState("");
  const [busy, setBusy] = useState(false);
  const repo = view.repository.repos.find((r) => r.default) ?? view.repository.repos[0];
  const last = view.try.last_run;
  const running = last && last.pill.tone === "busy";
  const enabled = view.ready && !busy && !running;
  // SAY WHY IT CANNOT RUN: the failing row's own fix ("Token needs Pull requests: Read and
  // write."), not a generic sentence.
  const reason = [...view.repository.repos, ...view.models]
    .map((row) => row.pill).find((pill) => pill.tone === "red" || pill.tone === "amber");
  const blocked = !view.ready
    ? (reason?.tip ?? view.verdict.tip)
    : `Runs a short build on ${repo?.full_name ?? "your repository"}. Never merges. You review it.`;
  // "Your first PR is open." is said once — when this page watched it open — not for ever.
  const wasRunning = useRef(false);
  const [justOpened, setJustOpened] = useState(false);
  useEffect(() => {
    if (last?.pill.tone === "busy") wasRunning.current = true;
    else if (last?.pr_url && wasRunning.current) { wasRunning.current = false; setJustOpened(true); }
  }, [last?.pill.tone, last?.pr_url]);
  const runHref = last ? `?area=delivery_workflows&wfr=${encodeURIComponent(last.run_id)}` : "";

  const start = async () => {
    if (!directive.trim()) return;
    setBusy(true);
    try {
      apply(await setupWrite<SetupView>(projectId, "try", { directive: directive.trim() },
                                        { label: "Open a PR" }));
      setDirective("");
    } catch (error) {
      toast((error as SetupFailure).refusal?.headline ?? "Not started", "error");
    } finally { setBusy(false); }
  };

  const label = mode === "open" ? "Quick PR" : "Try it";
  return (
    <SetupRow ref={ref} row="try" label={label} step={step} mode={mode} focused={focused}
              tip="One sentence becomes a pull request you review.">
      {mode === "waiting" ? (
        <span className="su-faint" title={blocked} data-tip={blocked}
              data-testid="su-try-blocked">Open a PR</span>
      ) : (
        <div className="su-stack">
          <div className="su-line su-try">
            <input type="text" value={directive} data-testid="su-try-input"
                   aria-label="Describe a small change"
                   placeholder="A small change, e.g. fix a typo"
                   disabled={!view.ready}
                   onChange={(e) => setDirective(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && enabled) void start(); }} />
            <GatedButton permission="workflow_run:start" type="button" className="su-btn su-btn-primary" data-testid="su-try-open"
                    disabled={!enabled || !directive.trim()} title={blocked} data-tip={blocked}
                    onClick={start}>
              Open a PR
            </GatedButton>
          </div>
          {last ? (
            <div className="su-line" data-testid="su-try-progress">
              {last.pr_url ? (
                <>
                  <a className="su-pill su-tone-green" href={last.pr_url} target="_blank"
                     rel="noreferrer noopener" title={last.pill.tip} data-tip={last.pill.tip}
                     data-testid="su-try-pr">{last.pill.label}</a>
                  {justOpened ? <span>Your first PR is open.</span> : null}
                </>
              ) : (
                <a href={runHref} className="su-plain"><Pill pill={last.pill} testid="su-try-pill" /></a>
              )}
              {last.pill.tone === "red" ? (
                <a className="su-link" href={runHref} data-testid="su-try-run">Open run ↗</a>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
    </SetupRow>
  );
});
