"use client";

import React, { useEffect, useState } from "react";

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
      <b className="host-advice-head">{advice.headline}</b>
      {compact ? null : <span className="host-advice-detail">{advice.detail}</span>}
      <span className="host-advice-action">{advice.action}</span>
    </div>
  );
}
