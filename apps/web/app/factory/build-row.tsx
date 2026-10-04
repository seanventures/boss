"use client";

/**
 * One build on the Floor (factory spec §4.2.2): the MissionRow look, with the build drawn as its
 * line of stations.
 *
 *   Invoice export   (Atlas✓)─(Vera✓)─(Forge●)─ · ─ · ─ ·    Coding · 2 of 8 · 9m   $0.31   ↗
 *
 * Every word is the server's (`word`, `tip`); `k of n` counts stations, never a percent; the
 * time ticks locally from `started_at`. Pressing a face opens its Act card; pressing anywhere
 * else opens the cockpit.
 */
import React from "react";

import type { BuildRow as Build } from "../../lib/factory-floor";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { localTime, span, type Band } from "../agents/live-util";
import { StationLine, WaitGlyph, activeStation, stationMoves } from "./station-line";

/** States whose face never moves: motion would claim work that is not happening. */
export const STILL_STATES = new Set(["quiet", "budget_wait", "blocked", "budget_refused", "needs_waiver", "queued"]);

/** Whether this row's face would move (it counts against the Floor's ring budget). */
export function buildAnimates(b: Build): boolean {
  if (STILL_STATES.has(b.state)) return false;
  const a = activeStation(b.stations);
  return !!a && stationMoves(a);
}

const COUNTED = new Set(["building", "queued", "needs_plan"]);
const TIMED = new Set(["building", "queued", "budget_wait", "needs_plan"]);

/** The "now" words, each its own run of text: `Coding · 2 of 8 · 9m`. */
export function nowParts(b: Build, now: number | null): { text: string; tip?: string; cls?: string }[] {
  const out: { text: string; tip?: string; cls?: string }[] = [
    { text: b.word, tip: b.tip, cls: `fx-word fx-word-${b.state}` },
  ];
  if (COUNTED.has(b.state) && b.n > 0 && b.rounds === 0) {
    out.push({ text: `${b.k} of ${b.n}`, tip: "Stations done. BOSS doesn't guess finish times.", cls: "fx-kn" });
  }
  if (TIMED.has(b.state)) {
    const since = b.state === "budget_wait" ? b.stationSince : b.startedAt;
    const t = since ? Date.parse(since) : NaN;
    if (now !== null && Number.isFinite(t)) out.push({ text: span((now - t) / 1000), cls: "fx-time" });
  }
  return out;
}

export type BuildRowProps = {
  build: Build;
  density?: "row" | "compact";
  band?: Band;
  now: number | null;
  still?: boolean;
  onOpen: (runId: string, from: HTMLElement | null) => void;
};

export function BuildRow({ build: b, density = "row", band = "wide", now, still, onOpen }: BuildRowProps) {
  useIdentityVersion();
  const frozen = still || STILL_STATES.has(b.state);
  const parts = nowParts(b, now);
  const cost = b.metered && b.costUsd > 0 ? `$${b.costUsd.toFixed(2)}` : "";
  const started = localTime(b.startedAt);
  const nameTip = [b.title, `attempt ${b.attempt}`, b.actor ? `started by ${b.actor}` : "", started]
    .filter(Boolean).join(" · ");
  const phone = band === "phone";

  function open(e: React.MouseEvent<HTMLElement>) {
    const t = e.target as HTMLElement;
    if (t.closest("a, [role='dialog'], [data-testid='ag-act-face']")) return;
    if (t.closest("button") && !t.closest("[data-fx-open]")) return;
    onOpen(b.runId, e.currentTarget.closest("li"));
  }

  return (
    <li className="fx-row" data-testid={`fx-build-row-${b.runId}`} data-state={b.state} data-density={density}
        data-still={frozen || undefined} data-band={band} onClick={open}
        title={cost && (band === "narrow" || phone) ? `${cost} · Metered model spend so far` : undefined}>
      <span className="fx-row-name">
        <button type="button" className="fx-row-open ag-trunc" data-fx-open title={nameTip}
                data-testid={`fx-build-open-${b.runId}`}>
          <span data-user-text>{density === "compact" ? `Attempt ${b.attempt}` : b.app}</span>
        </button>
      </span>
      <span className="fx-row-line">
        {(b.state === "queued" || b.state === "budget_wait") && !activeStation(b.stations)
          ? <WaitGlyph size={24} tip={b.tip || (b.state === "queued" ? "Waiting for a worker" : b.word)} /> : null}
        <StationLine stations={b.stations} density="row" band={band} still={frozen}
                     label={`Stations of ${b.app}`} />
      </span>
      <span className="fx-row-now">
        {parts.map((p, i) => (
          <React.Fragment key={i}>
            {i > 0 ? <span aria-hidden> · </span> : null}
            <span className={p.cls} title={p.tip}>{p.text}</span>
          </React.Fragment>
        ))}
      </span>
      {cost && band !== "narrow" && !phone
        ? <span className="fx-row-cost" title="Metered model spend so far">{cost}</span>
        : <span className="fx-row-cost" aria-hidden />}
      <button type="button" className="ag-icon-btn fx-row-go" data-fx-open aria-label={`Open ${b.app}`} title="Open the build">↗</button>
    </li>
  );
}
