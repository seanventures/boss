"use client";

/**
 * Search the build's logs (build page): one box over the Story, every tool call (its command or
 * path and its output) and the verify output. Matches are highlighted; Enter / Shift+Enter (or
 * ↓ ↑) jump between them; `Errors` and `Stack traces` are quick filters over the same lines.
 * `/` focuses the box, Esc clears it. Choosing a tool-call match opens that call in the Story.
 */
import { CaretDown, CaretUp, MagnifyingGlass, X } from "@phosphor-icons/react";
import React, { useEffect, useMemo, useRef, useState } from "react";

import { highlight, searchLines, step, type LogLine, type QuickFilter } from "../../lib/build-detail";

export type SearchState = { query: string; filter: QuickFilter };

export function SearchBox({ value, onChange, count, current, onStep, inputRef }: {
  value: SearchState; onChange: (s: SearchState) => void; count: number; current: number;
  onStep: (dir: 1 | -1) => void; inputRef: React.RefObject<HTMLInputElement | null>;
}) {
  const active = !!value.query.trim() || !!value.filter;
  const pick = (f: QuickFilter) => onChange({ ...value, filter: value.filter === f ? null : f });
  return (
    <span className="fx-search" role="search" data-testid="fx-search">
      <span className="fx-search-box">
        <MagnifyingGlass size={12} aria-hidden />
        <input ref={inputRef} type="search" className="fx-search-input" value={value.query} placeholder="Search logs"
               aria-label="Search the story, tool calls and test output" title="Search logs ( / )"
               data-testid="fx-search-input"
               onChange={(e) => onChange({ ...value, query: e.target.value })}
               onKeyDown={(e) => {
                 if (e.key === "Enter") { e.preventDefault(); onStep(e.shiftKey ? -1 : 1); }
                 else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onChange({ query: "", filter: null }); e.currentTarget.blur(); }
               }} />
        {active ? (
          <span className="fx-search-count" aria-live="polite" data-testid="fx-search-count">
            {count ? `${current + 1}/${count}` : "0"}
          </span>
        ) : null}
        {active ? (
          <>
            <button type="button" className="fx-search-btn" aria-label="Previous match" disabled={!count} onClick={() => onStep(-1)}
                    data-testid="fx-search-prev"><CaretUp size={11} weight="bold" aria-hidden /></button>
            <button type="button" className="fx-search-btn" aria-label="Next match" disabled={!count} onClick={() => onStep(1)}
                    data-testid="fx-search-next"><CaretDown size={11} weight="bold" aria-hidden /></button>
            <button type="button" className="fx-search-btn" aria-label="Clear search" onClick={() => onChange({ query: "", filter: null })}
                    data-testid="fx-search-clear"><X size={11} weight="bold" aria-hidden /></button>
          </>
        ) : null}
      </span>
      <button type="button" className="fx-chip" aria-pressed={value.filter === "errors"} onClick={() => pick("errors")}
              title="Lines that say something failed" data-testid="fx-search-errors">Errors</button>
      <button type="button" className="fx-chip" aria-pressed={value.filter === "stack"} onClick={() => pick("stack")}
              title="Traceback, at …, Error:, FAILED, ✘" data-testid="fx-search-stack">Stack traces</button>
    </span>
  );
}

const SOURCE_WORD = { story: "Story", tool: "Tool", verify: "Tests" } as const;

export function SearchResults({ hits, query, current, onPick }: {
  hits: LogLine[]; query: string; current: number; onPick: (hit: LogLine, i: number) => void;
}) {
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = list.current?.querySelector<HTMLElement>(`[data-hit="${current}"]`);
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
  }, [current]);
  if (!hits.length) return <span className="fx-muted" data-testid="fx-search-none">No matches</span>;
  let lastWhere = "";
  return (
    <ol ref={list} className="fx-hits" data-testid="fx-hits">
      {hits.map((h, i) => {
        // One head per place: each tool call, the verify output, a story row.
        const place = `${h.where}#${h.call ?? ""}`;
        const head = place !== lastWhere ? h.where : "";
        lastWhere = place;
        return (
          <li key={h.id} data-hit={i} data-current={i === current || undefined} data-source={h.source}
              className="fx-hit" data-testid={`fx-hit-${i}`}>
            {head ? <span className="fx-hit-where"><span className="fx-hit-src">{SOURCE_WORD[h.source]}</span>{head}</span> : null}
            <button type="button" className="fx-hit-line fx-mono" onClick={() => onPick(h, i)}
                    title={h.call !== undefined ? "Show this call in the story" : undefined} data-user-text>
              {highlight(h.text, query).map(([t, m], k) => (m ? <mark key={k}>{t}</mark> : <React.Fragment key={k}>{t}</React.Fragment>))}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/** The search's state and its matches over `index`. */
export function useLogSearch(index: LogLine[]) {
  const [state, setState] = useState<SearchState>({ query: "", filter: null });
  const [current, setCurrent] = useState(-1);
  const hits = useMemo(() => searchLines(index, state.query, state.filter), [index, state.query, state.filter]);
  useEffect(() => { setCurrent(hits.length ? 0 : -1); }, [state.query, state.filter]); // eslint-disable-line react-hooks/exhaustive-deps
  const active = !!state.query.trim() || !!state.filter;
  return {
    state, setState, hits, active,
    current: Math.min(current, hits.length - 1),
    setCurrent,
    step: (dir: 1 | -1) => setCurrent((c) => step(c, hits.length, dir)),
  };
}
