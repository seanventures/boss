"use client";

/**
 * A face, a stack of faces, and a state said in a glyph and a word — for the Agents area.
 *
 * ONE FACE EVERYWHERE (spec §2.1, test 11). `AgFace` is the kit's <AgentAvatar>, under the name
 * the Agents area already calls it by: it resolves through the same identity store and draws the
 * same SVG as a chip, a step node or the dock, so the Roster, the Passport, the Swarm and the
 * Lineage can never show a different face for the same agent. Nothing is drawn here.
 */
// React by name: some suites render with renderToStaticMarkup (classic runtime).
import React from "react";
import {
  Archive, Bell, ChatCircleDots, Check, DotsThree, Gear, Minus, Pause, Warning,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import type { AgentIdentity, AvatarSize, AvatarSpec, AvatarState, AvatarVariant } from "../../lib/agent-world-types";
import { STATE_WORD, identityOf } from "../../lib/agent-world";
import { useIdentityVersion } from "../../lib/use-agent-identities";

export function AgFace({ roleKey, identity, spec, size, state = "idle", variant = "agent", decorative = false,
                         aiBadge, motion, className }: {
  roleKey: string;
  identity?: AgentIdentity;
  /** An unsaved face to preview (the Face picker, the birth). */
  spec?: AvatarSpec;
  /** A kit size, or any pixel size for a hero face (the kit picks its detail level). */
  size: AvatarSize | number;
  state?: AvatarState;
  variant?: AvatarVariant;
  /** Next to the agent's name: hidden from screen readers, which already hear the name. */
  decorative?: boolean;
  /** The kit's `AI` badge on the face (on from 32px). Off where `AI agent` is said beside it. */
  aiBadge?: boolean;
  motion?: "birth" | "wave";
  className?: string;
}) {
  return (
    <AgentAvatar agent={roleKey} identity={identity} spec={spec} variant={variant} size={size} state={state}
                 standalone={!decorative} aiBadge={aiBadge} motion={motion}
                 className={["ag-face", className].filter(Boolean).join(" ")} />
  );
}

/** Up to `max` faces, overlapped, with `+n` past that. */
export function AgFaceStack({ roleKeys, size = 24, max = 4, states, label, decorative = false }: {
  roleKeys: string[]; size?: AvatarSize; max?: number;
  states?: Map<string, AvatarState>; label?: string;
  /** Inside a control that already says who: hidden from screen readers. */
  decorative?: boolean;
}) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const shown = roleKeys.slice(0, max);
  const more = roleKeys.length - shown.length;
  return (
    <span className="ag-stack" data-size={size}
          {...(decorative ? { "aria-hidden": true as const }
            : { role: "img", "aria-label": label ?? roleKeys.map((k) => identityOf(k).name).join(", ") })}>
      {shown.map((k, i) => (
        <AgFace key={`${k}:${i}`} roleKey={k} size={size} state={states?.get(k) ?? "idle"} decorative />
      ))}
      {more > 0 ? <span className="ag-stack-more" aria-hidden>+{more}</span> : null}
    </span>
  );
}

const GLYPH: Record<AvatarState, React.ComponentType<{ size?: number; weight?: "bold" | "fill" | "regular"; "aria-hidden"?: boolean }>> = {
  idle: Minus, thinking: DotsThree, speaking: ChatCircleDots, working: Gear, needs_you: Bell,
  done: Check, stuck: Warning, paused: Pause, retired: Archive,
};

/** A state is never colour alone: a glyph and a word, always (spec §7 rules). */
export function AgState({ state, compact = false }: { state: AvatarState; compact?: boolean }) {
  const Icon = GLYPH[state];
  return (
    <span className="ag-state" data-state={state} data-compact={compact || undefined}>
      <Icon size={12} weight="bold" aria-hidden />
      <span>{STATE_WORD[state]}</span>
    </span>
  );
}

/** The `AI` micro-badge (spec §3.1): a dot and the letters, with the disclosure as tooltip. */
export function AiBadge({ long = false }: { long?: boolean }) {
  return (
    <span className="ag-ai-tag" title="An AI agent. Not a real person." data-testid="ag-ai-badge">
      <span className="ag-ai-dot" aria-hidden />{long ? "AI agent" : "AI"}
    </span>
  );
}
