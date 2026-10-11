"use client";

import React, { useEffect, useState } from "react";
import type { CommandCenterView } from "../../lib/command-center-view";
import { lastSeenKey, returningBrief } from "../../lib/returning-brief";
import { Go } from "./hero";
import styles from "./returning-brief.module.css";

/** A compact returning-work brief. Opening a record never retries or approves work. */
export function ReturningBrief({ view, projectId, orgId, viewer, reachable, visible, container }: {
  view: CommandCenterView | null; projectId: string; orgId?: string;
  viewer: string; reachable: boolean; visible: boolean;
  container: React.RefObject<HTMLDivElement | null>;
}) {
  const key = lastSeenKey(orgId, projectId, viewer);
  const [prior, setPrior] = useState<{ key: string; at: string | null } | null>(null);
  useEffect(() => {
    let at: string | null = null;
    try { at = window.localStorage.getItem(key); } catch { /* storage is optional */ }
    setPrior({ key, at });
  }, [key]);
  useEffect(() => {
    if (prior?.key !== key || !reachable || !visible || !container.current?.getClientRects().length
        || !view || view.projectId !== projectId
        || !Number.isFinite(Date.parse(view.generatedAt))) return;
    try {
      const recorded = window.localStorage.getItem(key);
      if (Date.parse(recorded ?? "") > Date.parse(view.generatedAt)) return;
      window.localStorage.setItem(key, view.generatedAt);
    } catch { /* storage is optional */ }
  }, [prior, key, reachable, visible, view, projectId, container]);
  const rows = returningBrief(view, projectId, prior?.key === key ? prior.at : null);
  if (!rows.length) return null;
  return <details className={styles.brief} data-testid="returning-brief" data-stale={reachable ? undefined : "true"}>
    <summary>Since your last visit <span>{rows.length} recent update{rows.length === 1 ? "" : "s"}</span></summary>
    <p>{reachable ? "Recent recorded work. Open a record for its current state and evidence."
      : "Showing the last available records. Current state is unavailable."}</p>
    <ol>{rows.map((row) => <li key={row.id}>
      <span>{row.text}</span>
      <Go patch={row.patch} orgId={orgId} title="Open the recorded attempt and its history">Open record</Go>
    </li>)}</ol>
    <p className={styles.note}>This is a short history, not a complete activity log. Recorded completion does not mean human acceptance.</p>
  </details>;
}
