"use client";

/**
 * An agent's face on the run surfaces, with the state it is really in.
 *
 * ONE FACE, ONE RESOLVER. The face IS the kit's <AgentAvatar> (app/agent-avatar.tsx): the same
 * resolver, SVG, ring, badge and motion as a chip, the Roster, the Passport and the room, so a
 * step node, a chain hop, the Act card and the dock draw the same character for the same role.
 * This file only names it the way the run surfaces need (nameOf) and draws how work arrived.
 */
import { Crown, PushPin, Sparkle, User, ArrowRight, Armchair, Robot } from "@phosphor-icons/react";
import React from "react";

import {
  agentIdentity,
  type ActHow, type AvatarSize, type AvatarState, type AvatarVariant,
} from "../../lib/agent-world-types";
import { AgentAvatar } from "../agent-avatar";
import { AgState } from "./agent-face";
import { roleWords } from "./chain-model";

export { AiBadge } from "./agent-face";

/** The word for every state: a state is never colour alone (spec §7). */
export const STATE_WORD: Record<AvatarState, string> = {
  idle: "Idle", thinking: "Thinking", speaking: "Speaking", working: "Working", needs_you: "Needs you",
  done: "Done", stuck: "Stuck", paused: "Paused", retired: "Retired",
};

export { roleWords };

export type FaceName = { name: string; role: string; callsign?: string; tagline?: string };

/**
 * How an agent is named on these surfaces: its callsign beside its role when it has one
 * ("Vera" · "QA Lead"), its role otherwise. Callsigns are shown next to the role, never instead.
 */
export function nameOf(roleKey: string | null, variant: AvatarVariant = "agent", personId?: string,
                       personName?: string): FaceName {
  if (!roleKey || variant !== "agent") {
    if (variant === "you") return { name: "You", role: "You" };
    if (variant === "boss") return { name: "BOSS", role: "BOSS" };
    if (variant === "auto") return { name: "Auto", role: "Auto" };
    // A person by name when BOSS knows it; "A person" only when nothing names them.
    return { name: personName || "A person", role: personName || personId || "A person" };
  }
  try {
    const id = agentIdentity(roleKey);
    return { name: id.callsign || id.name, role: id.name, callsign: id.callsign, tagline: id.tagline };
  } catch {
    const role = roleWords(roleKey);
    return { name: role, role };
  }
}

export function RunFace({ roleKey, variant = "agent", size = 24, state = "idle", personId, standalone, aiMark }: {
  roleKey: string | null; variant?: AvatarVariant; size?: AvatarSize; state?: AvatarState;
  /** Who "A person" is, as the face's tooltip. */
  personId?: string;
  /** A face with no name beside it is an image with a name; next to a name it is decoration. */
  standalone?: boolean;
  /** The `AI` badge on the face itself (≥32px), for a face shown without the AI badge beside it. */
  aiMark?: boolean;
}) {
  const v: AvatarVariant = roleKey ? variant : variant === "agent" ? "person" : variant;
  return (
    <AgentAvatar agent={v === "agent" ? roleKey ?? undefined : undefined} variant={v} size={size}
                 state={state} standalone={Boolean(standalone)} aiBadge={Boolean(aiMark)}
                 className="ag-face" tip={v === "person" && personId ? personId : undefined} />
  );
}

/** The state as a glyph and a word, for places a face alone would be colour only. */
export function StateWord({ state }: { state: AvatarState }) {
  return <AgState state={state} compact />;
}

/** How the work reached this agent, as a glyph. The words are the server's edge label. */
const HOW_GLYPH: Partial<Record<string, React.ComponentType<{ size?: number; weight?: "bold" | "fill" }>>> = {
  named: PushPin, model_pick: Sparkle, only_holder: Sparkle, only_candidate: Sparkle, captain: Crown,
  run_actor: User, started: User, approved: User, asked_by_person: User, next_stage: ArrowRight,
  seated: Armchair, asked_by_agent: Robot,
};
/** Where a how has no server label yet (ran_as before the act arrives), the glyph's own name. */
const HOW_NAME: Record<string, string> = {
  named: "Named", model_pick: "BOSS picked", only_holder: "BOSS picked", only_candidate: "BOSS picked",
  captain: "Captain", run_actor: "You", started: "You", approved: "You", asked_by_person: "You",
  next_stage: "Stage", seated: "Panel", asked_by_agent: "Agent",
};

export function HowGlyph({ how, edgeLabel, size = 10 }: { how: ActHow | string; edgeLabel?: string; size?: number }) {
  const Icon = HOW_GLYPH[how];
  if (!Icon) return null;
  const said = edgeLabel || HOW_NAME[how] || "";
  return (
    <span className="ag-how" data-how={how} data-tip={said || undefined} aria-label={said || undefined}
          role="img" data-testid="ag-how">
      <Icon size={size} weight="bold" />
    </span>
  );
}
