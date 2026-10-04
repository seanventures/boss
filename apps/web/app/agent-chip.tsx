"use client";

/**
 * An agent, as a face and a name.
 *
 * The assignment lists rendered as bare text with a tiny uppercase source word after each one —
 * "Adversarial Reviewer BY DEFAULT  Chief Architect BY DEFAULT  QA Lead BY DEFAULT" — a run-on
 * line where the label competed with the name and nothing separated one agent from the next.
 * The meeting room had the right shape: a face, the name beside it, one pill per agent.
 *
 * The face is now the agent's boss-kit-v1 character (AgentAvatar), resolved through the one
 * resolver, so a panel seat title ("QA / Test"), a role key (`qa`) and a callsign ("Vera") are
 * the same face here, on a workflow step, in the inspector and in a meeting. The `mr-avatar
 * tone-` wrapper stays so every existing layout keeps its size and spacing.
 */

import React, { type ReactNode } from "react";

import type { AvatarSize, AvatarState } from "../lib/agent-world-types";
import { withLifecycle } from "../lib/avatar-kit/states";
import { own } from "../lib/avatar-kit/dict";
import { useIdentity } from "../lib/use-agent-identities";
import { AgentAvatar, AgentStateWord } from "./agent-avatar";

/** Initials and a tone per agent. Deliberately the same palette the meeting room uses. */
const META: Record<string, { initials: string; tone: string }> = {
  product_manager: { initials: "PM", tone: "accent" },
  product_owner: { initials: "PO", tone: "accent" },
  vp_product: { initials: "VP", tone: "accent" },
  chief_architect: { initials: "AR", tone: "blue" },
  software_engineer: { initials: "EN", tone: "blue" },
  engineering_captain: { initials: "EC", tone: "blue" },
  ux_engineer: { initials: "UX", tone: "pink" },
  design_critic: { initials: "DC", tone: "pink" },
  security_reviewer: { initials: "SE", tone: "amber" },
  secrets_steward: { initials: "SS", tone: "amber" },
  adversarial_reviewer: { initials: "RV", tone: "amber" },
  qa: { initials: "QA", tone: "green" },
  integration_validator: { initials: "IV", tone: "green" },
  release_manager: { initials: "RM", tone: "green" },
  deployment_manager: { initials: "DM", tone: "green" },
  infra_steward: { initials: "IS", tone: "muted" },
  finops_sentinel: { initials: "FI", tone: "muted" },
  model_spend_analyst: { initials: "MS", tone: "muted" },
  researcher: { initials: "RS", tone: "accent" },
  metatron: { initials: "MT", tone: "blue" },
  cto: { initials: "CT", tone: "blue" },
  coo: { initials: "CO", tone: "muted" },
  cao: { initials: "CA", tone: "muted" },
  pm: { initials: "PM", tone: "accent" },
  program_manager: { initials: "PG", tone: "accent" },
  vp_agents: { initials: "VA", tone: "muted" },
  providerops_manager: { initials: "POM", tone: "muted" },
  resource_janitor: { initials: "RJ", tone: "muted" },
};

/** The panel's own seat names, which are display strings rather than role keys. */
const BY_DISPLAY: Record<string, { initials: string; tone: string }> = {
  "product manager": { initials: "PM", tone: "accent" },
  "engineer / architect": { initials: "EN", tone: "blue" },
  "ux designer": { initials: "UX", tone: "pink" },
  "privacy & safety": { initials: "PS", tone: "amber" },
  "facilitator / chief of staff": { initials: "CS", tone: "green" },
  operations: { initials: "OP", tone: "muted" },
};

/** Two letters that mean something, and a colour that is stable for this agent. */
export function agentMeta(key: string, label?: string) {
  const byKey = own(META, (key || "").trim().toLowerCase());
  if (byKey) return byKey;
  const byName = own(BY_DISPLAY, (label || key || "").trim().toLowerCase());
  if (byName) return byName;
  // A word-initial pair beats the first two letters: "Release Manager" is RM, not RE.
  const words = (label || key || "?").replace(/[_-]+/g, " ").trim().split(/\s+/);
  const initials = words.length > 1
    ? (words[0][0] + words[1][0]).toUpperCase()
    : (words[0] ?? "?").slice(0, 2).toUpperCase();
  // Stable, not random: the same agent must not change colour between screens.
  const tones = ["accent", "blue", "pink", "amber", "green", "muted"];
  let sum = 0;
  for (const ch of key || label || "") sum = (sum + ch.charCodeAt(0)) % 997;
  return { initials, tone: tones[sum % tones.length] };
}

export function AgentChip({ roleKey, label, note, children, href, size = 24, state }: {
  roleKey: string;
  label?: string;
  /** Where this agent came from — "by default", "chosen". Secondary to the name, never louder. */
  note?: string;
  /** A remove button, or nothing on a read-only list. */
  children?: ReactNode;
  href?: string;
  /** Face size in px; 24 in a chip. */
  size?: AvatarSize;
  /** What the agent is doing, from a real status (lib/avatar-kit/states.ts). Shown as face + word. */
  state?: AvatarState;
}) {
  const meta = agentMeta(roleKey, label);
  const identity = useIdentity(roleKey || label || ""); // repaints when identities load
  const name = label || identity.name;
  const callsign = identity.callsign && identity.callsign !== name ? ` · ${identity.callsign}` : "";
  // One state for the face, its word and the chip: a paused or retired agent is paused or
  // retired here whatever it was last doing (§2.5 truthful state).
  const shown = withLifecycle(state ?? "idle", identity.lifecycle);
  const sayState = Boolean(state) || shown !== "idle";
  return (
    <span className="agent-chip" data-testid={`agent-chip-${roleKey}`} data-state={sayState ? shown : undefined}>
      <AgentAvatar identity={identity} size={size} state={shown} aiBadge={false}
                   className={`mr-avatar tone-${meta.tone} ag-face`} tip={`${name}${callsign} · AI agent`} />
      {href
        ? <a className="agent-chip-name" href={href}>{name}</a>
        : <span className="agent-chip-name">{name}</span>}
      {/* The tooltip and badge are not read aloud; this is how a screen reader hears it is an AI. */}
      <span className="ag-sr">, AI agent</span>
      {sayState ? <AgentStateWord state={shown} /> : null}
      {note ? <span className="agent-chip-note">{note}</span> : null}
      {children}
    </span>
  );
}
