"use client";

/**
 * The status line (command-center spec §4.5): one sentence of state beside four live numbers.
 *
 * THE SENTENCE IS THE SERVER'S (`view.status`), chosen by the first matching rule, so the pill,
 * the favicon and this line cannot disagree. The web adds only what it alone can know: that BOSS
 * could not be reached (`Can't reach BOSS`), and the `Paused` chip — which never changes the
 * words. Screen readers hear the sentence when its KEY changes, never on every poll.
 *
 * THE NUMBERS NEVER TICK. A value changes in place; each is a link to the section that holds
 * it. `NEED YOU` is the one needs-you number (Approvals, the top-bar pill and Live say it too).
 */
import React, { useEffect, useRef, useState } from "react";

import type { Today, ViewCounts, ViewStatus } from "../../lib/command-center-view";

export function money(micros: number): string {
  return `$${(Math.max(0, micros) / 1_000_000).toFixed(2)}`;
}

export type StatusLineProps = {
  status: ViewStatus & { paused: boolean };
  /** Null when no view has ever been read: the numbers say `—`, never a confident zero. */
  counts: ViewCounts | null;
  today: Today | null;
  /** SPENT was pressed: Today switches to Spend. */
  onSpend: () => void;
  /** The help card, for the `?` popover. */
  help?: React.ReactNode;
  helpOpen: boolean;
  onHelp: (open: boolean) => void;
};

export function StatusLine({ status, counts, today, onSpend, help, helpOpen, onHelp }: StatusLineProps) {
  // ANNOUNCED ON A CHANGE OF STATE, NOT A POLL. The live region starts empty (a page load is
  // not news) and takes the sentence only when its key moves.
  const [said, setSaid] = useState("");
  const lastKey = useRef(status.key);
  useEffect(() => {
    if (status.key !== lastKey.current) {
      lastKey.current = status.key;
      setSaid(status.text);
    }
  }, [status.key, status.text]);

  const helpRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!helpOpen) return undefined;
    const outside = (e: PointerEvent) => {
      if (!helpRef.current?.contains(e.target as Node)) onHelp(false);
    };
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape") onHelp(false); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [helpOpen, onHelp]);

  const unreachable = status.key === "unreachable";
  const dash = "—";
  const spent = today ? today.moneyMicros : 0;
  const included = today ? today.includedMicros : 0;
  const coveredOnly = !!today && spent === 0 && included > 0;
  const spentTip = coveredOnly
    ? `Covered by your plan · ${money(included)} API value`
    : "Money spent today. Plan-covered calls are $0.";

  return (
    <div className="cc-status" data-tone={status.tone} data-key={status.key}>
      <div className="cc-status-state">
        <span className="cc-dot" data-tone={status.tone} aria-hidden />
        <p className="cc-status-text" title={status.tip}
            {...(unreachable ? { "data-testid": "cc2-load-error" } : {})}>
          {status.text}
        </p>
        {status.paused ? (
          <span className="cc-chip" data-testid="cc-paused"
                title="Live updates are paused. Press Live in the top bar.">Paused</span>
        ) : null}
        <span className="ag-sr" role="status" aria-live="polite">{said}</span>
        {help ? (
          <div className="cc-help" ref={helpRef}>
            <button type="button" className="cc-help-btn" aria-expanded={helpOpen}
                    aria-haspopup="dialog" aria-label="What you can do here"
                    title="What you can do here" data-testid="cc-help"
                    onClick={() => onHelp(!helpOpen)}>?</button>
            {helpOpen ? (
              <div className="cc-help-pop" role="dialog" aria-label="What you can do here">{help}</div>
            ) : null}
          </div>
        ) : null}
      </div>

      <nav className="cc-nums" aria-label="At a glance">
        <a className="cc-num" href="#cc2-running" data-testid="cc2-kpi-running"
           title="Workflow runs in progress">
          <span className="cc-num-l">RUNNING</span>
          <span className="cc-num-n">{counts ? counts.running : dash}</span>
        </a>
        <a className={`cc-num${counts && counts.needsYou > 0 ? " amber" : ""}`} href="#cc2-needs-you"
           data-testid="cc2-kpi-needs-you" title="Decisions waiting. Same as Approvals and the top bar.">
          <span className="cc-num-l">NEED YOU</span>
          <span className="cc-num-n">{counts ? counts.needsYou : dash}</span>
        </a>
        <a className="cc-num" href="#cc-today" data-testid="cc-kpi-done-today"
           title={`Runs that finished today. Failed: ${today ? today.runsFailed : 0}.`}>
          <span className="cc-num-l">DONE TODAY</span>
          <span className="cc-num-n">{today ? today.runsFinished : dash}</span>
        </a>
        <a className={`cc-num${today && spent === 0 && included === 0 ? " cc-num-zero" : ""}`}
           href="#cc-today" data-testid="cc-kpi-spent" title={spentTip}
           onClick={() => onSpend()}>
          <span className="cc-num-l">SPENT</span>
          <span className="cc-num-n">{!today ? dash : coveredOnly ? "Included" : money(spent)}</span>
        </a>
      </nav>
    </div>
  );
}
