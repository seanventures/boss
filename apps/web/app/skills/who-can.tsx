"use client";

/**
 * Who can (spec §5.4): the crew × abilities, for grants in bulk and for seeing the whole crew.
 *
 * A filled dot holds; a ring with a star is recommended and not held; empty is neither. A click
 * writes that ability's whole new holder set (PUT holders), shift-click does the row. An ability
 * that is not set up cannot be given (its cells are disabled, with why), only taken away. Every
 * change is counted and the last one can be undone. At phone width the matrix becomes a list.
 */
import React, { useMemo, useState } from "react";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { Sparkle, Star } from "@phosphor-icons/react";

import type { Ability } from "../../lib/kit-types";
import { kitApi, type CrewMember, type KitWithCrew } from "../../lib/kit-api";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { AbilityIcon } from "./ability-icons";
import { blocked } from "./agents-section";
import { GROUP_LABEL, shortError, TIP, agentName } from "./kit-copy";

const CREW_GROUP: Record<string, number> = {
  engineering_captain: 0, chief_architect: 0, backend_engineer: 0, frontend_engineer: 0,
  software_engineer: 0, docs_writer: 0,
  qa: 1, security_reviewer: 1, adversarial_reviewer: 1, design_critic: 1,
  pm: 2, program_manager: 2,
  sre: 3, operations_engineer: 3, release_manager: 3, finops_sentinel: 3,
};
const GROUP_NAMES = ["Build", "Review", "Product", "Ops", "Others"];

type Change = { ability: string; before: string[] };

export function WhoCan({ kit, crew, projectId, actor, focusRole, onChanged, onReload, onOpenAbility }: {
  kit: KitWithCrew; crew: CrewMember[]; projectId: string; actor: string; focusRole: string | null;
  onChanged: (a: Ability) => void; onReload: () => void; onOpenAbility: (key: string) => void;
}) {
  const identities = useIdentityVersion(); // names repaint when identities load
  const [show, setShow] = useState<"all" | "gaps">("all");
  const [changes, setChanges] = useState<Change[]>([]);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(focusRole);

  const rows = useMemo(() => [...crew].sort((a, b) =>
    (CREW_GROUP[a.role_key] ?? 4) - (CREW_GROUP[b.role_key] ?? 4)
    || agentName(a.role_key).localeCompare(agentName(b.role_key))), [crew, identities]);
  const abilities = useMemo(() => kit.groups.flatMap((g) => kit.abilities.filter((a) => a.group === g)), [kit]);
  const gap = (a: Ability, role: string) => a.recommended.includes(role) && !a.holders.includes(role);
  const visibleRows = show === "gaps" ? rows.filter((r) => abilities.some((a) => gap(a, r.role_key))) : rows;

  const setHolders = async (ability: Ability, roles: string[], toast: string, record = true) => {
    setBusy(true);
    const r = await kitApi.holders(projectId, actor, ability.key, roles);
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return false; }
    if (record) setChanges((c) => [...c, { ability: ability.key, before: ability.holders }]);
    showToast("success", toast);
    onChanged(r.data);
    return true;
  };

  const toggle = (ability: Ability, role: string) => {
    const name = agentName(role);
    const holds = ability.holders.includes(role);
    if (!holds && blocked(ability)) return;
    void setHolders(ability, holds ? ability.holders.filter((r) => r !== role) : [...ability.holders, role],
                    holds ? `Took ${ability.label} from ${name}` : `Gave ${ability.label} to ${name}`);
  };

  const toggleRow = async (role: string) => {
    const grantable = abilities.filter((a) => !blocked(a));
    const giving = grantable.some((a) => !a.holders.includes(role));
    for (const a of giving ? grantable : abilities) {
      const holds = a.holders.includes(role);
      if (giving && !holds) await setHolders(a, [...a.holders, role], `Gave ${a.label} to ${agentName(role)}`);
      if (!giving && holds) await setHolders(a, a.holders.filter((r) => r !== role), `Took ${a.label} from ${agentName(role)}`);
    }
  };

  const undo = async () => {
    const last = changes[changes.length - 1];
    if (!last) return;
    const ability = kit.abilities.find((a) => a.key === last.ability);
    if (!ability) return;
    if (await setHolders(ability, last.before, "Saved", false)) setChanges((c) => c.slice(0, -1));
  };

  const applyRecommended = async () => {
    const r = await kitApi.recommended(projectId, actor);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Applied recommended · ${r.data.granted.length} grants`);
    onReload();
  };

  const cell = (a: Ability, role: string) => {
    const holds = a.holders.includes(role);
    const rec = a.recommended.includes(role);
    const off = blocked(a);
    const tip = off ? (holds ? "Held · not set up" : `Set up ${a.label} first`)
      : `${agentName(role)} · ${a.label}${rec ? " · Recommended" : ""}`;
    return (
      <button type="button" className="kit-cell" aria-pressed={holds} disabled={busy || (off && !holds)}
              data-held={holds || undefined} data-rec={(rec && !holds) || undefined} data-off={off || undefined}
              data-tip={tip} aria-label={tip} data-testid={`kit-cell-${role}-${a.key}`}
              onClick={(e) => { if (e.shiftKey) void toggleRow(role); else toggle(a, role); }}>
        {holds ? <span className="kit-cell-dot" /> : rec ? <span className="kit-cell-ring"><Star size={8} weight="fill" /></span> : null}
      </button>
    );
  };

  return (
    <div className="kit-who" data-testid="kit-who">
      <div className="kit-head">
        <button type="button" className="kit-btn" data-tip={TIP.applyRecommended} data-testid="kit-who-apply"
                disabled={busy} onClick={() => void applyRecommended()}><Sparkle size={14} weight="fill" /> Apply recommended</button>
        <span className="kit-field-label">Show</span>
        <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Show">
          <button type="button" role="radio" aria-checked={show === "all"} data-testid="kit-who-all" onClick={() => setShow("all")}>All</button>
          <button type="button" role="radio" aria-checked={show === "gaps"} data-tip={TIP.gaps} data-testid="kit-who-gaps" onClick={() => setShow("gaps")}>Gaps</button>
        </span>
        <span className="kit-spacer" />
        {changes.length ? (
          <>
            <span className="kit-faint" data-testid="kit-who-changes">{changes.length} changes</span>
            <button type="button" className="kit-link" disabled={busy} data-testid="kit-who-undo" onClick={() => void undo()}>Undo last</button>
          </>
        ) : null}
      </div>

      <div className="kit-matrix-wrap">
        <table className="kit-matrix" data-testid="kit-matrix">
          <thead>
            <tr>
              <th aria-label="Agent" />
              {abilities.map((a) => (
                <th key={a.key} scope="col">
                  <button type="button" className="kit-col" data-tip={`${GROUP_LABEL[a.group] ?? a.group} · ${a.tip}`}
                          data-testid={`kit-col-${a.key}`} onClick={() => onOpenAbility(a.key)}>
                    <AbilityIcon name={a.icon} size={16} />
                    <span>{a.label}</span>
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row, i) => {
              const group = CREW_GROUP[row.role_key] ?? 4;
              const first = i === 0 || (CREW_GROUP[visibleRows[i - 1].role_key] ?? 4) !== group;
              return (
                <React.Fragment key={row.role_key}>
                  {first ? <tr className="kit-matrix-group"><th colSpan={abilities.length + 1}>{GROUP_NAMES[group]}</th></tr> : null}
                  <tr data-testid={`kit-row-${row.role_key}`} data-focus={focusRole === row.role_key || undefined}>
                    <th scope="row">
                      <a className="kit-rowhead" href={`?area=workforce&role=${encodeURIComponent(row.role_key)}&tab=skills`}
                         data-tip={`${row.display_name ?? row.role_key} · AI agent`}>
                        <AgentAvatar agent={row.role_key} size={20} aiBadge={false} />
                        <span>{agentName(row.role_key)}</span>
                      </a>
                    </th>
                    {abilities.map((a) => <td key={a.key}>{cell(a, row.role_key)}</td>)}
                  </tr>
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <ul className="kit-who-list" data-testid="kit-who-list">
        {visibleRows.map((row) => (
          <li key={row.role_key} data-open={expanded === row.role_key || undefined}>
            <button type="button" className="kit-rowhead" aria-expanded={expanded === row.role_key}
                    onClick={() => setExpanded(expanded === row.role_key ? null : row.role_key)}>
              <AgentAvatar agent={row.role_key} size={20} aiBadge={false} />
              <span>{agentName(row.role_key)}</span>
              <span className="kit-faint">{abilities.filter((a) => a.holders.includes(row.role_key)).length}</span>
            </button>
            {expanded === row.role_key ? (
              <div className="kit-who-list-cells">
                {abilities.map((a) => (
                  <div key={a.key} className="kit-who-item">
                    <AbilityIcon name={a.icon} size={16} />
                    <span>{a.label}</span>
                    {cell(a, row.role_key)}
                  </div>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
