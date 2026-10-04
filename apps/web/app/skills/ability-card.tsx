"use client";

/**
 * <AbilityCard>: one thing the crew can do, with ONE status and ONE button (spec §5.2).
 *
 * The status is computed by the API (services/kit.py) and only ever one of eight; the button is
 * the next action for that status and nothing else. Explanations live in tooltips: the label's
 * tooltip is the ability's one-line description, the pill's says when it was tested or why it
 * fails. Clicking anywhere but the button opens the drawer.
 */
import React from "react";
import { useIdentityVersion } from "../../lib/use-agent-identities";

import { AgentAvatar } from "../agent-avatar";
import { agentIdentity } from "../../lib/agent-identity";
import type { Ability, AbilityStatus } from "../../lib/kit-types";
import { AbilityIcon } from "./ability-icons";
import { BUTTON, PILL, PILL_TONE, agentName } from "./kit-copy";

export const REPO_HREF = "?area=setup&focus=repository";

export type CardAction = "test" | "agents" | "connect" | "repo" | "fix" | "review" | "turn_on";

export const ACTION_OF: Record<AbilityStatus, CardAction | null> = {
  ready: "test", unheld: "agents", setup: "connect", needs_repo: "repo", testing: null,
  failing: "fix", review: "review", off: "turn_on",
};

export function StatusPill({ status, tip }: { status: AbilityStatus; tip?: string }) {
  return (
    <span className="kit-pill" data-tone={PILL_TONE[status]} data-status={status}
          data-tip={tip || undefined} data-testid="kit-pill">
      {status === "testing" ? <span className="kit-spin" aria-hidden="true" /> : null}
      {PILL[status]}
    </span>
  );
}

export function HolderStack({ holders, max = 4, size = 20 }: { holders: string[]; max?: number; size?: number }) {
  useIdentityVersion(); // names repaint when identities load
  if (holders.length === 0) return <span className="kit-noone">No one</span>;
  const shown = holders.slice(0, max);
  const names = holders.map((h) => agentName(h)).join(", ");
  return (
    <span className="kit-stack" data-tip={names} data-testid="kit-holders">
      {shown.map((h) => <AgentAvatar key={h} agent={h} size={size} aiBadge={false} className="kit-stack-face" />)}
      {holders.length > max ? <span className="kit-stack-more">+{holders.length - max}</span> : null}
    </span>
  );
}

/** The card's one button: a link for the repo (it lives in Setup), a button otherwise. */
export function PrimaryAction({ ability, status, onAction, compact }: {
  ability: Ability; status: AbilityStatus; onAction: (a: CardAction) => void; compact?: boolean;
}) {
  const action = ACTION_OF[status];
  const label = BUTTON[status];
  const cls = `kit-btn${status === "setup" || status === "needs_repo" || status === "failing" || status === "review" ? " kit-btn-primary" : ""}${compact ? " kit-btn-sm" : ""}`;
  if (action === "repo") {
    return <a className={cls} href={REPO_HREF} data-testid={`kit-action-${ability.key}`}
              onClick={(e) => e.stopPropagation()}>{label}</a>;
  }
  if (action === "connect" && ability.connect_href) {
    return <a className={cls} href={ability.connect_href} data-testid={`kit-action-${ability.key}`}
              onClick={(e) => e.stopPropagation()}>{label}</a>;
  }
  return (
    <button type="button" className={cls} disabled={action === null}
            data-testid={`kit-action-${ability.key}`}
            onClick={(e) => { e.stopPropagation(); if (action) onAction(action); }}>
      {label}
    </button>
  );
}

export function AbilityCard({ ability, testing, onOpen, onAction }: {
  ability: Ability;
  testing?: boolean;
  onOpen: () => void;
  onAction: (a: CardAction) => void;
}) {
  const status: AbilityStatus = testing ? "testing" : ability.status;
  const line = status === "failing" || status === "review" ? ability.status_line : null;
  return (
    <article className="kit-card" data-status={status} data-testid={`kit-card-${ability.key}`}
             role="button" tabIndex={0} aria-label={ability.label}
             onClick={onOpen}
             onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}>
      <div className="kit-card-row">
        <AbilityIcon name={ability.icon} />
        <h4 className="kit-card-label" data-tip={ability.tip}>{ability.label}</h4>
        <StatusPill status={status} tip={ability.status_tip} />
      </div>
      <div className="kit-card-row kit-card-foot">
        <HolderStack holders={ability.holders} />
        <PrimaryAction ability={ability} status={status} onAction={onAction} />
      </div>
      {line ? (
        <div className="kit-card-line" data-tone={status === "failing" ? "red" : "amber"}
             data-testid={`kit-card-line-${ability.key}`}>
          <span data-tip={ability.status_tip}>{line}</span>
        </div>
      ) : null}
    </article>
  );
}
