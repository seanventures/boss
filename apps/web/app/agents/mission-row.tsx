"use client";

/**
 * One running workflow, as a lane of the faces that worked it (agents-home spec §4.3). Shared by
 * Agents › Live (density "lane") and the Command Center's Running now (density "row").
 *
 *   Ship it #182   You → Juno → Atlas → (Forge●) → Vera     Coding · step 4 of 7 · 12m   ↗
 *
 * NOTHING IS ESTIMATED. `step k of n` and the time on the current step, both from the run's own
 * rows; never a percent or a finish time. The active hop is in its real state; a run waiting
 * on you ends in an amber `You` hop that takes you to its card. Upcoming named steps show as
 * hollow faces only when the workflow names them. A hop that arrives while you watch draws its
 * edge in once (the chain's own `data-new`); nothing else on the row moves.
 */
import { MapPin, Sparkle } from "@phosphor-icons/react";
import React, { useRef } from "react";

import type { Act, ActStatus } from "../../lib/agent-world-types";
import type { Mission, MissionHop } from "../../lib/agents-live-board";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { ActFace } from "./act-card";
import { RunFace, nameOf } from "./run-face";
import { ANIMATED_STATES, linkTo, navigate, plainClick, runPatch, span, type Band } from "./live-util";
import { localFull } from "../../lib/local-time";

type Hop = Extract<MissionHop, { kind: "hop" }>;

const STATUS_OF: Record<string, ActStatus> = {
  working: "active", thinking: "active", speaking: "active", needs_you: "awaiting_input",
  done: "complete", stuck: "error", idle: "pending", paused: "pending", retired: "pending",
};

/** The hop as the Act card reads it: the server's words only, nothing made up. */
function actOf(hop: Hop, mission: Mission): Act {
  return {
    actId: hop.actId, roleKey: hop.roleKey, kind: hop.roleKey ? "step" : "run",
    status: STATUS_OF[hop.state] ?? "pending", rootActId: hop.actId,
    handedBy: hop.person ? { id: hop.person, kind: "person" } : { kind: "boss" },
    how: (hop.how as Act["how"]) ?? "named", edgeLabel: hop.edgeLabel, considered: [],
    scope: { skills: [], mcp: [] }, accountable: hop.person ?? mission.actor ?? "",
    label: hop.label, links: { workflow_run_id: mission.runId }, artifacts: [],
    tokensIn: 0, tokensOut: 0, costUsd: 0, reconstructed: false, startedAt: "",
  };
}

/** The lane as drawn (walk D11). A wait for a person is drawn ONCE, as the amber You / A person
 *  hop the row adds from its state: an agent's hop is never ringed amber for it. The person who
 *  started the run is never "stuck" — a run that failed failed at a step, not at its start. */
export function laneHops(m: Pick<Mission, "hops" | "state">): MissionHop[] {
  return m.hops.map((h) => {
    if (h.kind !== "hop") return h;
    if (h.state === "needs_you") return { ...h, state: "idle" };
    if (!h.roleKey && h.state === "stuck") return { ...h, state: "done" };
    return h;
  });
}

/** Does this face match the header's `Find an agent` text? Empty text matches everyone. */
export function faceMatches(roleKey: string | null, query: string | undefined): boolean {
  const q = (query ?? "").trim().toLowerCase();
  if (!q) return true;
  if (!roleKey) return false;
  const n = nameOf(roleKey);
  return [roleKey, n.name, n.role, n.callsign ?? ""].some((x) => x.toLowerCase().includes(q));
}

/** A tooltip of ours stays ≤14 words (walk D20); a long server sentence is clipped. */
export function clipWords(text: string | null | undefined, max = 14): string | undefined {
  const words = String(text ?? "").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return undefined;
  return words.length > max ? `${words.slice(0, max).join(" ")}…` : words.join(" ");
}

/** The hop doing the work now: the last one in a live state, else the last one. */
export function currentHopIndex(hops: MissionHop[]): number {
  for (let i = hops.length - 1; i >= 0; i -= 1) {
    const h = hops[i];
    if (h.kind === "hop" && ANIMATED_STATES.has(h.state)) return i;
    if (h.kind === "hop" && h.state === "stuck") return i;
  }
  for (let i = hops.length - 1; i >= 0; i -= 1) if (hops[i].kind === "hop") return i;
  return -1;
}

/** Whether this row's current face moves (it counts against Live's ring budget). */
export function rowAnimates(m: Mission): boolean {
  if (m.state === "waiting_you") return true;
  const hops = laneHops(m);
  const i = currentHopIndex(hops);
  const h = i >= 0 ? hops[i] : null;
  return !!h && h.kind === "hop" && ANIMATED_STATES.has(h.state);
}

/** Fold a long lane (spec §4.3, §5.3): first `head`, `+n`, last `tail`, the current always kept. */
export function foldHops(hops: MissionHop[], band: Band): MissionHop[] {
  const limit = band === "wide" ? 7 : band === "mid" ? 6 : 4;
  if (hops.length <= limit) return hops;
  const [head, tail] = band === "wide" ? [2, 4] : band === "mid" ? [2, 3] : [1, 2];
  const cur = currentHopIndex(hops);
  const keep = new Set<number>();
  for (let i = 0; i < head; i += 1) keep.add(i);
  for (let i = hops.length - tail; i < hops.length; i += 1) keep.add(i);
  if (cur >= 0) keep.add(cur);
  const out: MissionHop[] = [];
  let hidden = 0;
  hops.forEach((h, i) => {
    if (keep.has(i)) {
      if (hidden) { out.push({ kind: "fold", hidden }); hidden = 0; }
      out.push(h);
    } else hidden += h.kind === "fold" ? h.hidden : 1;
  });
  if (hidden) out.push({ kind: "fold", hidden });
  return out;
}

/** The "Now" words, exactly as the spec says them. Each part its own run of text. */
export function nowParts(m: Mission, stepSeconds: number | null): { text: string; tip?: string; cls?: string }[] {
  const step = m.stepCount ? { text: `step ${m.stepIndex} of ${m.stepCount}`, tip: "Steps done so far. BOSS doesn't guess finish times.", cls: "ag-mission-step" } : null;
  const time = stepSeconds !== null ? { text: span(stepSeconds), cls: "ag-mission-time" } : null;
  const parts = (...xs: ({ text: string; tip?: string; cls?: string } | null)[]) => xs.filter((x): x is { text: string; tip?: string; cls?: string } => !!x && !!x.text);
  switch (m.state) {
    case "waiting_you": return parts({ text: "Waiting on you", cls: "ag-mission-you" }, step, time);
    case "waiting_person": {
      const who = m.waitingOnRole ? nameOf(m.waitingOnRole).role : "a person";
      return parts({ text: `Waiting on ${who}` }, step);
    }
    case "waiting": return parts({ text: m.waitingOn ? `Waiting · ${m.waitingOn}` : "Waiting" }, step, time);
    case "stopped": return parts({ text: `Failed · step ${m.stepIndex}`, tip: clipWords(m.error), cls: "ag-mission-failed" });
    case "blocked": return parts({ text: "Blocked", tip: clipWords(m.blockedReason), cls: "ag-mission-blocked" }, step);
    case "stalled": return parts({ text: "Stalled", tip: "No progress for over 7 days. Resume or close it.", cls: "ag-mission-blocked" }, step);
    case "done": return parts({ text: `Finished · ${m.stepCount} steps${m.elapsedSeconds ? ` · ${span(m.elapsedSeconds)}` : ""}` });
    default: return parts({ text: m.verb || "Working" }, step, time);
  }
}

export type MissionRowProps = {
  mission: Mission;
  density?: "lane" | "row";
  band?: Band;
  /** The client clock (ms), for elapsed times between polls; null before mount. */
  now: number | null;
  /** The face may move (Live's ring budget). */
  still?: boolean;
  /** The amber `You` hop was pressed: focus that attention card. */
  onYou?: (attentionId: string) => void;
  /** For links that open in a new tab (presses navigate in place). */
  orgId?: string | null;
  /** The header's `Find an agent` text: faces that don't match dim (walk D10). */
  search?: string;
};

function stepSecondsOf(m: Mission, now: number | null): number | null {
  if (now !== null && m.stepSince) {
    const t = Date.parse(m.stepSince);
    if (Number.isFinite(t)) return Math.max(0, (now - t) / 1000);
  }
  return m.stepElapsedSeconds ?? null;
}

function totalSecondsOf(m: Mission, now: number | null): number | null {
  if (now !== null && m.startedAt && m.state !== "done") {
    const t = Date.parse(m.startedAt);
    if (Number.isFinite(t)) return Math.max(0, (now - t) / 1000);
  }
  return m.elapsedSeconds ?? null;
}

export function MissionRow({ mission: m, density = "lane", band = "wide", now, still, onYou, orgId, search }: MissionRowProps) {
  useIdentityVersion();
  // The hops here when the row first drew: only a hop that arrives while you watch draws in.
  const seen = useRef<Set<string> | null>(null);
  const hopIds = m.hops.filter((h): h is Hop => h.kind === "hop").map((h) => h.actId);
  if (seen.current === null) seen.current = new Set(hopIds);
  const fresh = new Set(hopIds.filter((id) => !seen.current!.has(id)));

  const stepSeconds = stepSecondsOf(m, now);
  const total = totalSecondsOf(m, now);
  const parts = nowParts(m, stepSeconds);
  const name = m.subject || m.workflowName;
  const nameTip = [`${m.workflowName}${m.version ? ` v${m.version}` : ""}`,
                   m.actor ? `started by ${m.actor}` : "", m.startedAt ? localFull(m.startedAt) : ""]
    .filter(Boolean).join(" · ");
  const cost = m.costUsd > 0 ? `$${m.costUsd.toFixed(2)}` : "";
  const runHref = linkTo(runPatch(m.runId), orgId);
  const mapPatch = { area: "workforce", view: "org", run: m.runId, role: null, focus: null };

  const hops = laneHops(m);
  const cur = currentHopIndex(hops);
  const curHop = cur >= 0 ? hops[cur] : null;
  let shown: MissionHop[] = band === "phone" && density === "lane"
    ? (curHop ? [curHop] : [])
    : foldHops(hops, band);
  const named = density === "lane" && band !== "phone";
  if (density === "row") shown = curHop ? [curHop] : [];
  const youHop = m.state === "waiting_you" || m.state === "waiting_person";

  function openRow(e: React.MouseEvent) {
    const t = e.target as HTMLElement;
    if (t.closest("a, button, [role='dialog']")) return;
    navigate(runPatch(m.runId));
  }

  return (
    <li className="ag-mission" data-testid={`ag-mission-${m.runId}`} data-state={m.state}
        data-density={density} data-still={still || undefined}
        title={cost && band === "narrow" ? `${cost} · Metered model spend so far` : undefined}
        onClick={openRow}>
      <span className="ag-mission-name">
        <a href={runHref} className="ag-trunc" title={nameTip}
           onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(runPatch(m.runId)); } }}>
          <span data-user-text>{name}</span>
        </a>
        {density === "row" && (m.stepLabel || curHop) ? (
          <span className="ag-mission-sub ag-trunc" data-user-text
                title={[m.subject ? m.workflowName : "", m.stepLabel].filter(Boolean).join(" · ") || undefined}>
            {[m.stepLabel, curHop && curHop.kind === "hop" && curHop.roleKey ? nameOf(curHop.roleKey).role : ""]
              .filter(Boolean).join(" · ")}
          </span>
        ) : null}
      </span>

      <ol className="ag-mission-lane" aria-label={`Who worked ${name}`}>
        {shown.map((h, i) => {
          if (h.kind === "fold") {
            return <li key={`fold-${i}`} className="ag-mission-fold" title={`${h.hidden} more`}>+{h.hidden}</li>;
          }
          if (h.kind === "loop") {
            const a = nameOf(h.a).name; const b = nameOf(h.b).name;
            return (
              <li key={`loop-${i}`} className="ag-mission-hop ag-mission-loop" aria-label={`${a} and ${b}, ${h.rounds} rounds`}>
                {i > 0 ? <span className="ag-chain-edge" aria-hidden><i className="ag-chain-line" /><span className="ag-chain-arrow">→</span></span> : null}
                <RunFace roleKey={h.a} size={24} /><span className="ag-chain-swap" aria-hidden>↔</span>
                <RunFace roleKey={h.b} size={24} /><span className="ag-chain-rounds">×{h.rounds}</span>
              </li>
            );
          }
          const who = h.roleKey ? nameOf(h.roleKey).name : h.person ? "You" : "BOSS";
          const isCur = h === curHop;
          return (
            <li key={h.actId || `hop-${i}`} className="ag-mission-hop" data-current={isCur || undefined}
                data-dim={faceMatches(h.roleKey, search) ? undefined : "true"}
                aria-label={`${who}, ${h.state.replace("_", " ")}, ${h.label}`}>
              {i > 0 && density === "lane" ? (
                <span className="ag-chain-edge" data-new={fresh.has(h.actId) ? "true" : undefined}
                      data-tip={h.edgeLabel || undefined} title={h.edgeLabel || undefined} aria-hidden>
                  <i className="ag-chain-line" /><span className="ag-chain-arrow">→</span>
                </span>
              ) : null}
              <span className="ag-mission-who">
                <ActFace act={actOf(h, m)} viewer={h.person ?? ""} size={24} />
                {named ? <span className="ag-mission-hop-name" aria-hidden>{who}</span> : null}
              </span>
            </li>
          );
        })}
        {youHop && density === "lane" ? (
          <li className="ag-mission-hop ag-mission-you-hop" aria-label={m.state === "waiting_you" ? "You, needs you" : "A person, needs you"}>
            {shown.length ? <span className="ag-chain-edge" aria-hidden><i className="ag-chain-line" /><span className="ag-chain-arrow">→</span></span> : null}
            {m.state === "waiting_you" && m.attentionId ? (
              <button type="button" className="ag-face-btn ag-mission-who" data-testid={`ag-mission-you-${m.runId}`}
                      title="Waiting on you · open the card" onClick={() => onYou?.(m.attentionId!)}>
                <RunFace roleKey={null} variant="you" size={24} state="needs_you" />
                {named ? <span className="ag-mission-hop-name" aria-hidden>You</span> : null}
              </button>
            ) : (
              <span className="ag-mission-who">
                <RunFace roleKey={null} variant={m.state === "waiting_you" ? "you" : "person"} size={24} state="needs_you" standalone />
                {named ? <span className="ag-mission-hop-name" aria-hidden>{m.state === "waiting_you" ? "You" : "A person"}</span> : null}
              </span>
            )}
          </li>
        ) : null}
        {density === "lane" && band !== "phone" ? m.next.map((n, i) => (
          <li key={`next-${i}`} className="ag-mission-next" title={`Next: ${n.roleKey ? nameOf(n.roleKey).name : "Auto"} · ${n.stepLabel}`}
              aria-label={`Next: ${n.roleKey ? nameOf(n.roleKey).name : "Auto"}, ${n.stepLabel}`}>
            {n.roleKey ? <RunFace roleKey={n.roleKey} size={16} /> : <Sparkle size={12} weight="bold" aria-hidden />}
          </li>
        )) : null}
      </ol>

      <span className="ag-mission-now" title={total !== null ? `Run time ${span(total)}` : undefined}>
        {parts.map((p, i) => (
          <React.Fragment key={i}>
            {i > 0 ? <span aria-hidden> · </span> : null}
            <span className={p.cls} title={p.tip}>{p.text}</span>
          </React.Fragment>
        ))}
      </span>

      {cost ? <span className="ag-mission-cost" title="Metered model spend so far">{cost}</span> : <span className="ag-mission-cost" aria-hidden />}

      <span className="ag-mission-acts">
        {density === "lane" ? (
          <a className="ag-icon-btn" href={linkTo(mapPatch, orgId)} aria-label="Show on map" data-testid={`ag-mission-map-${m.runId}`}
             title="See this run's agents on the team map"
             onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(mapPatch); } }}>
            <MapPin size={14} weight="bold" aria-hidden />
          </a>
        ) : null}
        <a className="ag-link" href={runHref} data-testid={`ag-mission-open-${m.runId}`}
           onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(runPatch(m.runId)); } }}>Open run ↗</a>
      </span>
    </li>
  );
}
