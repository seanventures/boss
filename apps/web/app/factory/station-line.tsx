"use client";

/**
 * The Line: a build's stations, each staffed by the face that is really there (factory spec §4.3).
 * One component, two densities: `row` on the Floor (24px faces, never wraps) and `header` in the
 * cockpit (labelled bays, the state word and time under each face).
 *
 *   Plan ─ Tests ─ Build● ─ Check ─ Review ─ PR ─ Preview ─ Ship
 *
 * NEVER A GUESS. A face is the act's role (`face: "agent"`), a hollow face only when the server
 * says the role is certain (`hollow`, never ringed), the BOSS glyph for machinery, `You` for a
 * person's press — else an empty slot. Quiet, budget-wait and blocked faces never move: motion
 * would claim work that is not happening.
 */
import { Hourglass } from "@phosphor-icons/react";
import React, { useRef } from "react";

import type { Act, ActStatus, AvatarState } from "../../lib/agent-world-types";
import { plural, type Station, type StationStatus } from "../../lib/factory-floor";
import { useIdentityVersion } from "../../lib/use-agent-identities";

import { ActFace } from "../agents/act-card";
import { AgFace } from "../agents/agent-face";
import { RunFace, nameOf } from "../agents/run-face";
import { span, type Band } from "../agents/live-util";

const AVATAR: Record<StationStatus, AvatarState> = {
  working: "working", needs_you: "needs_you", done: "done", stuck: "stuck",
  waiting: "paused", pending: "idle", skipped: "idle", failed: "failed", stopped: "stopped",
};
const ACT_STATUS: Record<StationStatus, ActStatus> = {
  working: "active", needs_you: "awaiting_input", done: "complete", stuck: "active",
  waiting: "pending", pending: "pending", skipped: "pending", failed: "error", stopped: "stale",
};

/** The station as the Act card reads it: the server's words, nothing made up. */
function actOf(s: Station): Act {
  return {
    actId: s.actId ?? "", roleKey: s.roleKey, kind: "stage", status: ACT_STATUS[s.status],
    rootActId: s.actId ?? "", handedBy: { kind: "boss" }, how: "next_stage",
    edgeLabel: s.edgeLabel ?? "", considered: [], scope: { skills: [], mcp: [] }, accountable: "",
    label: `${s.label} · ${s.word}`, links: {}, artifacts: [], tokensIn: 0, tokensOut: 0, costUsd: 0,
    reconstructed: false, startedAt: s.startedAt ?? "", endedAt: s.endedAt,
  };
}

/** Who stands at a station, by name: for aria labels and tooltips. */
export function whoAt(s: Station): string {
  if (s.face === "agent" && s.roleKey) return nameOf(s.roleKey).name;
  if (s.face === "hollow" && s.hollowRole) return nameOf(s.hollowRole).name;
  if (s.face === "boss") return "BOSS";
  if (s.face === "you") return "You";
  return "";
}

/** Whether a station's face would move (it counts against the Floor's ring budget). */
export function stationMoves(s: Station): boolean {
  return (s.status === "working" || s.status === "needs_you") && (s.face === "agent" || s.face === "you");
}

/** The station being worked, or where the build ended: needs-you first, then failed, stuck,
 *  stopped, working, waiting. */
export function activeStation(stations: Station[]): Station | null {
  for (const st of ["needs_you", "failed", "stuck", "stopped", "working", "waiting"] as StationStatus[]) {
    const hit = stations.find((s) => s.status === st);
    if (hit) return hit;
  }
  return null;
}

function seconds(s: Station, now: number | null): number | null {
  const start = s.startedAt ? Date.parse(s.startedAt) : NaN;
  if (!Number.isFinite(start)) return null;
  const end = s.endedAt ? Date.parse(s.endedAt) : now;
  if (end === null || !Number.isFinite(end)) return null;
  return Math.max(0, (end - start) / 1000);
}

/** BOSS waiting on a worker or on budget: an hourglass, never a face that claims work (walk D22). */
export function WaitGlyph({ size = 24, tip }: { size?: number; tip?: string }) {
  return (
    <span className="fx-wait" data-face="waiting" title={tip || "Waiting for a worker"} style={{ width: size, height: size }}>
      <Hourglass size={Math.max(12, size - 10)} weight="bold" aria-hidden />
    </span>
  );
}

function Face({ s, size, still, viewer }: { s: Station; size: 16 | 24 | 32; still: boolean; viewer: string }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const state = still && (s.status === "working" || s.status === "needs_you") ? "idle" : AVATAR[s.status];
  if (s.status === "waiting" && s.face !== "agent") return <WaitGlyph size={size} tip={s.tip} />;
  if (s.key === "review" && s.seats.length > 1) {
    // One face per seat, each in its own state — two seats of one role are two faces.
    const seats = s.seats.filter((x) => x.roleKey);
    const shown = seats.slice(0, 3);
    const seatState = (x: Station["seats"][number]): AvatarState =>
      still && (x.status === "working" || x.status === "needs_you") ? "idle" : AVATAR[x.status];
    return (
      <span className="fx-seats ag-stack" data-seats={s.seats.length} data-size={size === 16 ? 16 : 24} role="img"
            aria-label={seats.map((x) => `${nameOf(x.roleKey).name}${x.seat > 1 ? ` #${x.seat}` : ""}`).join(", ")}>
        {shown.map((x) => (
          <span key={x.actId || `seat-${x.seat}`} data-seat={x.seat} data-state={seatState(x)}>
            <AgFace roleKey={x.roleKey!} size={size === 16 ? 16 : 24} state={seatState(x)} decorative />
          </span>
        ))}
        {seats.length > shown.length ? <span className="ag-stack-more" aria-hidden>+{seats.length - shown.length}</span> : null}
      </span>
    );
  }
  if (s.face === "agent" && s.roleKey) {
    if (s.actId && size !== 16) {
      // A stuck station's act is still open (active): the face says stuck, not the act's working.
      return <ActFace act={actOf(s)} viewer={viewer} size={size === 32 ? 32 : 24}
                      state={still || s.status === "stuck" ? state : undefined} />;
    }
    return <RunFace roleKey={s.roleKey} size={size} state={state} standalone />;
  }
  if (s.face === "hollow" && s.hollowRole) {
    return (
      <span className="fx-hollow" title={`Next: ${nameOf(s.hollowRole).name} · ${s.label}`}>
        <RunFace roleKey={s.hollowRole} size={size} state="idle" standalone />
      </span>
    );
  }
  if (s.face === "boss") {
    return (
      <span className="fx-boss" title="BOSS machinery. No agent at this station.">
        <RunFace roleKey={null} variant="boss" size={size} state={state} standalone />
      </span>
    );
  }
  if (s.face === "you") return <RunFace roleKey={null} variant="you" size={size} state={state} standalone />;
  return <span className="fx-slot" aria-hidden />;
}

export type StationLineProps = {
  stations: Station[];
  density?: "row" | "header";
  band?: Band;
  /** Faces hold still (ring budget, quiet, budget wait, Paused). */
  still?: boolean;
  /** Header: a bay pressed (filter the Story to it). */
  onBay?: (key: Station["key"]) => void;
  /** Header: the bay the Story is filtered to. */
  selected?: Station["key"] | null;
  /** The client clock, for durations under header bays. */
  now?: number | null;
  viewer?: string;
  label?: string;
};

export function StationLine({ stations, density = "row", band = "wide", still = false, onBay, selected, now = null, viewer = "", label = "Stations" }: StationLineProps) {
  useIdentityVersion();
  // An edge draws in only for a station that becomes active while you watch.
  const seen = useRef<Set<string> | null>(null);
  const started = stations.filter((s) => s.status !== "pending" && s.status !== "skipped").map((s) => s.key);
  if (seen.current === null) seen.current = new Set(started);
  const fresh = new Set(started.filter((k) => !seen.current!.has(k)));
  const bays = useRef<(HTMLLIElement | null)[]>([]);

  const active = activeStation(stations);
  const done = stations.filter((s) => s.status === "done").length;
  const pending = stations.filter((s) => s.status === "pending").length;

  if (density === "row" && (band === "narrow" || band === "phone")) {
    return (
      <span className="fx-line fx-line-folded" role="list" aria-label={label} data-still={still || undefined}>
        {band === "narrow" && done ? <span role="listitem" className="fx-fold-done" title={`${plural(done, "station")} done`}>✓{done}</span> : null}
        {active ? (
          <span role="listitem" className="fx-bay" data-status={active.status} data-face={active.face} data-key={active.key}
                aria-label={[active.label, whoAt(active), active.word].filter(Boolean).join(", ")}>
            <Face s={active} size={24} still={still} viewer={viewer} />
          </span>
        ) : null}
        {band === "narrow" && pending ? <span role="listitem" className="fx-fold-next" title={`${plural(pending, "station")} next`}>{pending} next</span> : null}
      </span>
    );
  }

  function onKey(e: React.KeyboardEvent, i: number) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = Math.max(0, Math.min(stations.length - 1, i + (e.key === "ArrowRight" ? 1 : -1)));
    bays.current[next]?.querySelector<HTMLElement>("[data-bay-btn]")?.focus();
  }

  const header = density === "header";
  const focusIndex = Math.max(0, stations.findIndex((s) => s === active));
  return (
    <ol className="fx-line" data-density={density} aria-label={label} data-still={still || undefined}>
      {stations.map((s, i) => {
        const who = whoAt(s);
        const secs = header ? seconds(s, now) : null;
        // A time under a bay is how long the work there took or has taken. A STOPPED bay's act
        // was left open, so its clock would only measure how long ago the build stopped.
        const time = secs !== null && s.status !== "pending" && s.status !== "skipped" && s.status !== "stopped"
          ? span(secs) : "";
        const isActive = s === active;
        // Row density: every face 24px, so the storm reads at a glance (walk D22).
        const size: 16 | 24 | 32 = header ? 32 : 24;
        const ariaLabel = [s.label, who, s.word.toLowerCase(), time].filter(Boolean).join(", ");
        const dot = !header && (s.status === "pending" || s.status === "skipped") && !(s.key === "review" && s.seats.length > 1);
        return (
          <li key={`${s.key}-${i}`} ref={(el) => { bays.current[i] = el; }} className="fx-bay"
              data-key={s.key} data-status={s.status} data-face={s.face} data-active={isActive || undefined}
              data-role={s.face === "agent" ? s.roleKey ?? undefined : undefined}
              data-selected={selected === s.key || undefined} aria-label={ariaLabel}
              data-not-reached={s.notReached || undefined}
              title={!header ? [`${s.label} · ${s.word}`, who, s.tip].filter(Boolean).join(" · ") : undefined}>
            {i > 0 ? (
              <span className="ag-chain-edge fx-edge" data-new={fresh.has(s.key) ? "true" : undefined}
                    title={s.edgeLabel || undefined} aria-hidden>
                <i className="ag-chain-line" />
                {s.rounds > 1 ? <span className="fx-rounds" title={s.loopWith ? `${nameOf(s.loopWith).name} sent it back` : undefined}>×{s.rounds}</span> : null}
              </span>
            ) : null}
            {header ? (
              <span className="fx-bay-body">
                <button type="button" className="fx-bay-name" data-bay-btn tabIndex={i === focusIndex ? 0 : -1}
                        aria-pressed={selected === s.key} onKeyDown={(e) => onKey(e, i)}
                        onClick={() => onBay?.(s.key)} data-testid={`fx-bay-${s.key}`}>{s.label}</button>
                <span className="fx-bay-face">
                  {s.loopWith && s.rounds > 1 ? (
                    <span className="fx-loop" aria-label={`${who} and ${nameOf(s.loopWith).name}, ${plural(s.rounds, "round")}`}>
                      <Face s={s} size={32} still={still} viewer={viewer} />
                      <span className="ag-chain-swap" aria-hidden>↔</span>
                      <RunFace roleKey={s.loopWith} size={24} state="done" standalone />
                    </span>
                  ) : <Face s={s} size={32} still={still} viewer={viewer} />}
                  {s.status === "waiting" ? <Hourglass size={12} weight="bold" className="fx-wait" aria-hidden /> : null}
                </span>
                <span className="fx-bay-word" title={s.tip || undefined}>
                  {s.word}{time ? ` · ${time}` : ""}
                </span>
                {s.links.map((l) => (
                  l.url ? <a key={l.label} className="fx-bay-link" href={l.url} target="_blank" rel="noreferrer">{l.label}</a>
                    : <span key={l.label} className="fx-bay-link">{l.label}</span>
                ))}
              </span>
            ) : dot ? (
              <span className="fx-slot" aria-hidden />
            ) : (
              <span className="fx-bay-face">
                <Face s={s} size={size} still={still} viewer={viewer} />
                {s.status === "waiting" ? <Hourglass size={10} weight="bold" className="fx-wait" aria-hidden /> : null}
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
