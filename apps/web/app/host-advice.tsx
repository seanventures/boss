"use client";

import React, { useEffect, useState } from "react";
// The icon set both callers already use. A hand-drawn triangle would have been one more
// warning glyph to keep in step with the rest of the console for no gain.
import { Warning } from "@phosphor-icons/react";

/**
 * "This computer is out of memory" — said to the person who can fix it.
 *
 * WHY THIS COMPONENT EXISTS. Two runs this week were read as BOSS degrading while the machine
 * underneath was thrashing: one took 85 minutes that should have taken 20, another 242 minutes
 * of which 63 were actual work. Both diagnoses came from a person reading container logs by
 * hand. A customer has nobody to do that, and the operator's verdict was exact — "there will be
 * no one to give that info to customer".
 *
 * Deliberately quiet. `show:false` on a healthy machine renders nothing at all: an indicator
 * that is always on is an indicator nobody reads, and this one has to be believed the one time
 * it appears.
 */

export type HostAdvice = {
  show: boolean;
  severity?: "warning" | "critical";
  headline?: string;
  detail?: string;
  action?: string;
  stalled_minutes?: number;
};

export function useHostAdvice(projectId: string, pollMs = 60_000): HostAdvice {
  const [advice, setAdvice] = useState<HostAdvice>({ show: false });
  useEffect(() => {
    let alive = true;
    const read = async () => {
      try {
        const res = await fetch(
          `/api/host-advice?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" },
        );
        if (!res.ok || !alive) return;
        const body = (await res.json()) as HostAdvice;
        if (alive) setAdvice(body ?? { show: false });
      } catch {
        /* A diagnostic that breaks the page it is diagnosing is worse than no diagnostic. */
      }
    };
    read();
    const timer = setInterval(read, pollMs);
    return () => { alive = false; clearInterval(timer); };
  }, [projectId, pollMs]);
  return advice;
}

export function HostAdviceBanner(
  { advice, compact = false }: { advice: HostAdvice; compact?: boolean },
) {
  if (!advice?.show) return null;
  return (
    <div className="host-advice" data-severity={advice.severity ?? "warning"}
         data-testid="host-advice" role="status">
      {/* aria-hidden because the banner says what it is in words, and role="status"
          above already carries it. An icon that announces "warning" before a headline
          saying the computer is struggling has said the same thing twice. */}
      <Warning className="host-advice-icon" size={16} weight="bold"
               aria-hidden data-testid="host-advice-icon" />
      {/* The text keeps the layout it already had — a wrapping row — and the icon takes a
          column beside it. Adding the icon as a plain sibling would have let it wrap onto a
          line of its own once the detail got long, which is exactly the case that matters. */}
      <div className="host-advice-body">
        <b className="host-advice-head">{advice.headline}</b>
        {compact ? null : <span className="host-advice-detail">{advice.detail}</span>}
        <span className="host-advice-action">{advice.action}</span>
      </div>
    </div>
  );
}
