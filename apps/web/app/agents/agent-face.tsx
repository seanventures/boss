"use client";

/**
 * A face, a stack of faces, and a state said in a glyph and a word — for the Agents area.
 *
 * The face itself is the kit's (avatarSvg, S-kit): this only places it, sizes it and says what
 * it is to a screen reader. Until the kit lands the face is the old initials disc inside the
 * same state ring, so every surface here renders and is testable on its own; at integration the
 * kit's SVG drops in with no change to any caller. Motion lives in the kit's SVG classes and in
 * agent-world.css; nothing here animates anything a reduced-motion user would see.
 */
// React by name: some suites render with renderToStaticMarkup (classic runtime).
import React from "react";
import {
  Archive, Bell, ChatCircleDots, Check, DotsThree, Gear, Minus, Pause, Warning,
} from "@phosphor-icons/react";

import { agentMeta } from "../agent-chip";
import type { AgentIdentity, AvatarSize, AvatarState, AvatarVariant } from "../../lib/agent-world-types";
import { STATE_WORD, faceMarkup, identityOf } from "../../lib/agent-world";

export function AgFace({ roleKey, identity, size, state = "idle", variant = "agent", hero = false,
                         decorative = false, className }: {
  roleKey: string;
  identity?: AgentIdentity;
  size: AvatarSize;
  state?: AvatarState;
  variant?: AvatarVariant;
  /** The Passport's 96px face: the only place idle breathes and blinks (spec §7). */
  hero?: boolean;
  /** Next to the agent's name: hidden from screen readers, which already hear the name. */
  decorative?: boolean;
  className?: string;
}) {
  const who = identity ?? identityOf(roleKey);
  const markup = faceMarkup(who.avatar, size, state, variant);
  const lifecycle = who.lifecycle !== "active" ? who.lifecycle : undefined;
  const shown: AvatarState = lifecycle ?? state;
  const a11y = decorative
    ? { "aria-hidden": true as const }
    : { role: "img", "aria-label": `${who.name}, ${STATE_WORD[shown]}` };
  const cls = ["ag-face", className].filter(Boolean).join(" ");
  if (markup) {
    return (
      <span className={cls} data-size={size} data-state={shown} data-hero={hero || undefined}
            data-kit="boss-v1" {...a11y} dangerouslySetInnerHTML={{ __html: markup }} />
    );
  }
  const meta = agentMeta(who.roleKey || roleKey, who.name);
  return (
    <span className={cls} data-size={size} data-state={shown} data-hero={hero || undefined} {...a11y}>
      <span className={`ag-face-disc mr-avatar tone-${meta.tone}`}>{size >= 20 ? meta.initials : ""}</span>
    </span>
  );
}

/** Up to `max` faces, overlapped, with `+n` past that. */
export function AgFaceStack({ roleKeys, size = 24, max = 4, states, label, decorative = false }: {
  roleKeys: string[]; size?: AvatarSize; max?: number;
  states?: Map<string, AvatarState>; label?: string;
  /** Inside a control that already says who: hidden from screen readers. */
  decorative?: boolean;
}) {
  const shown = roleKeys.slice(0, max);
  const more = roleKeys.length - shown.length;
  return (
    <span className="ag-stack" data-size={size}
          {...(decorative ? { "aria-hidden": true as const }
            : { role: "img", "aria-label": label ?? roleKeys.map((k) => identityOf(k).name).join(", ") })}>
      {shown.map((k) => (
        <AgFace key={k} roleKey={k} size={size} state={states?.get(k) ?? "idle"} decorative />
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
    <span className="ag-ai" title="An AI agent. Not a real person." data-testid="ag-ai-badge">
      <span className="ag-ai-dot" aria-hidden />{long ? "AI agent" : "AI"}
    </span>
  );
}
