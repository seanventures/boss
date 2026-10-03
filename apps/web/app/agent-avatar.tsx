"use client";

/**
 * <AgentAvatar>: the one face (spec §3.1).
 *
 * Give it any name BOSS has for an agent — role key, seat title, callsign — or an identity, or
 * an unsaved spec to preview, or a system variant (Auto, You, A person, BOSS). It resolves
 * through lib/agent-identity.ts and draws through lib/agent-avatar.ts, the same pure function
 * the share card and the README cast use, so a face is identical on every surface.
 *
 * Next to a name the face is decorative (aria-hidden, the name says who; AgentChip adds a
 * visually hidden "AI agent"). Standalone it is `role="img"` with "<name>, AI agent, <state>",
 * so a screen reader always hears that an agent is an AI. The AI badge shows from 32px; below
 * that the caller's tooltip carries "AI agent" (AgentChip does).
 */
import React, { useRef } from "react";

import { avatarSvg } from "../lib/agent-avatar";
import { agentIdentity, useIdentityVersion } from "../lib/agent-identity";
import type { AgentIdentity, AvatarSpec, AvatarState, AvatarVariant } from "../lib/agent-world-types";
import { LIVE_STATES, STATE_WORDS, withLifecycle } from "../lib/avatar-kit/states";
import { useAvatarMotion } from "./agents/use-avatar-motion";

export const SYSTEM_NAMES: Record<Exclude<AvatarVariant, "agent">, string> = {
  auto: "Auto", you: "You", person: "A person", boss: "BOSS",
};

export type FaceInput = {
  /** A role key, seat title, alias or callsign. */
  agent?: string;
  identity?: AgentIdentity;
  /** An unsaved face (the Profile › Face preview, the birth). Wins over the identity's own. */
  spec?: AvatarSpec;
  variant?: AvatarVariant;
};

/** What a face input draws: its identity (for agents), its name, the state after lifecycle, and the SVG. */
export function resolveFace(input: FaceInput, size: number, state: AvatarState = "idle") {
  const variant = input.variant ?? "agent";
  if (variant !== "agent") {
    return { identity: null, name: SYSTEM_NAMES[variant], state, svg: avatarSvg({ kit: "boss-v1", seed: variant }, { size, state, variant }) };
  }
  const identity = input.identity ?? agentIdentity(input.agent ?? "");
  const shown = withLifecycle(state, identity.lifecycle);
  return { identity, name: identity.name, state: shown, svg: avatarSvg(input.spec ?? identity.avatar, { size, state: shown }) };
}

export function AgentAvatar({ size = 24, state = "idle", motion, standalone = false, aiBadge, className, tip, onClick, ...input }: FaceInput & {
  size?: number;
  state?: AvatarState;
  /** One-shot motion: the birth assembly, or the five-click wave. */
  motion?: "birth" | "wave";
  /** No name beside it: give it role="img" and a label instead of hiding it. */
  standalone?: boolean;
  /** Defaults on for agents at 32px and up. */
  aiBadge?: boolean;
  className?: string;
  tip?: string;
  onClick?: () => void;
}) {
  useIdentityVersion(); // a loaded callsign or custom face repaints every face at once
  const ref = useRef<HTMLSpanElement>(null);
  const face = resolveFace(input, size, state);
  const live = LIVE_STATES.has(face.state) || (face.state === "idle" && size >= 96) || Boolean(motion);
  useAvatarMotion(ref, live);
  const isAgent = (input.variant ?? "agent") === "agent";
  const label = `${face.name}${isAgent ? ", AI agent" : ""}, ${STATE_WORDS[face.state]}`;
  const showAi = (aiBadge ?? true) && isAgent && size >= 32;
  return (
    <span ref={ref} className={`ag-avatar${className ? ` ${className}` : ""}`}
          style={{ ["--ag-size" as string]: `${size}px` }}
          data-state={face.state} data-motion={motion} data-role-key={face.identity?.roleKey}
          {...(standalone ? { role: "img", "aria-label": label } : { "aria-hidden": true })}
          {...(tip ? { "data-tip": tip } : {})}
          {...(onClick ? { onClick } : {})}>
      <span className="ag-avatar-svg" dangerouslySetInnerHTML={{ __html: face.svg }} />
      {showAi ? <span className="ag-ai" aria-hidden data-tip="An AI agent. Not a real person.">AI</span> : null}
    </span>
  );
}

/** The state as a word, beside a face: state is never the picture alone. */
export function AgentStateWord({ state }: { state: AvatarState }) {
  if (state === "idle") return null;
  return <span className="ag-state-word" data-state={state}>{STATE_WORDS[state]}</span>;
}
