"use client";

/** App › Security over the Factory, as a sheet: Esc or the backdrop closes it. `?security=<app id>`
 *  opens it on that app, so a link (or the review's gate) lands on the checklist. */
import React, { useEffect } from "react";

import { AppSecurity } from "./app-security";

export function readSecurityParam(): string | null {
  try { return new URLSearchParams(window.location.search).get("security") || null; } catch { return null; }
}

export function writeSecurityParam(appId: string | null): void {
  try {
    const url = new URL(window.location.href);
    if (appId) url.searchParams.set("security", appId); else url.searchParams.delete("security");
    window.history.replaceState(null, "", url.toString());
  } catch { /* not a browser */ }
}

export function SecuritySheet({ projectId, appKey, appName, onClose }: {
  projectId: string; appKey: string; appName?: string; onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); onClose(); } };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fx-sheet-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="fx-sheet sec-sheet" role="dialog" aria-modal="true" aria-label={`Security of ${appName ?? appKey}`}
               data-testid="sec-sheet">
        <button type="button" className="ag-act-close sec-close" aria-label="Close" onClick={onClose}>×</button>
        <AppSecurity projectId={projectId} appKey={appKey} appName={appName ?? appKey} />
      </section>
    </div>
  );
}
