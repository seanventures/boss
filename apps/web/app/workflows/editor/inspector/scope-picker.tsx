"use client";

import React, { useMemo, useRef, useState } from "react";
import { X } from "@phosphor-icons/react";

import type { Palette } from "../../../../lib/workflow-editor-types";
import { abilityOf, itemLabel, itemsByAbility, notSetUp, setUpHref, useKitAbilities } from "../../../../lib/use-kit-abilities";
import { Popover } from "./popover";

const GROUP_WORD: Record<string, string> = { code: "Code", research: "Research", coordinate: "Coordinate", ship: "Ship", media: "Media" };

type Kind = "skill" | "mcp";

/**
 * What an agent task may use: chips, plus a `+ Skill` / `+ MCP` picker.
 *
 * A STEP SCOPES, IT NEVER GRANTS. The options are what the named agent HOLDS, or, with Auto , 
 * what any agent holds, grouped by agent, since BOSS then picks among the holders. A chip the
 * named agent does not hold turns amber rather than disappearing: the save would refuse it, and
 * the operator should see which one before pressing Save.
 */
export function ScopePicker({ kind, selected, captain, palette, onChange, projectId, problem }: {
  kind: Kind;
  selected: string[];
  captain: string | null;
  palette: Palette;
  onChange(next: string[]): void;
  /** For the Kit's abilities: offered first, and `Not set up` where they are not. */
  projectId?: string;
  /** The Check's problem on this field, said in one line (the whole of it in the tooltip). */
  problem?: string | null;
}) {
  // ABILITIES FIRST (skills-mcp spec §3.3): what a step can use, in outcomes, each with the
  // skills or servers behind it. One not set up is red and links to where it is set up.
  const abilities = useKitAbilities(projectId);
  const byAbility = useMemo(() => itemsByAbility(abilities, kind), [abilities, kind]);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const holdingsOf = (a: Palette["agents"][number]) => (kind === "skill" ? a.skills : a.mcp);
  const named = captain ? palette.agents.find((a) => a.role_key === captain) : undefined;

  const groups = useMemo(() => {
    if (captain) {
      return [{ agent: named?.name ?? captain, items: named ? [...holdingsOf(named)].sort() : [] }];
    }
    return palette.agents
      .map((a) => ({ agent: a.name || a.role_key, items: [...holdingsOf(a)].sort() }))
      .filter((g) => g.items.length);
  }, [palette.agents, captain, named, kind]);

  const heldByAnyone = useMemo(() => new Set(palette.agents.flatMap(holdingsOf)),
    [palette.agents, kind]);
  const notHeld = (item: string) => (captain
    ? !(named && holdingsOf(named).includes(item))
    : !heldByAnyone.has(item));

  const toggle = (item: string) =>
    onChange(selected.includes(item) ? selected.filter((x) => x !== item) : [...selected, item]);

  const q = query.trim().toLowerCase();
  const labelOf = (item: string) => itemLabel(abilities, kind, item);
  const matches = (item: string) => !q || item.toLowerCase().includes(q) || labelOf(item).toLowerCase().includes(q);
  // A chip already says `Not set up, Set up `, so the Check's sentence about the same item is
  // not said again under it (K15); any other problem is one line.
  const flagged = selected.some((item) => notSetUp(abilityOf(abilities, kind, item)));
  const label = kind === "skill" ? "Skill" : "MCP";
  const whose = named?.name ?? captain ?? "";

  return (
    <div className="wf-scope" data-testid={`wf-scope-${kind}`}>
      <div className="wf-chips">
        {selected.length === 0 ? <span className="wf-chip wf-chip-ghost">Any it holds</span> : null}
        {selected.map((item) => {
          const amber = notHeld(item);
          const ability = abilityOf(abilities, kind, item);
          const unset = notSetUp(ability);
          return (
            <React.Fragment key={item}>
              <span className="wf-chip" data-amber={amber || undefined} data-tone={unset ? "red" : undefined}
                    data-testid={`wf-scope-chip-${kind}-${item}`}
                    title={[labelOf(item) !== item ? item : null,
                            unset ? `${ability!.label}, Not set up`
                              : amber ? (captain ? `Not held by ${whose}` : "Not held by any agent") : null]
                      .filter(Boolean).join(", ") || item}>
                {labelOf(item)}
                <button type="button" className="wf-chip-x" aria-label={`Remove ${item}`}
                        onClick={() => toggle(item)}><X size={10} weight="bold" /></button>
              </span>
              {unset ? (
                <a className="wf-chip" data-tone="red" href={setUpHref(ability!.key)}
                   title="Open this ability in Skills & MCP" data-testid={`wf-scope-setup-${kind}-${item}`}>
                  Not set up, Set up 
                </a>
              ) : null}
            </React.Fragment>
          );
        })}
        <button ref={trigger} type="button" className="wf-chip wf-chip-add"
                aria-expanded={open} data-testid={`wf-scope-add-${kind}`}
                onClick={() => setOpen((o) => !o)}>+ {label}</button>
      </div>
      {problem && !flagged ? (
        <span className="wf-field-error wf-one-line" role="alert" title={problem}
              data-testid={`wf-scope-problem-${kind}`}>{problem}</span>
      ) : null}
      <Popover anchor={trigger} open={open} onClose={() => { setOpen(false); setQuery(""); }}
               label={kind === "skill" ? "Limit skills" : "Limit MCP"} testId={`wf-scope-pop-${kind}`}>
        <input className="wf-pop-search" autoFocus value={query} placeholder="Search"
               aria-label="Search" onChange={(e) => setQuery(e.target.value)} />
        <div className="wf-pop-scroll">
          {byAbility.length ? (
            <fieldset className="wf-pop-group" data-testid={`wf-scope-abilities-${kind}`}>
              <legend>Abilities</legend>
              {byAbility.filter((g) => !q || g.ability.label.toLowerCase().includes(q)
                                      || g.items.some(matches)).map(({ ability, items }) => {
                const all = items.every((i) => selected.includes(i));
                const unset = notSetUp(ability);
                return (
                  <label key={ability.key} className="wf-check" data-testid={`wf-scope-ability-${kind}-${ability.key}`}
                         title={`${GROUP_WORD[ability.group] ?? ability.group}, ${items.map(labelOf).join(", ")}`}>
                    <input type="checkbox" checked={all}
                           onChange={() => onChange(all ? selected.filter((x) => !items.includes(x))
                                                        : [...new Set([...selected, ...items])])} />
                    <span>{ability.label}</span>
                    {unset ? (
                      <a className="wf-chip" data-tone="red" href={setUpHref(ability.key)}
                         title="Open this ability in Skills & MCP" data-testid={`wf-scope-ability-setup-${ability.key}`}
                         onClick={(e) => e.stopPropagation()}>Not set up, Set up </a>
                    ) : null}
                  </label>
                );
              })}
            </fieldset>
          ) : null}
          {groups.map((group) => {
            const items = group.items.filter(matches);
            if (!items.length) return null;
            return (
              <fieldset key={group.agent} className="wf-pop-group">
                {groups.length > 1 || !captain ? <legend>{group.agent}</legend> : null}
                {items.map((item) => (
                  <label key={`${group.agent}:${item}`} className="wf-check" title={item}>
                    <input type="checkbox" checked={selected.includes(item)}
                           data-testid={`wf-scope-opt-${kind}-${item}`}
                           onChange={() => toggle(item)} />
                    <span>{labelOf(item)}</span>
                  </label>
                ))}
              </fieldset>
            );
          })}
          {groups.every((g) => !g.items.some(matches)) ? (
            <div className="wf-faint wf-pop-empty">{captain ? `${whose} holds none` : "None held"}</div>
          ) : null}
        </div>
      </Popover>
    </div>
  );
}
