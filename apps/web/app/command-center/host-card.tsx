"use client";
/**
 * THE HOST, AS A SYSTEM CARD (design pass). The landing page used the console-wide banner:
 * three lines of prose and a raw `docker system prune` across the top of the factory. The truth
 * is the same — the server's own words (run_retrospective.host_advice) — said concisely: the
 * headline and the measurement, with the fix one hover (or one copy) away.
 *
 * SEAM: the stability stream turns this into its system-health card (a shared AttentionCard
 * kind and a /v1 system-health read); until then it reads `view.host`.
 */
import React, { useState } from "react";
import { Copy, Check, Warning } from "@phosphor-icons/react";

import type { HostAdvice } from "../host-advice";

/** The first `command` the server's advice names, if any. */
export function commandIn(action: string | undefined): string | null {
  const m = /`([^`]+)`/.exec(action ?? "");
  return m ? m[1] : null;
}

export function HostCard({ advice }: { advice: HostAdvice }) {
  const [copied, setCopied] = useState(false);
  if (!advice?.show) return null;
  const command = commandIn(advice.action);
  const fix = (advice.action ?? "").replace(/`/g, "");
  return (
    <div className="cc-host" data-severity={advice.severity ?? "warning"} data-testid="host-advice" role="status">
      <Warning className="cc-host-icon" size={16} weight="bold" aria-hidden />
      <b className="cc-host-head">{advice.headline}</b>
      {advice.detail ? <span className="cc-host-detail" title={advice.detail}>{advice.detail}</span> : null}
      <span className="cc-spacer" />
      {fix ? <span className="cc-host-fix" title={fix} tabIndex={0} data-testid="cc-host-fix">How to fix</span> : null}
      {command ? (
        <button type="button" className="ag-btn cc-host-copy" title={`Copy: ${command}`} data-testid="cc-host-copy"
                onClick={() => {
                  try { void navigator.clipboard?.writeText(command); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { /* none */ }
                }}>
          {copied ? <Check size={12} weight="bold" aria-hidden /> : <Copy size={12} weight="bold" aria-hidden />}
          {copied ? "Copied" : "Copy fix"}
        </button>
      ) : null}
    </div>
  );
}
