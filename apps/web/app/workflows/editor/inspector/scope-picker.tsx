"use client";

import React, { useMemo, useRef, useState } from "react";
import { X } from "@phosphor-icons/react";

import type { Palette } from "../../../../lib/workflow-editor-types";
import { Popover } from "./popover";

type Kind = "skill" | "mcp";

/**
 * What an agent task may use: chips, plus a `+ Skill` / `+ MCP` picker.
 *
 * A STEP SCOPES, IT NEVER GRANTS. The options are what the named agent HOLDS, or — with Auto —
 * what any agent holds, grouped by agent, since BOSS then picks among the holders. A chip the
 * named agent does not hold turns amber rather than disappearing: the save would refuse it, and
 * the operator should see which one before pressing Save.
 */
export function ScopePicker({ kind, selected, captain, palette, onChange }: {
  kind: Kind;
  selected: string[];
  captain: string | null;
  palette: Palette;
  onChange(next: string[]): void;
}) {
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
  const label = kind === "skill" ? "Skill" : "MCP";
  const whose = named?.name ?? captain ?? "";

  return (
    <div className="wf-scope" data-testid={`wf-scope-${kind}`}>
      <div className="wf-chips">
        {selected.length === 0 ? <span className="wf-chip wf-chip-ghost">Any it holds</span> : null}
        {selected.map((item) => {
          const amber = notHeld(item);
          return (
            <span key={item} className="wf-chip" data-amber={amber || undefined}
                  data-testid={`wf-scope-chip-${kind}-${item}`}
                  title={amber ? (captain ? `Not held by ${whose}` : "Not held by any agent") : item}>
              {item}
              <button type="button" className="wf-chip-x" aria-label={`Remove ${item}`}
                      onClick={() => toggle(item)}><X size={10} weight="bold" /></button>
            </span>
          );
        })}
        <button ref={trigger} type="button" className="wf-chip wf-chip-add"
                aria-expanded={open} data-testid={`wf-scope-add-${kind}`}
                onClick={() => setOpen((o) => !o)}>+ {label}</button>
      </div>
      <Popover anchor={trigger} open={open} onClose={() => { setOpen(false); setQuery(""); }}
               label={kind === "skill" ? "Limit skills" : "Limit MCP"} testId={`wf-scope-pop-${kind}`}>
        <input className="wf-pop-search" autoFocus value={query} placeholder="Search"
               aria-label="Search" onChange={(e) => setQuery(e.target.value)} />
        <div className="wf-pop-scroll">
          {groups.map((group) => {
            const items = group.items.filter((i) => !q || i.toLowerCase().includes(q));
            if (!items.length) return null;
            return (
              <fieldset key={group.agent} className="wf-pop-group">
                {groups.length > 1 || !captain ? <legend>{group.agent}</legend> : null}
                {items.map((item) => (
                  <label key={`${group.agent}:${item}`} className="wf-check">
                    <input type="checkbox" checked={selected.includes(item)}
                           data-testid={`wf-scope-opt-${kind}-${item}`}
                           onChange={() => toggle(item)} />
                    <span>{item}</span>
                  </label>
                ))}
              </fieldset>
            );
          })}
          {groups.every((g) => !g.items.some((i) => !q || i.toLowerCase().includes(q))) ? (
            <div className="wf-faint wf-pop-empty">{captain ? `${whose} holds none` : "None held"}</div>
          ) : null}
        </div>
      </Popover>
    </div>
  );
}
