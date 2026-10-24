"use client";

/**
 * THE PROJECT TREE (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.5): an IDE tree beside the canvas.
 *
 * Tabs Project · Rules · Learning. Children load lazily from /tree/children and page at 200; the
 * list is virtualised (a package can hold 400 classes); rows are 14 px text on 28 px rows with a
 * chevron, a solid kind dot, the label and a right-aligned count. The search filters it (matches
 * plus their ancestors, matches highlighted); clicking a row with a node focuses it; the
 * selection follows the canvas both ways and scrolls into view. Keyboard: the WAI-ARIA tree
 * pattern (arrows move and expand, Home/End, Enter focuses).
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { kindName, kindToken } from "../../lib/kg-layout";
import { getTreeChildren } from "../../lib/kg-view";
import type { KgTreeRow, KgTreeTab } from "../../lib/kg-view-types";

const ROW_H = 28;
const OVERSCAN = 12;
const ROOT = "\u0000root";

type Page = { rows: KgTreeRow[]; total: number; next: number | null; loading: boolean; error: string | null };

export type TreePick = { nodeId: string | null; row: KgTreeRow };

const TABS: Array<{ key: KgTreeTab; label: string; tip: string }> = [
  { key: "project", label: "Project", tip: "Areas, packages, classes and methods" },
  { key: "rules", label: "Rules", tip: "Proposed, approved and rejected business rules" },
  { key: "learning", label: "Learning", tip: "What agents learned while they worked, newest first" },
];

function dotToken(row: KgTreeRow): string {
  if (row.kind === "group") return row.layer === "rules" ? "--kg-layer-rules" : row.layer === "learning" ? "--kg-layer-learning" : "--kg-layer-code";
  return kindToken(row.kind);
}

type Flat = { row: KgTreeRow; depth: number; parentKey: string | null };

export function ProjectTree({ projectId, actor, tab, onTab, query, selectedId, onPick, refreshKey, reveal }: {
  projectId: string;
  actor: string;
  tab: KgTreeTab;
  onTab: (tab: KgTreeTab) => void;
  /** The area's search: filters the tree to matches and their ancestors. */
  query: string;
  selectedId: string | null;
  onPick: (pick: TreePick) => void;
  /** Bumped when live learnings arrive: the open Learning/Rules lists reload. */
  refreshKey?: number;
  /** A node to open the tree to (a search pick): its label is searched and its ancestors expanded. */
  reveal?: { label: string; nodeId: string } | null;
}) {
  const who = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [pages, setPages] = useState<Map<string, Page>>(new Map());
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [filtered, setFiltered] = useState<{ q: string; rows: KgTreeRow[]; loading: boolean; error: string | null } | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const [scroll, setScroll] = useState({ top: 0, height: 600 });
  const q = (reveal?.label ?? query).trim();

  const load = useCallback(async (parent: string | null, offset = 0) => {
    const key = parent ?? ROOT;
    setPages((m) => {
      const next = new Map(m);
      const prev = m.get(key);
      next.set(key, { rows: prev?.rows ?? [], total: prev?.total ?? 0, next: prev?.next ?? null, loading: true, error: null });
      return next;
    });
    const r = await getTreeChildren(who, { tab, parent, offset });
    setPages((m) => {
      const next = new Map(m);
      const prev = m.get(key);
      if (!r.ok) next.set(key, { rows: prev?.rows ?? [], total: prev?.total ?? 0, next: prev?.next ?? null, loading: false, error: r.error });
      else next.set(key, { rows: offset ? [...(prev?.rows ?? []), ...r.data.rows] : r.data.rows, total: r.data.total, next: r.data.next_offset, loading: false, error: null });
      return next;
    });
  }, [who, tab]);

  // A new tab (or project) starts from its roots.
  useEffect(() => {
    setPages(new Map());
    setOpen(new Set());
    setCursor(null);
    void load(null);
  }, [load, refreshKey]);

  // The search: one call answers the matches plus their ancestors (§3 tree/children with q).
  useEffect(() => {
    if (!q) { setFiltered(null); return undefined; }
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = setTimeout(async () => {
      setFiltered((f) => ({ q, rows: f?.q === q ? f.rows : [], loading: true, error: null }));
      const r = await getTreeChildren(who, { tab, q }, ctl?.signal);
      if (r.ok) setFiltered({ q, rows: r.data.rows, loading: false, error: null });
      else if (r.status !== -1) setFiltered({ q, rows: [], loading: false, error: r.error });
    }, 150);
    return () => { clearTimeout(timer); ctl?.abort(); };
  }, [q, who, tab]);

  const flat: Flat[] = useMemo(() => {
    if (filtered) {
      // Matches + ancestors: build the forest from parent_key and show it fully open.
      const kids = new Map<string, KgTreeRow[]>();
      const keys = new Set(filtered.rows.map((r) => r.key));
      for (const r of filtered.rows) {
        const p = r.parent_key && keys.has(r.parent_key) ? r.parent_key : ROOT;
        kids.set(p, [...(kids.get(p) ?? []), r]);
      }
      const out: Flat[] = [];
      const walk = (key: string, depth: number) => {
        for (const r of kids.get(key) ?? []) {
          out.push({ row: r, depth, parentKey: key === ROOT ? null : key });
          walk(r.key, depth + 1);
        }
      };
      walk(ROOT, 0);
      return out;
    }
    const out: Flat[] = [];
    const walk = (key: string, depth: number) => {
      for (const r of pages.get(key)?.rows ?? []) {
        out.push({ row: r, depth, parentKey: key === ROOT ? null : key });
        if (open.has(r.key)) walk(r.key, depth + 1);
      }
    };
    walk(ROOT, 0);
    return out;
  }, [filtered, pages, open]);

  // "Load more" rows at the end of a paged level, as their own focusable entries.
  const moreAfter = useMemo(() => {
    const m = new Map<string, string>();
    if (filtered) return m;
    for (const [key, page] of pages) {
      if (page.next === null) continue;
      const rows = page.rows;
      if (!rows.length) continue;
      m.set(rows[rows.length - 1].key, key);
    }
    return m;
  }, [pages, filtered]);

  const toggle = (row: KgTreeRow) => {
    if (!row.has_children || filtered) return;
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(row.key)) next.delete(row.key);
      else {
        next.add(row.key);
        if (!pages.has(row.key)) void load(row.key);
      }
      return next;
    });
  };

  const activate = (row: KgTreeRow) => {
    setCursor(row.key);
    if (row.node_id) onPick({ nodeId: row.node_id, row });
    else if (row.has_children) {
      toggle(row);
      onPick({ nodeId: null, row });
    }
  };

  // Selection follows the canvas: the row holding the selected node becomes the cursor and is
  // scrolled into view.
  const selectedIndex = flat.findIndex((f) => f.row.node_id && f.row.node_id === selectedId);
  useEffect(() => {
    if (selectedIndex < 0) return;
    setCursor(flat[selectedIndex].row.key);
    const el = scroller.current;
    if (!el) return;
    const top = selectedIndex * ROW_H;
    if (top < el.scrollTop || top > el.scrollTop + el.clientHeight - ROW_H) el.scrollTop = Math.max(0, top - el.clientHeight / 2);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIndex, selectedId]);

  const cursorIndex = Math.max(0, flat.findIndex((f) => f.row.key === cursor));
  const focusRow = (i: number) => {
    const target = flat[Math.max(0, Math.min(flat.length - 1, i))];
    if (!target) return;
    setCursor(target.row.key);
    requestAnimationFrame?.(() => {
      scroller.current?.querySelector<HTMLElement>(`[data-key="${CSS?.escape ? CSS.escape(target.row.key) : target.row.key}"]`)?.focus();
    });
    const el = scroller.current;
    if (el) {
      const top = flat.indexOf(target) * ROW_H;
      if (top < el.scrollTop) el.scrollTop = top;
      else if (top > el.scrollTop + el.clientHeight - ROW_H) el.scrollTop = top - el.clientHeight + ROW_H;
    }
  };

  const onKey = (e: React.KeyboardEvent, i: number) => {
    const f = flat[i];
    if (!f) return;
    const { row } = f;
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); focusRow(i + 1); break;
      case "ArrowUp": e.preventDefault(); focusRow(i - 1); break;
      case "Home": e.preventDefault(); focusRow(0); break;
      case "End": e.preventDefault(); focusRow(flat.length - 1); break;
      case "ArrowRight":
        e.preventDefault();
        if (row.has_children && !filtered && !open.has(row.key)) toggle(row);
        else if (flat[i + 1]?.parentKey === row.key) focusRow(i + 1);
        break;
      case "ArrowLeft": {
        e.preventDefault();
        if (open.has(row.key) && !filtered) toggle(row);
        else if (f.parentKey) focusRow(flat.findIndex((x) => x.row.key === f.parentKey));
        break;
      }
      case "Enter":
      case " ":
        e.preventDefault();
        activate(row);
        break;
      default:
    }
  };

  useEffect(() => {
    const el = scroller.current;
    if (!el) return undefined;
    const on = () => setScroll({ top: el.scrollTop, height: el.clientHeight || 600 });
    on();
    el.addEventListener("scroll", on, { passive: true });
    return () => el.removeEventListener("scroll", on);
  }, []);

  const start = Math.max(0, Math.floor(scroll.top / ROW_H) - OVERSCAN);
  const end = Math.min(flat.length, Math.ceil((scroll.top + Math.max(scroll.height, 600)) / ROW_H) + OVERSCAN);
  const root = pages.get(ROOT);
  const loadingRoot = !filtered && (!root || (root.loading && !root.rows.length));
  const errorRoot = !filtered ? root?.error : filtered.error;
  const lower = q.toLowerCase();

  return (
    <div className="kg-tree" data-testid="kg-tree" data-tab={tab}>
      <div className="inf-seg kg-tree-tabs" role="tablist" aria-label="Tree">
        {TABS.map((t) => (
          <button key={t.key} type="button" role="tab" className="inf-seg-btn" aria-selected={tab === t.key}
                  title={t.tip} data-testid={`kg-tree-tab-${t.key}`} onClick={() => onTab(t.key)}>{t.label}</button>
        ))}
      </div>
      {q ? (
        <p className="kg-tree-note" data-testid="kg-tree-note" role="status">
          {filtered?.loading ? "Searching…" : filtered && !filtered.error
            ? `${filtered.rows.filter((r) => r.match).length.toLocaleString("en-US")} matching ${filtered.rows.filter((r) => r.match).length === 1 ? "row" : "rows"}, opened to where they are`
            : null}
        </p>
      ) : null}
      <div ref={scroller} className="kg-tree-scroll" role="tree" aria-label={`${TABS.find((t) => t.key === tab)?.label} tree`}
           aria-busy={loadingRoot || undefined} data-testid="kg-tree-list">
        {loadingRoot ? (
          <div className="kg-skel-rows" aria-hidden data-testid="kg-tree-skeleton">
            {Array.from({ length: 12 }, (_, i) => <span key={i} className="kg-skel-row" style={{ width: `${55 + ((i * 37) % 40)}%` }} />)}
          </div>
        ) : null}
        {errorRoot ? (
          <div className="kg-inline-err" role="alert" data-testid="kg-tree-error">
            <span>{errorRoot}</span>
            <button type="button" className="inf-link" onClick={() => (filtered ? setFiltered(null) : void load(null))}>Retry</button>
          </div>
        ) : null}
        {!loadingRoot && !errorRoot && flat.length === 0 && !(filtered?.loading) ? (
          <p className="kg-tree-empty" data-testid="kg-tree-empty">
            {q ? `Nothing in ${TABS.find((t) => t.key === tab)?.label} matches “${q}”.`
              : tab === "rules" ? "No rules yet. Agents propose them when they scan an area for business rules."
              : tab === "learning" ? "Nothing learned yet. Agents record what they find while they work."
              : "No code yet. Index a repository in Settings."}
          </p>
        ) : null}
        <div style={{ height: flat.length * ROW_H, position: "relative" }}>
          {flat.slice(start, end).map((f, j) => {
            const i = start + j;
            const { row, depth } = f;
            const isOpen = open.has(row.key) || !!filtered;
            const page = pages.get(row.key);
            const selected = !!row.node_id && row.node_id === selectedId;
            const match = filtered && row.match;
            return (
                <div key={row.key} role="treeitem" className="kg-row" data-key={row.key} data-testid="kg-tree-row" data-level={row.level}
                     data-kind={row.kind} data-match={match || undefined}
                     aria-level={depth + 1} aria-selected={selected} aria-expanded={row.has_children ? isOpen : undefined}
                     tabIndex={i === (selectedIndex >= 0 ? selectedIndex : cursorIndex) ? 0 : -1}
                     style={{ top: i * ROW_H, paddingLeft: 6 + depth * 14 }}
                     title={`${kindName(row.kind)} · ${row.label}`}
                     onClick={() => activate(row)} onKeyDown={(e) => onKey(e, i)}>
                  <span className="kg-row-caret" aria-hidden
                        onClick={(e) => { if (row.has_children) { e.stopPropagation(); setCursor(row.key); toggle(row); } }}>
                    {row.has_children ? (page?.loading && isOpen ? "…" : isOpen ? "▾" : "▸") : ""}
                  </span>
                  <span className="kg-dot kg-row-dot" style={{ background: `var(${dotToken(row)})` }} aria-hidden />
                  <span className="kg-row-label">{highlight(row.label, match ? lower : "")}</span>
                  {row.count ? <span className="kg-row-count">{row.count.toLocaleString("en-US")}</span> : null}
                </div>
            );
          })}
        </div>
        {[...moreAfter.entries()].length ? (
          <div className="kg-tree-more">
            {[...moreAfter.values()].map((parent) => {
              const page = pages.get(parent)!;
              const owner = parent === ROOT ? "top level" : flat.find((f) => f.row.key === parent)?.row.label ?? "this level";
              return (
                <button key={parent} type="button" className="inf-link" data-testid="kg-tree-more" disabled={page.loading}
                        onClick={() => void load(parent === ROOT ? null : parent, page.rows.length)}>
                  {page.loading ? "Loading…" : `Show ${Math.min(200, page.total - page.rows.length).toLocaleString("en-US")} more in ${owner} (${page.rows.length.toLocaleString("en-US")} of ${page.total.toLocaleString("en-US")})`}
                </button>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function highlight(label: string, q: string): React.ReactNode {
  if (!q) return label;
  const i = label.toLowerCase().indexOf(q);
  if (i < 0) return label;
  return <>{label.slice(0, i)}<mark className="kg-mark">{label.slice(i, i + q.length)}</mark>{label.slice(i + q.length)}</>;
}
