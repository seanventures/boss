"use client";

/**
 * THE STATION STRIP, one component everywhere a piece of work is a row (ACCEPTANCE 05-32): the
 * Project's Work rows, Overview's Recent work, the Projects card and Code Tasks' build rows draw
 * the same six stations — P · D · B · R · PR · S — each done (green), now (teal, pulsing),
 * needs you (amber, round), failed (red), not started (faint) or skipped. Each node's tooltip
 * names the station, its state and who stood at it.
 *
 * `fromFactory` maps a build's own stations (Plan · You plan · Tests · Build · Check · Review ·
 * PR · Preview · Ship, lib/factory-floor) onto the six, so Code Tasks reads the same strip.
 */
import React from "react";

import type { Station, StationStatus } from "../lib/factory-floor";
import "./stations.css";

export type StepState = "done" | "now" | "waiting" | "todo" | "failed" | "skipped";
export type StripStage = { key: string; label: string; state: StepState; who?: string };

export const STATE_WORD: Record<StepState, string> = { done: "done", now: "in progress", waiting: "needs you", todo: "not started", failed: "failed", skipped: "skipped" };
const NAMES: Record<string, string> = { plan: "Plan", design: "Decide", build: "Build", review: "Review", pr: "PR", ship: "Ship" };

export function Stations({ stages, testid }: { stages: StripStage[]; testid?: string }) {
  if (!stages.length) return <span className="px-st" aria-hidden />;
  const said = stages.map((st) => `${NAMES[st.key] ?? (st.key || st.label)} ${STATE_WORD[st.state]}`).join(", ");
  return (
    <span className="px-st" role="img" aria-label={said} data-testid={testid}>
      {stages.map((st, i) => (
        <React.Fragment key={`${st.key}-${i}`}>
          {i ? <span className="px-st-w" data-s={stages[i - 1].state === "done" ? "done" : undefined} /> : null}
          <span className="px-st-n" data-s={st.state} data-station={st.key}
                title={[`${NAMES[st.key] ?? (st.key || st.label)}: ${STATE_WORD[st.state]}`, st.who].filter(Boolean).join(" · ")}>
            {st.label || st.key.slice(0, 1).toUpperCase()}</span>
        </React.Fragment>
      ))}
    </span>
  );
}

/** A build's factory stations → the six (contract §14 `stages`). */
const SIX: { key: string; label: string; from: Station["key"][] }[] = [
  { key: "plan", label: "P", from: ["plan"] }, { key: "design", label: "D", from: ["you_plan"] },
  { key: "build", label: "B", from: ["tests", "build", "check"] }, { key: "review", label: "R", from: ["review"] },
  { key: "pr", label: "PR", from: ["pr", "preview"] }, { key: "ship", label: "S", from: ["ship"] },
];
const STEP_OF: Record<StationStatus, StepState> = {
  working: "now", stuck: "now", waiting: "now", needs_you: "waiting", done: "done", pending: "todo", skipped: "skipped",
  failed: "failed", stopped: "failed",
};

export function fromFactory(stations: Station[], nameOf: (roleKey: string) => string = (r) => r): StripStage[] {
  return SIX.map((six) => {
    const got = stations.filter((s) => six.from.includes(s.key));
    const states = got.map((s) => STEP_OF[s.status] ?? "todo");
    const state: StepState = !got.length ? "skipped"
      : states.includes("failed") ? "failed" : states.includes("waiting") ? "waiting" : states.includes("now") ? "now"
      : states.every((x) => x === "skipped") ? "skipped" : states.every((x) => x === "done" || x === "skipped") ? "done"
      : states.some((x) => x === "done") ? "now" : "todo";
    const who = [...new Set(got.map((s) => (s.face === "agent" && s.roleKey ? nameOf(s.roleKey) : s.face === "you" ? "You" : s.face === "boss" ? "BOSS" : "")).filter(Boolean))].join(", ");
    return { key: six.key, label: six.label, state, who: who || undefined };
  });
}
