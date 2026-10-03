"use client";

/**
 * QuickAdd: the `+` on a connector. A search and the top six steps, inserted right there.
 *
 * Keyboard first: the search has focus, ↑/↓ move through the list, Enter adds, Esc closes.
 * It is also the keyboard and touch alternative to dragging from the palette.
 */
import { MagnifyingGlass } from "@phosphor-icons/react";
import React, { useId, useMemo, useRef, useState } from "react";

import type { Palette, PaletteItem } from "../../../lib/workflow-editor-types";
import { NodeIcon } from "../flow/type-icons";
import { matches, paletteEntries, type PaletteEntry } from "./palette";
import { Popover } from "./inspector/popover";

export const QUICK_ADD_TOP = 6;
const QUICK_ADD_SEARCHED = 8;

/** With no query: the agent task, a gate, then built-ins, up to six. */
export function quickAddEntries(palette: Palette | null, query: string): PaletteEntry[] {
  const all = paletteEntries(palette).filter((e) => !e.disabled);
  if (query.trim()) return all.filter((e) => matches(e, query)).slice(0, QUICK_ADD_SEARCHED);
  const first = all.filter((e) => e.group === "agent" || e.key === "block:decision");
  const rest = all.filter((e) => !first.includes(e) && e.group !== "flow");
  return [...first, ...rest].slice(0, QUICK_ADD_TOP);
}

export function QuickAdd({ palette, anchor, open, onPick, onClose }: {
  palette: Palette | null;
  anchor: HTMLElement | null;
  open: boolean;
  onPick(item: PaletteItem): void;
  onClose(): void;
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listId = useId();
  const anchorRef = useRef<HTMLElement | null>(null);
  anchorRef.current = anchor;
  const entries = useMemo(() => quickAddEntries(palette, query), [palette, query]);
  const current = Math.min(active, Math.max(0, entries.length - 1));

  const pick = (entry: PaletteEntry | undefined) => {
    if (!entry) return;
    onPick(entry.item);
    setQuery("");
    setActive(0);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((current + 1) % Math.max(1, entries.length)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((current - 1 + entries.length) % Math.max(1, entries.length)); }
    else if (e.key === "Enter") { e.preventDefault(); pick(entries[current]); }
  };

  return (
    <Popover anchor={anchorRef} open={open} onClose={() => { setQuery(""); setActive(0); onClose(); }}
             label="Add a step" width={320} testId="wf-quick-add">
      <div className="wf-quick">
        <label className="wf-pal-search">
          <MagnifyingGlass size={14} weight="bold" aria-hidden />
          <input type="search" placeholder="Add a step…" aria-label="Add a step" value={query} autoFocus
                 role="combobox" aria-expanded aria-controls={listId}
                 aria-activedescendant={entries[current] ? `${listId}-${current}` : undefined}
                 onChange={(e) => { setQuery(e.target.value); setActive(0); }} onKeyDown={onKeyDown} />
        </label>
        <ul className="wf-quick-list" role="listbox" id={listId} aria-label="Steps">
          {entries.map((entry, i) => (
            <li key={entry.key} id={`${listId}-${i}`} role="option" aria-selected={i === current}
                className="wf-quick-item" data-active={i === current ? "true" : undefined}
                data-tip={entry.tip} data-testid={`wf-quick-${entry.key}`}
                onPointerEnter={() => setActive(i)}
                onClick={() => pick(entry)}>
              <span className="wf-pal-icon" data-type={entry.type}><NodeIcon icon={entry.icon} type={entry.type} size={14} /></span>
              <span className="wf-pal-label">{entry.label}</span>
            </li>
          ))}
          {entries.length === 0 ? <li className="wf-quick-none" role="presentation">No match</li> : null}
        </ul>
      </div>
    </Popover>
  );
}
