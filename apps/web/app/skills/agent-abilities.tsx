"use client";

/**
 * The Passport's abilities (skills-mcp spec §5.1, §5.7): what this agent can do, as the Kit's
 * ability chips, with the same grant write the Kit uses (PUT holders — the whole new set for
 * that ability). A filled chip is held; an outlined one with a star is recommended for its role
 * and not held. `All abilities ↗` opens Who can on this agent's row.
 */
import React, { useCallback, useEffect, useState } from "react";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { Star } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { kitApi } from "../../lib/kit-api";
import { agentIdentity } from "../../lib/agent-identity";
import { showToast } from "../client-forms";
import { AbilityIcon } from "./ability-icons";
import { blocked } from "./agents-section";
import { shortError, TIP, agentName } from "./kit-copy";

export function AgentAbilities({ projectId, roleKey, actor, onChanged }: {
  projectId: string; roleKey: string; actor: string; onChanged?: () => void;
}) {
  useIdentityVersion(); // names repaint when identities load
  const [abilities, setAbilities] = useState<Ability[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await kitApi.load(projectId, "none");
    setAbilities(r.ok ? r.data.abilities : []);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const toggle = async (a: Ability) => {
    const holds = a.holders.includes(roleKey);
    if (!holds && blocked(a)) return;
    setBusy(a.key);
    const r = await kitApi.holders(projectId, actor, a.key, holds ? a.holders.filter((x) => x !== roleKey) : [...a.holders, roleKey]);
    setBusy(null);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    const name = agentName(roleKey);
    showToast("success", holds ? `Took ${a.label} from ${name}` : `Gave ${a.label} to ${name}`);
    setAbilities((list) => (list ?? []).map((x) => (x.key === a.key ? r.data : x)));
    onChanged?.();
  };

  const shown = (abilities ?? []).filter((a) => a.holders.includes(roleKey) || a.recommended.includes(roleKey));
  return (
    <div className="kit-agent-abilities" data-testid="agent-abilities">
      <div className="kit-row">
        <span className="kit-section-label">Abilities</span>
        <a className="kit-link" href={`?area=skills&tab=who&role=${encodeURIComponent(roleKey)}`}
           data-tip={TIP.allAbilities} data-testid="agent-all-abilities">All abilities ↗</a>
      </div>
      <div className="kit-filters">
        {shown.map((a) => {
          const holds = a.holders.includes(roleKey);
          return (
            <button key={a.key} type="button" className="kit-chip kit-chip-btn" aria-pressed={holds}
                    disabled={busy === a.key || (!holds && blocked(a))}
                    data-tip={!holds && blocked(a) ? `Set up ${a.label} first` : a.tip}
                    data-testid={`agent-ability-${a.key}`} onClick={() => void toggle(a)}>
              <AbilityIcon name={a.icon} size={14} />
              {a.label}
              {!holds ? <Star size={10} weight="fill" aria-label="Recommended" /> : null}
            </button>
          );
        })}
        {abilities !== null && shown.length === 0 ? <span className="kit-faint">No abilities yet</span> : null}
      </div>
    </div>
  );
}
