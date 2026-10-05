"use client";

/**
 * The Log's list (spec §4.2.6): one 32px line per row, read like the river — local time to the
 * second (UTC in the tooltip), the level as a glyph AND a word (never colour alone), the agent's
 * face (or the stream's icon), the title with the first line of its body, and on the right the
 * run's name, how long it took, what it cost (money only) and `×n` when identical lines fold.
 *
 * WINDOWED: only the lines on screen (plus a margin) are in the DOM, whatever the depth — a
 * 50,000-line range never puts 50,000 rows in the page.
 */
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Info, Warning, XCircle } from "@phosphor-icons/react";

import type { ActivityLogItem } from "../../lib/activity-log";
import { clock, took, utcTitle } from "../../lib/log-time";
import { usd } from "../../lib/money";
import { nameOf, RunFace } from "../agents/run-face";
import { STREAM_ICON } from "./facet-rail";
import {
  REFUSAL_KINDS, LEVEL_LABEL, costOf, durationOf, firstLine, flatten, runChip, windowOf, type DisplayRow,
} from "./log-model";
import { useIdentityVersion } from "../../lib/use-agent-identities";

const LEVEL_GLYPH: Record<string, React.ComponentType<{ size?: number; weight?: "bold" | "fill" | "regular" }>> = {
  error: XCircle, warn: Warning, info: Info,
};

/** Who said it: the agent's face; BOSS for its own processes; else the stream's icon. */
function Who({ item }: { item: ActivityLogItem }) {
  if (item.agent) return <RunFace roleKey={item.agent} size={20} />;
  if (item.source === "app_log" && (item.service === "api" || item.service === "worker" || !item.service)) {
    return <RunFace roleKey={null} variant="boss" size={20} />;
  }
  const Icon = STREAM_ICON[item.source];
  return Icon ? <span className="lx-src-icon" aria-hidden><Icon size={14} /></span> : null;
}

export function lineLabel(item: ActivityLogItem): string {
  const who = item.agent ? nameOf(item.agent).name : "";
  return [clock(item.createdAt), LEVEL_LABEL[item.level] ?? item.level, who, item.title].filter(Boolean).join(", ");
}

export function LogLine({ row, labels, selected, fresh, onOpen, onFold }: {
  row: Extract<DisplayRow, { kind: "line" }>;
  labels: Record<string, string>;
  selected: boolean;
  fresh: boolean;
  onOpen: (id: string) => void;
  onFold: (foldKey: string) => void;
}) {
  useIdentityVersion(); // names land when the install's agents load
  const { item } = row;
  const Glyph = LEVEL_GLYPH[item.level];
  const run = runChip(item, labels);
  const cost = costOf(item);
  const dur = took(durationOf(item));
  const preview = firstLine(item.body);
  return (
    <div className="lx-row" role="option" aria-selected={selected} tabIndex={selected ? 0 : -1}
         data-testid="lx-row" data-id={item.id} data-level={item.level} data-fresh={fresh || undefined}
         data-in-fold={row.inFold || undefined} aria-label={lineLabel(item)}
         style={{ transform: `translateY(${row.top}px)` }}
         onClick={(e) => { if (!(e.target as Element).closest("button")) onOpen(item.id); }}>
      <time className="lx-time" dateTime={item.createdAt} title={utcTitle(item.createdAt)}>{clock(item.createdAt)}</time>
      <span className="lx-lvl" data-level={item.level}>
        {Glyph ? <Glyph size={12} weight="bold" aria-hidden /> : <span className="lx-dot" data-level={item.level} aria-hidden />}
        <span>{LEVEL_LABEL[item.level] ?? item.level}</span>
      </span>
      <span className="lx-who"><Who item={item} /></span>
      <span className="lx-title">
        <span className="lx-title-text" data-user-text title={item.title}>{item.title || item.kind}</span>
        {preview ? <span className="lx-preview" data-user-text title={preview}>{preview}</span> : null}
      </span>
      <span className="lx-right">
        {REFUSAL_KINDS.has(item.kind) ? <span className="lx-limit" title="A limit stopped this work">Limit</span> : null}
        {run ? <span className="lx-run" data-user-text title={run.id}>{run.label || run.id.slice(0, 12)}</span> : null}
        {dur ? <span className="lx-took" title={`Took ${dur}`}>{dur}</span> : null}
        {cost > 0 ? <span className="lx-cost" title="Metered model spend">{usd(cost)}</span> : null}
        {row.folded > 1 && !row.inFold ? (
          <button type="button" className="lx-fold" data-testid="lx-fold" title={`${row.folded} identical lines folded`}
                  onClick={() => onFold(row.foldKey)}>×{row.folded}</button>
        ) : null}
      </span>
    </div>
  );
}

export function LogRows({ items, labels, byRun, selectedId, arrived, onOpen, onHolding, onNearEnd, footer, toTop = 0 }: {
  items: ActivityLogItem[];
  labels: Record<string, string>;
  byRun: boolean;
  selectedId: string | null;
  arrived: ReadonlySet<string>;
  onOpen: (id: string) => void;
  /** The reader has scrolled away from the top: new lines must wait. */
  onHolding: (holding: boolean) => void;
  onNearEnd: () => void;
  footer: React.ReactNode;
  /** Bumped to take the reader to the newest line (`n new ↑`, D9). */
  toTop?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(800);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const rows = useMemo(() => flatten(items, { byRun, open, labels }), [items, byRun, open, labels]);
  const visible = useMemo(() => windowOf(rows, scrollTop, height), [rows, scrollTop, height]);
  const total = rows.length ? rows[rows.length - 1].top + rows[rows.length - 1].height : 0;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return undefined;
    const measure = () => setHeight(el.clientHeight || 800);
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // `n new ↑` shows the new lines AND goes to them: they arrive at the top.
  useEffect(() => {
    if (!toTop || !box.current) return;
    const el = box.current;
    if (typeof el.scrollTo === "function") el.scrollTo({ top: 0, behavior: "smooth" });
    el.scrollTop = 0;
    setScrollTop(0);
  }, [toTop]);

  // The selected line stays in view as J/K moves it.
  useEffect(() => {
    if (!selectedId || !box.current) return;
    const row = rows.find((r) => r.kind === "line" && r.item.id === selectedId);
    if (!row) return;
    const el = box.current;
    if (row.top < el.scrollTop) el.scrollTop = row.top;
    else if (row.top + row.height > el.scrollTop + el.clientHeight) el.scrollTop = row.top + row.height - el.clientHeight;
  }, [selectedId, rows]);

  return (
    <div ref={box} className="lx-list" role="listbox" aria-label="Log lines" data-testid="lx-list"
         onScroll={(e) => {
           const el = e.currentTarget;
           setScrollTop(el.scrollTop);
           onHolding(el.scrollTop > 40);
           if (el.scrollTop + el.clientHeight > total - 600) onNearEnd();
         }}>
      <div className="lx-list-inner" style={{ height: total }}>
        {visible.map((row) => {
          if (row.kind === "day") {
            return <div key={row.key} className="lx-day" style={{ transform: `translateY(${row.top}px)` }}>{row.label}</div>;
          }
          if (row.kind === "run") {
            return (
              <div key={row.key} className="lx-runhead" style={{ transform: `translateY(${row.top}px)` }}
                   title="Counted on loaded lines">
                <span data-user-text>{row.label}</span>
                <span className="lx-runhead-n">{row.lines} lines{row.errors ? ` · ${row.errors} errors` : ""}</span>
              </div>
            );
          }
          return (
            <LogLine key={row.key} row={row} labels={labels} selected={row.item.id === selectedId}
                     fresh={arrived.has(row.item.id)} onOpen={onOpen}
                     onFold={(k) => setOpen((cur) => {
                       const next = new Set(cur);
                       if (next.has(k)) next.delete(k); else next.add(k);
                       return next;
                     })} />
          );
        })}
      </div>
      <div className="lx-list-foot">{footer}</div>
    </div>
  );
}

/** The flattened line order, for J/K. */
export function lineOrder(items: ActivityLogItem[], byRun: boolean, labels: Record<string, string>): string[] {
  return flatten(items, { byRun, open: new Set(), labels }).filter((r) => r.kind === "line").map((r) => (r as Extract<DisplayRow, { kind: "line" }>).item.id);
}
