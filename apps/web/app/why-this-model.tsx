"use client";

/**
 * "Why this model" (docs/product/MODEL_ROUTER.md): one route the model router chose — the tier
 * as a chip, the source · model, and on demand the reasons in the order they applied. Shared by
 * the build cockpit and a workflow step, so both say it the same way.
 */
import React, { useEffect, useId, useState } from "react";

import { parseRoutes, shortLabel, type RouteRow } from "../lib/model-routes";
import { useOnScreen } from "../lib/area-shown";

export type { RouteRow };

export function TierChip({ tier, label }: { tier?: string | null; label?: string | null }) {
  if (!tier) return null;
  return <span className={`wm-tier wm-${tier}`} data-testid="wm-tier">{label ?? tier}</span>;
}

export function WhyThisModel({ route, title }: { route: RouteRow; title?: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <span className="wm" data-testid={`wm-route-${route.ref ?? route.step}`}>
      {title ? <span className="wm-step">{title}</span> : null}
      <TierChip tier={route.tier} label={route.tier_label} />
      <span className="wm-main">
        <span className="wm-label" data-user-text title={route.label ?? undefined}>
          {shortLabel(route.label ?? route.engine ?? "")}
        </span>
        {route.escalation ? <span className="wm-flag" title={route.escalation}>↑ escalated</span> : null}
        {route.moved_by_headroom ? (
          <span className="wm-flag" title={`Moved off a subscription near its limit, to ${route.label ?? "another source"}`}>moved</span>
        ) : null}
      </span>
      {route.why.length ? (
        <button type="button" className="wm-why-btn" aria-expanded={open} aria-controls={id}
                data-testid="wm-why" onClick={() => setOpen((v) => !v)}
                title="Why this model">Why</button>
      ) : null}
      {open ? (
        <ol id={id} className="wm-why" data-testid="wm-why-list">
          {route.why.map((line, i) => <li key={i}>{line}</li>)}
        </ol>
      ) : null}
    </span>
  );
}

/** A run's routes, read once and again while it is live (every 15s, only when visible). */
export function useRunRoutes(projectId: string, unitId: string | undefined, live = false): RouteRow[] {
  const [routes, setRoutes] = useState<RouteRow[]>([]);
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    if (!unitId || !projectId) { setRoutes([]); return; }
    let stopped = false;
    const read = async () => {
      try {
        const r = await fetch(`/api/model-routes?project_id=${encodeURIComponent(projectId)}`
          + `&unit_id=${encodeURIComponent(unitId)}`, { cache: "no-store" });
        if (!r.ok) return;
        const body = await r.json();
        if (!stopped) setRoutes(parseRoutes((body as { routes?: unknown }).routes));
      } catch { /* "Why this model" is an extra; the step renders without it */ }
    };
    void read();
    if (!live || !onScreen) return () => { stopped = true; };
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void read();
    }, 15_000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [projectId, unitId, live, onScreen]);
  return routes;
}

/** The route a workflow step ran on: the newest whose ref is that step. */
export function routeForStep(routes: RouteRow[], stepId: string): RouteRow | null {
  for (let i = routes.length - 1; i >= 0; i -= 1) if (routes[i].ref === stepId) return routes[i];
  return null;
}
