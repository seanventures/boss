"use client";

/**
 * SEV1–SEV4 and an alert's status, drawn the same way everywhere (BOSSAPP-598 feedback 5, 6).
 *
 *   SevFlag     ⬣ SEV1   ▲ SEV2   ◐ SEV3   ⓘ SEV4      a distinct glyph AND colour token per level;
 *               the tooltip says what it means and how it is routed.
 *   StatusChip  ● Firing (red) · ◔ Acknowledged · Param (amber) · ○ Pending (grey)
 *               ✓ Resolved 12:04 · 6 min (green) · ⊘ Silenced / Maintenance (muted)
 *
 * Colour is never the only signal: each level and each status has its own shape.
 */
import React from "react";

import { SEVERITIES, type Sev, type SevInfo, type Status } from "../../lib/app-alerts";

const GLYPH: Record<Sev, React.ReactNode> = {
  // SEV1: a filled octagon (stop / page someone)
  sev1: <path d="M5 1h6l4 4v6l-4 4H5l-4-4V5z" />,
  // SEV2: a filled triangle (warning)
  sev2: <path d="M8 1.5 15 14.5H1z" />,
  // SEV3: a half-filled circle (partial)
  sev3: <><circle cx="8" cy="8" r="6.2" fill="none" strokeWidth="1.6" className="obs-sev-ring" /><path d="M8 1.8a6.2 6.2 0 0 1 0 12.4z" /></>,
  // SEV4: an "i" in a ring (informational)
  sev4: <><circle cx="8" cy="8" r="6.2" fill="none" strokeWidth="1.6" className="obs-sev-ring" /><rect x="7.1" y="7" width="1.8" height="5" /><rect x="7.1" y="4" width="1.8" height="1.8" /></>,
};

export function sevInfo(sev: Sev, catalog: SevInfo[] = SEVERITIES): SevInfo {
  return catalog.find((s) => s.key === sev) ?? SEVERITIES.find((s) => s.key === sev) ?? SEVERITIES[2];
}

/** "SEV2 Major: Major degradation. High urgency: notifies at once" (≤ 14 words). */
export function sevTip(sev: Sev, catalog?: SevInfo[]): string {
  const s = sevInfo(sev, catalog);
  return `${s.label} ${s.word}: ${s.meaning}. ${s.routing}`;
}

export function SevGlyph({ sev }: { sev: Sev }) {
  return (
    <svg className="obs-sev-glyph" viewBox="0 0 16 16" width="12" height="12" aria-hidden focusable="false">{GLYPH[sev]}</svg>
  );
}

export function SevFlag({ sev, catalog }: { sev: Sev; catalog?: SevInfo[] }) {
  const s = sevInfo(sev, catalog);
  return (
    <span className="obs-sev" data-sev={sev} data-testid="obs-sev" title={sevTip(sev, catalog)} aria-label={`${s.label} ${s.word}`}>
      <SevGlyph sev={sev} />
      <span>{s.label}</span>
    </span>
  );
}

const STATUS_GLYPH: Record<Status["key"], React.ReactNode> = {
  firing: <circle cx="8" cy="8" r="6" />,
  acknowledged: <><circle cx="8" cy="8" r="6" fill="none" strokeWidth="1.8" className="obs-sev-ring" /><path d="M8 2a6 6 0 0 1 6 6H8z" /></>,
  pending: <circle cx="8" cy="8" r="5.4" fill="none" strokeWidth="1.8" className="obs-sev-ring" />,
  resolved: <path d="M3 8.5 6.5 12 13 4.5" fill="none" strokeWidth="2.2" className="obs-sev-ring" />,
  recovered: <path d="M3 8.5 6.5 12 13 4.5" fill="none" strokeWidth="2.2" className="obs-sev-ring" />,
  silenced: <><circle cx="8" cy="8" r="6" fill="none" strokeWidth="1.6" className="obs-sev-ring" /><path d="M3.8 12.2 12.2 3.8" strokeWidth="1.6" className="obs-sev-ring" /></>,
  maintenance: <><circle cx="8" cy="8" r="6" fill="none" strokeWidth="1.6" className="obs-sev-ring" /><path d="M3.8 12.2 12.2 3.8" strokeWidth="1.6" className="obs-sev-ring" /></>,
};

const STATUS_TIPS: Record<Status["key"], string> = {
  firing: "Firing: needs attention, nobody has acknowledged it",
  acknowledged: "Acknowledged: someone is on it; repeats stopped",
  pending: "Pending: breached, waiting out its For before firing",
  resolved: "Resolved: it is closed",
  recovered: "Recovered: nothing is firing now",
  silenced: "Silenced: it still evaluates, nobody is told",
  maintenance: "Maintenance window: muted while it lasts",
};

/** The one chip that says whether something is still open. `extra` follows the word (time · duration). */
export function StatusChip({ status, extra, extraTip, testId = "obs-status" }: {
  status: Status; extra?: string | null; extraTip?: string | null; testId?: string;
}) {
  const tip = [status.tip ?? STATUS_TIPS[status.key], extraTip].filter(Boolean).join(". ");
  return (
    <span className="obs-status" data-status={status.key} data-tone={status.tone} data-testid={testId} title={tip}>
      <svg className="obs-sev-glyph" viewBox="0 0 16 16" width="11" height="11" aria-hidden focusable="false">{STATUS_GLYPH[status.key]}</svg>
      <span data-copy="data">{status.word}{extra ? ` ${extra}` : ""}</span>
    </span>
  );
}
