"use client";

/**
 * An agent's face on the run surfaces, with the state it is really in.
 *
 * ONE FACE, ONE RESOLVER. The face is the avatar kit's (`avatarSvg` over `agentIdentity`, both
 * S-kit), so a step node, a chain hop, the Act card and the dock draw the same character for the
 * same role. Until the kit is mounted those throw; the face then falls back to the console's
 * existing initials disc (`agentMeta`), which is what every chip draws today, so nothing goes
 * blank in between. The state ring, badge word and accessible name are drawn here either way.
 *
 * Integration: when app/agent-avatar.tsx lands, `KitFace` can become `<AgentAvatar>` with the
 * same props; the wrapper (ring, word, aria) stays.
 */
import { Crown, HandPalm, PushPin, Sparkle, User, ArrowRight, Armchair, Robot } from "@phosphor-icons/react";
import React from "react";

import {
  agentIdentity, avatarSvg,
  type ActHow, type AvatarSize, type AvatarState, type AvatarVariant,
} from "../../lib/agent-world-types";
import { agentMeta } from "../agent-chip";
import { roleWords } from "./chain-model";

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
export function nameOf(roleKey: string | null, variant: AvatarVariant = "agent", personId?: string): FaceName {
  if (!roleKey || variant !== "agent") {
    if (variant === "you") return { name: "You", role: "You" };
    if (variant === "boss") return { name: "BOSS", role: "BOSS" };
    if (variant === "auto") return { name: "Auto", role: "Auto" };
    return { name: "A person", role: personId || "A person" };
  }
  try {
    const id = agentIdentity(roleKey);
    return { name: id.callsign || id.name, role: id.name, callsign: id.callsign, tagline: id.tagline };
  } catch {
    const role = roleWords(roleKey);
    return { name: role, role };
  }
}

/** The kit's SVG for this face, or null while the kit is not mounted. */
function kitSvg(roleKey: string, size: number, state: AvatarState, variant: AvatarVariant): string | null {
  try {
    return avatarSvg(agentIdentity(roleKey).avatar, { size, state, variant });
  } catch {
    return null;
  }
}

const GLYPH: Partial<Record<AvatarVariant, React.ComponentType<{ size?: number; weight?: "bold" }>>> = {
  auto: Sparkle, you: User, person: HandPalm, boss: Robot,
};

export function RunFace({ roleKey, variant = "agent", size = 24, state = "idle", label, personId, standalone, aiMark }: {
  roleKey: string | null; variant?: AvatarVariant; size?: AvatarSize; state?: AvatarState;
  /** The accessible name when the face stands alone; defaults to the agent's name. */
  label?: string; personId?: string;
  /** A face with no name beside it is an image with a name; next to a name it is decoration. */
  standalone?: boolean;
  /** The `AI` micro-mark on the face itself (≥32px), for a face shown without the AI badge. */
  aiMark?: boolean;
}) {
  const v: AvatarVariant = roleKey ? variant : variant === "agent" ? "person" : variant;
  const named = nameOf(roleKey, v, personId);
  const svg = roleKey && v === "agent" ? kitSvg(roleKey, size, state, v) : null;
  const Glyph = GLYPH[v];
  const meta = roleKey ? agentMeta(roleKey, named.role) : null;
  const a11y = standalone
    ? { role: "img", "aria-label": `${label ?? named.name}, ${STATE_WORD[state]}` }
    : { "aria-hidden": true as const };
  return (
    <span className="ag-face" data-size={size} data-state={state} data-variant={v}
          data-role-key={roleKey ?? undefined} style={{ ["--ag-size" as string]: `${size}px` }} {...a11y}>
      <span className="ag-face-ring" aria-hidden />
      {svg ? (
        <span className="ag-face-kit" aria-hidden dangerouslySetInnerHTML={{ __html: svg }} />
      ) : Glyph ? (
        <span className="ag-face-glyph" aria-hidden><Glyph size={Math.max(9, Math.round(size * 0.5))} weight="bold" /></span>
      ) : (
        <span className={`mr-avatar tone-${meta?.tone ?? "muted"} ag-face-disc`} aria-hidden>{meta?.initials ?? "?"}</span>
      )}
      {aiMark && size >= 32 && v === "agent" ? <span className="ag-ai" aria-hidden>AI</span> : null}
    </span>
  );
}

/** The `AI` mark beside a name: an agent is never mistaken for a person. */
export function AiBadge() {
  return <span className="ag-ai-badge" data-tip="An AI agent. Not a real person." aria-label="AI agent">AI</span>;
}

/** The state as a word with its dot, for places a face alone would be colour only. */
export function StateWord({ state }: { state: AvatarState }) {
  return <span className="ag-state" data-state={state}><i aria-hidden />{STATE_WORD[state]}</span>;
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
