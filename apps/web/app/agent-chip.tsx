"use client";

/**
 * An agent, as a face and a name.
 *
 * The assignment lists rendered as bare text with a tiny uppercase source word after each one —
 * "Adversarial Reviewer BY DEFAULT  Chief Architect BY DEFAULT  QA Lead BY DEFAULT" — a run-on
 * line where the label competed with the name and nothing separated one agent from the next.
 * The meeting room already had the right answer and has had it all along: a coloured disc with
 * the agent's initials, the name beside it, one pill per agent.
 *
 * This is that, generalised. The meeting room keys its colours off DISPLAY names ("Product
 * Manager", "UX Designer") because that is what a panel records; the workforce keys off role
 * keys (`product_manager`, `ux_engineer`). Both arrive here, so both are matched — otherwise
 * the same agent would be one colour in a panel and another on a workflow, which is worse than
 * having no colour at all.
 */

import type { ReactNode } from "react";

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
  providerops_manager: { initials: "PO", tone: "muted" },
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
  const byKey = META[(key || "").trim().toLowerCase()];
  if (byKey) return byKey;
  const byName = BY_DISPLAY[(label || key || "").trim().toLowerCase()];
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

export function AgentChip({ roleKey, label, note, children, href }: {
  roleKey: string;
  label?: string;
  /** Where this agent came from — "by default", "chosen". Secondary to the name, never louder. */
  note?: string;
  /** A remove button, or nothing on a read-only list. */
  children?: ReactNode;
  href?: string;
}) {
  const meta = agentMeta(roleKey, label);
  const name = label || roleKey.replace(/_/g, " ");
  return (
    <span className="agent-chip" data-testid={`agent-chip-${roleKey}`}>
      <span className={`mr-avatar tone-${meta.tone}`} aria-hidden>{meta.initials}</span>
      {href
        ? <a className="agent-chip-name" href={href}>{name}</a>
        : <span className="agent-chip-name">{name}</span>}
      {note ? <span className="agent-chip-note">{note}</span> : null}
      {children}
    </span>
  );
}
