"use client";

/**
 * The Ask dock, with the agent in it (spec §3.10).
 *
 * The head is the agent: its face ringed `thinking` while a turn is in flight and `speaking`
 * while its words arrive, its name, the `AI` badge, its tagline, and what the conversation is
 * about (`About: Build · Ship it ×`). Each answer carries the agent's face in a 24px gutter.
 * Work the agent cites renders as chips that open the Act card (`I ran this · 14:02 ↗`).
 *
 * THE DOCK CARRIES THE ROLE KEY. A display name ("QA Lead") resolved through a table of titles
 * drifts — the same agent was a different colour in the dock than on its step. The key is what
 * the face, the name and the turn use; the display name is only a fallback for older doors.
 */
import { X } from "@phosphor-icons/react";
import React, { useCallback, useEffect, useRef, useState } from "react";

import { agentIdentity, type AvatarState } from "../../lib/agent-world-types";
import { ActFace } from "./act-card";
import { shortLabel } from "./chain-model";
import { useActDetail } from "./run-acts";
import { AiBadge, nameOf, RunFace, STATE_WORD } from "./run-face";

/**
 * The role key a dock is talking to, as the turn sends it: the key it was opened with, else its
 * name resolved by the kit. Never a guess — a key made up from a display name ("QA Lead" →
 * `qa_lead`, while the agent is `qa`) would address an agent that does not exist.
 */
export function dockRoleKey(roleKey: string | undefined, roleName: string | undefined): string | null {
  const key = (roleKey ?? "").trim();
  if (key) return key;
  const name = (roleName ?? "").trim();
  if (!name) return null;
  try {
    return agentIdentity(name).roleKey || null;
  } catch {
    return null;   // the kit's resolver is not mounted: no key, and the turn says so by omission
  }
}

/** The key the dock draws a face and a name from: the real key, else the name made key-shaped.
 *  Local display only; it never leaves the console. */
export function dockFaceKey(roleKey: string | undefined, roleName: string | undefined): string | null {
  const real = dockRoleKey(roleKey, roleName);
  if (real) return real;
  const name = (roleName ?? "").trim();
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || null;
}

/** How long a face keeps speaking after the last chunk of words arrived (spec §7). */
export const SPEAK_DECAY_MS = 300;

/**
 * The dock agent's state from what is really happening: `thinking` while a turn is in flight
 * and nothing has arrived, `speaking` for SPEAK_DECAY_MS after each chunk of its words, `idle`
 * otherwise. `chunk()` is called once per arrival — a whole reply today, each token when the
 * turn streams.
 */
export function useDockSpeech(inFlight: boolean): { state: AvatarState; chunk(): void } {
  const [speaking, setSpeaking] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  const chunk = useCallback(() => {
    setSpeaking(true);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setSpeaking(false), SPEAK_DECAY_MS);
  }, []);
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
  return { state: speaking ? "speaking" : inFlight ? "thinking" : "idle", chunk };
}

export function DockAgentHead({ roleKey, roleName, state, projectId, actId, onClearAct }: {
  roleKey: string | null; roleName?: string; state: AvatarState; projectId: string;
  actId?: string | null; onClearAct?(): void;
}) {
  const named = roleKey ? nameOf(roleKey) : { name: roleName || "BOSS", role: roleName || "BOSS", tagline: undefined, callsign: undefined };
  const about = useActDetail(projectId, actId ?? null);
  return (
    <div className="ag-dock-head" data-testid="ag-dock-head" data-state={state}>
      <RunFace roleKey={roleKey} variant={roleKey ? "agent" : "boss"} size={32} state={state} standalone
               label={named.name} />
      <span className="ag-dock-who">
        <span className="ag-dock-name">
          <strong data-testid="ag-dock-name">{named.name}</strong>
          {roleKey ? <AiBadge /> : null}
          <span className="ag-dock-state" data-testid="ag-dock-state" aria-live="polite">
            {state === "idle" ? "" : STATE_WORD[state]}
          </span>
        </span>
        {named.callsign && named.role !== named.name ? <span className="ag-dock-role">{named.role}</span> : null}
        {named.tagline ? <span className="ag-dock-tagline" data-tip={named.tagline}>{named.tagline}</span> : null}
      </span>
      {actId ? (
        <span className="ag-dock-about" data-testid="ag-dock-about"
              data-tip={about?.label || "The work this conversation is about"}>
          <span className="ag-dock-about-text">About: {about ? shortLabel(about.label) : "this work"}</span>
          {onClearAct ? (
            <button type="button" className="ag-dock-about-x" aria-label="Not about this work"
                    data-tip="Ask without this context" onClick={onClearAct} data-testid="ag-dock-about-clear">
              <X size={10} weight="bold" aria-hidden />
            </button>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}

/** The 24px face beside an answer. */
export function DockGutterFace({ roleKey, state }: { roleKey: string | null; state?: AvatarState }) {
  return <RunFace roleKey={roleKey} variant={roleKey ? "agent" : "boss"} size={24} state={state ?? "idle"} />;
}

function hhmm(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

/** One cited piece of work: `I ran this · 14:02 ↗` (UTC, as the console tells time), opening its Act card. */
function CitedAct({ actId, projectId, viewer, speaker }: {
  actId: string; projectId: string; viewer: string; speaker: string | null;
}) {
  const act = useActDetail(projectId, actId);
  if (!act) {
    return <span className="ag-cite" data-testid="ag-cite" data-loading="true">Cited work ↗</span>;
  }
  const mine = !!speaker && act.roleKey === speaker;
  const who = mine ? "I" : nameOf(act.roleKey, act.roleKey ? "agent" : "person").name;
  const at = hhmm(act.startedAt);
  return (
    <span className="ag-cite" data-testid="ag-cite" data-act-id={act.actId}>
      <ActFace act={act} viewer={viewer} size={20} label={`${who} ran this`}>
        <span className="ag-cite-text">{who} ran this{at ? ` · ${at}` : ""} ↗</span>
      </ActFace>
    </span>
  );
}

/** The work an answer cites, as chips under it. */
export function CitedActs({ ids, projectId, viewer, speaker }: {
  ids: string[]; projectId: string; viewer: string; speaker: string | null;
}) {
  const unique = [...new Set(ids.filter(Boolean))].slice(0, 6);
  if (!unique.length) return null;
  return (
    <span className="ag-cites" data-testid="ag-cites">
      {unique.map((id) => <CitedAct key={id} actId={id} projectId={projectId} viewer={viewer} speaker={speaker} />)}
    </span>
  );
}

/** Cited act ids from an answer or a turn's reply, whatever shape they arrive in. */
export function citedIds(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => typeof x === "string" ? x
    : x && typeof x === "object" ? String((x as Record<string, unknown>).act_id ?? (x as Record<string, unknown>).actId ?? "") : "")
    .filter((x) => x.startsWith("act_"));
}
