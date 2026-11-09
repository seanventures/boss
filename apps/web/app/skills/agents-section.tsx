"use client";

/**
 * Agents (spec §5.3 3): who holds this ability, as faces, with a toggle each. A toggle writes ALL
 * of the ability's skill and MCP grants for that agent in one call (PUT holders, the whole new
 * set). Agents whose role recommends it carry a star. An ability that is not set up cannot be
 * given, only taken away.
 */
import { GatedButton } from "../../components/gated";
import React, { useState } from "react";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { DotsThree, Star } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { kitApi, type CrewMember } from "../../lib/kit-api";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { plural, shortError, agentName } from "./kit-copy";
import { ToolsNarrow } from "./tools-narrow";

export function blocked(ability: Ability): boolean {
  return ability.status === "setup" || ability.status === "needs_repo";
}

export function AgentsSection({ ability, crew, projectId, actor, onChanged }: {
  ability: Ability; crew: CrewMember[]; projectId: string; actor: string; onChanged: (a: Ability) => void;
}) {
  useIdentityVersion(); // names repaint when identities load
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [menu, setMenu] = useState<string | null>(null);
  const [narrow, setNarrow] = useState<string | null>(null);
  const held = new Set(ability.holders);
  const recommended = new Set(ability.recommended);
  const cantGive = blocked(ability);

  const write = async (roles: string[], toast: string) => {
    setBusy(true);
    const r = await kitApi.holders(projectId, actor, ability.key, roles);
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", toast);
    onChanged(r.data);
  };
  const toggle = (role: string) => {
    const name = agentName(role);
    if (held.has(role)) void write([...held].filter((r) => r !== role), `Took ${ability.label} from ${name}`);
    else void write([...held, role], `Gave ${ability.label} to ${name}`);
  };
  const ordered = [...crew].sort((a, b) =>
    Number(recommended.has(b.role_key)) - Number(recommended.has(a.role_key))
    || agentName(a.role_key).localeCompare(agentName(b.role_key)));
  return (
    <div className="kit-agents" data-testid="kit-agents">
      <div className="kit-agents-bar">
        <GatedButton permission="agent_ability:assign" type="button" className="kit-btn kit-btn-sm" disabled={busy || cantGive}
                data-testid="kit-agents-recommended"
                onClick={() => void write([...recommended], `Gave ${ability.label} to ${plural(recommended.size, "agent")}`)}>
          Recommended only
        </GatedButton>
        <GatedButton permission="agent_ability:assign" type="button" className="kit-btn kit-btn-sm" disabled={busy || cantGive} data-testid="kit-agents-everyone"
                onClick={() => void write(crew.map((c) => c.role_key), `Gave ${ability.label} to ${plural(crew.length, "agent")}`)}>
          Everyone
        </GatedButton>
        <span className="kit-confirm-wrap">
          <GatedButton permission="agent_ability:assign" type="button" className="kit-btn kit-btn-sm" disabled={busy || held.size === 0}
                  data-testid="kit-agents-clear" onClick={() => setConfirm(true)}>Clear</GatedButton>
          {confirm ? (
            <span className="kit-pop" role="dialog" aria-label="Confirm clear" data-testid="kit-agents-clear-confirm">
              <strong>Take {ability.label} from {plural(held.size, "agent")}?</strong>
              <span className="kit-pop-actions">
                <GatedButton permission="agent_ability:assign" type="button" className="kit-btn kit-btn-danger kit-btn-sm" data-testid="kit-agents-clear-yes"
                        onClick={() => { setConfirm(false); void write([], `Took ${ability.label} from ${plural(held.size, "agent")}`); }}>Clear</GatedButton>
                <button type="button" className="kit-btn kit-btn-sm" onClick={() => setConfirm(false)}>Cancel</button>
              </span>
            </span>
          ) : null}
        </span>
      </div>
      <ul className="kit-agent-grid">
        {ordered.map((c) => {
          const on = held.has(c.role_key);
          const name = agentName(c.role_key);
          const disabled = busy || (!on && cantGive);
          return (
            <li key={c.role_key} className="kit-agent" data-on={on} data-testid={`kit-agent-${c.role_key}`}>
              <AgentAvatar agent={c.role_key} size={24} aiBadge={false} />
              <span className="kit-agent-name" data-tip={`${name}, ${c.display_name ?? c.role_key}, AI agent`}>
                {name}
              </span>
              {recommended.has(c.role_key) ? (
                <span className="kit-star" data-tip={`In ${c.display_name ?? name}'s default loadout`}
                      data-testid={`kit-agent-rec-${c.role_key}`}>
                  <Star size={12} weight="fill" aria-label="Recommended" />
                </span>
              ) : null}
              <GatedButton permission="agent_ability:assign" type="button" role="switch" aria-checked={on} aria-label={`${name} holds ${ability.label}`}
                      className="kit-switch" disabled={disabled}
                      data-tip={!on && cantGive ? `Set up ${ability.label} first` : undefined}
                      data-testid={`kit-agent-toggle-${c.role_key}`} onClick={() => toggle(c.role_key)} />
              <span className="kit-confirm-wrap">
                <button type="button" className="kit-icon-btn" aria-label="More" onClick={() => setMenu(menu === c.role_key ? null : c.role_key)}>
                  <DotsThree size={14} />
                </button>
                {menu === c.role_key ? (
                  <span className="kit-pop kit-menu" role="menu">
                    <a role="menuitem" href={`?area=workforce&role=${encodeURIComponent(c.role_key)}`}>Open agent</a>
                    {on && ability.mcp.length ? (
                      <GatedButton permission="agent_ability:assign" type="button" role="menuitem" onClick={() => { setMenu(null); setNarrow(c.role_key); }}>Tools ▾</GatedButton>
                    ) : null}
                  </span>
                ) : null}
                {narrow === c.role_key ? (
                  <ToolsNarrow ability={ability} role={c.role_key} projectId={projectId} actor={actor}
                               onClose={() => setNarrow(null)} />
                ) : null}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
