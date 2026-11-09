"use client";

/**
 * The pieces Infra and Observability share (infra-observability spec §4.2, §7), frozen in
 * Phase 0 so the two streams draw one product: the area header row, the state dot, NEEDS YOU,
 * the partial-data line, a section head, the right drawer, the sheet, the typed confirm, and the
 * URL writer. Tokens only (`inf-` classes in infra.css); no inline style, no hex.
 */
import { useSearchParams } from "next/navigation";
import React, { useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";

import type { AttentionItem } from "../../lib/agents-live-board";
import type { Tone, Word } from "../../lib/infra-view";
import { AttentionCard } from "../agents/attention-card";
import { useAreaKeydown } from "../../lib/area-keys";

// ── URL state ───────────────────────────────────────────────────────────────────────────────

/** Patch the console URL in place. `push` for a drawer or a tab a person opened (Back closes
 *  it), `replace` for a filter or the window. Nothing here refreshes the console. */
export function writeUrl(patch: Record<string, string | null>, mode: "push" | "replace" = "push"): void {
  try {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    if (mode === "push") window.history.pushState(null, "", url.toString());
    else window.history.replaceState(null, "", url.toString());
  } catch { /* not a browser */ }
}

/** Re-read the URL on mount and on every back/forward.
 *
 *  THE FIRST RENDER READS WHAT THE SERVER READ. Reading `window.location` in the initial state
 *  made the server paint Fleet (it has no window) and the client hydrate Previews: React 19 does
 *  not patch a mismatched attribute, so the Fleet tab stayed selected over the Previews panel.
 *  The router's search params are the same on both sides; `window` is the fallback only where
 *  there is no router (tests). */
export function useUrlState<T>(read: (search: string) => T): [T, () => void] {
  const params = useSearchParams();
  const [state, setState] = useState<T>(() => read(params ? `?${params.toString()}`
    : typeof window === "undefined" ? "" : window.location.search));
  const reader = useRef(read);
  reader.current = read;
  useEffect(() => {
    const sync = () => setState(reader.current(window.location.search));
    sync();
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  return [state, () => setState(reader.current(window.location.search))];
}

// ── the state dot (§4.2): colour is never the only signal ───────────────────────────────────

export function Dot({ tone, label, ring }: { tone: Tone; label?: string; ring?: boolean }) {
  return <span className="inf-dot" data-tone={tone} data-ring={ring ? "1" : undefined}
               role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}

/** Dot + word. The word is the server's (`infra_view.word()`). `changed` rings the dot once. */
export function StateWord({ w, testId, changed }: { w: Word; testId?: string; changed?: boolean }) {
  return (
    <span className="inf-state" data-tone={w.tone} data-testid={testId} title={w.word}>
      <Dot tone={w.tone} ring={changed} />
      <span className="inf-state-word">{w.word}</span>
    </span>
  );
}

// ── the area header (§4.2) ──────────────────────────────────────────────────────────────────

export type HeadTab = { key: string; label: string; tip: string; count?: number; dot?: { tone: Tone; tip: string } | null };

/** Title (screen readers; the console title already names the area), pill tabs with arrow-key
 *  navigation, counts, spacer, actions. One row; wraps at narrow widths, tabs scroll in their
 *  own strip under 640 px. */
export function AreaHead({ area, title, tabs, active, onTab, counts, children }: {
  area: "infra" | "obs" | "kg"; title: string; tabs: HeadTab[]; active: string; onTab: (key: string) => void;
  counts?: React.ReactNode; children?: React.ReactNode;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: React.KeyboardEvent, i: number) => {
    const n = tabs.length;
    const to = e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n
      : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (to < 0) return;
    e.preventDefault();
    refs.current[to]?.focus();
    onTab(tabs[to].key);
  };
  return (
    <header className="inf-head" data-testid={`${area}-head`}>
      <h2 className="ag-sr">{title}</h2>
      <span className="inf-tabs" role="tablist" aria-label={`${title} views`}>
        {tabs.map((t, i) => (
          <button key={t.key} type="button" role="tab" ref={(el) => { refs.current[i] = el; }}
                  aria-selected={active === t.key} tabIndex={active === t.key ? 0 : -1}
                  className="inf-tab" data-testid={`${area}-tab-${t.key}`} title={t.tip}
                  onClick={() => onTab(t.key)} onKeyDown={(e) => onKey(e, i)}>
            {t.dot ? <span title={t.dot.tip}><Dot tone={t.dot.tone} label={t.dot.tip} /></span> : null}
            {t.label}
            {t.count ? <span className="inf-tab-count">{t.count}</span> : null}
          </button>
        ))}
      </span>
      {counts ? <span className="inf-head-counts" data-testid={`${area}-head-counts`}>{counts}</span> : null}
      <span className="ag-spacer" />
      {children}
    </header>
  );
}

/** `/` focuses Find (§7). */
export function FindBox({ value, onChange, label, testId }: { value: string; onChange: (v: string) => void; label: string; testId: string }) {
  const ref = useRef<HTMLInputElement>(null);
  useAreaKeydown((e: KeyboardEvent) => {
    const t = e.target as HTMLElement | null;
    if (e.key === "/" && !t?.closest?.("input, textarea, select, [contenteditable=true]") && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      ref.current?.focus();
    }
  }, { target: "document" });
  return (
    <label className="inf-find">
      <span className="ag-sr">{label}</span>
      <input ref={ref} type="search" placeholder="Find" value={value} data-testid={testId}
             title="Press / to search" onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

// ── NEEDS YOU (§4.2): AttentionCards, at most 4, then `+n more`; hidden when empty ──────────

export const CARDS_SHOWN = 4;
export function NeedsYou({ items, projectId, actor, orgId, onRefresh, testId }: {
  items: AttentionItem[]; projectId: string; actor: string; orgId?: string | null; onRefresh: () => void; testId: string;
}) {
  const [all, setAll] = useState(false);
  if (!items.length) return null;
  const shown = all ? items : items.slice(0, CARDS_SHOWN);
  return (
    <section className="inf-needs" aria-label="Needs you" data-testid={testId}>
      <div className="inf-needs-cards">
        {shown.map((it) => (
          <AttentionCard key={it.id} item={it} projectId={projectId} actor={actor} orgId={orgId ?? undefined}
                         onRefresh={onRefresh} onDone={onRefresh} />
        ))}
      </div>
      {items.length > CARDS_SHOWN && !all ? (
        <button type="button" className="inf-link" data-testid={`${testId}-more`} onClick={() => setAll(true)}>
          +{items.length - CARDS_SHOWN} more
        </button>
      ) : null}
    </section>
  );
}

/** `Some data missing, <names>`, one line; the tooltip names what failed (§4.2). */
export function PartialLine({ partial, error, testId }: { partial: string[]; error?: string | null; testId: string }) {
  if (error) {
    return <div className="inf-partial" role="status" data-testid={`${testId}-unreachable`} title={error}>Can&apos;t reach BOSS</div>;
  }
  if (!partial.length) return null;
  return (
    <div className="inf-partial" role="status" data-testid={testId} title={`Not answered: ${partial.join(", ")}`}>
      Some data missing, <span data-copy="data">{partial.join(", ")}</span>
    </div>
  );
}

/** A section head: 11 px caps label, count, spacer, actions. Sections never disappear: an
 *  empty one says so in one line under its head. */
export function ZoneHead({ id, label, count, children, tip }: { id: string; label: string; count?: number; tip?: string; children?: React.ReactNode }) {
  return (
    <header className="ag-live-head inf-zone-head">
      <h3 id={id} className="ag-zone-label" title={tip}>{label}</h3>
      {count !== undefined && count > 0 ? <span className="ag-zone-count" data-testid={`${id}-count`}>{count}</span> : null}
      <span className="ag-spacer" />
      {children}
    </header>
  );
}

export function Empty({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return <div className="inf-empty" data-testid={testId}>{children}</div>;
}

export function Skeleton({ testId }: { testId: string }) {
  return (
    <div className="ag-live-skeleton" aria-busy="true" data-testid={testId}>
      <span className="ag-sr">Loading</span>
      <div className="ag-live-skel-rows"><i /><i /><i /></div>
    </div>
  );
}

// ── focus trap shared by Drawer and Sheet (§7) ──────────────────────────────────────────────

function useDialog(onClose: () => void) {
  const ref = useRef<HTMLElement>(null);
  const closer = useRef(onClose);
  closer.current = onClose;
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const box = ref.current;
    box?.querySelector<HTMLElement>("[data-autofocus], button, a[href], input, select, textarea, [tabindex='0']")?.focus();
    const onKey = (e: KeyboardEvent) => {
      // A DIALOG ABOVE THIS ONE (the consent sheet, "Confirm it's you") owns the keys while the
      // focus is in it: Esc closes that one, never the sheet underneath it.
      const at = document.activeElement;
      if (ref.current && at && !ref.current.contains(at) && at.closest("[role='dialog']")) return;
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closer.current(); return; }
      if (e.key !== "Tab" || !ref.current) return;
      const els = [...ref.current.querySelectorAll<HTMLElement>("button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex='0']")];
      if (!els.length) return;
      const first = els[0];
      const last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      if (opener && opener.isConnected) opener.focus?.();
    };
  }, []);
  return ref;
}

const noSubscribe = () => () => {};
/** An overlay (Drawer, Sheet) renders at the end of <body>, never inside the tab. A `position: fixed`
 *  box is fixed to its nearest transformed / filtered ancestor, not the viewport: inside a panel
 *  that animates in (a transform), a drawer filled the panel, not the screen. The server and the
 *  hydrating render draw it in place (no mismatch); the next render moves it to <body>. */
function OnTop({ children }: { children: React.ReactElement }) {
  const client = useSyncExternalStore(noSubscribe, () => true, () => false);
  return client ? createPortal(children, document.body) : children;
}

/** A right drawer over the tab (full screen under 640 px). URL state, so Back closes it. */
export function Drawer({ label, testId, width = 440, expandable = false, onClose, head, children }: {
  label: string; testId: string; width?: 440 | 520 | 560 | 880; onClose: () => void; head: React.ReactNode; children: React.ReactNode;
  /** A drawer that reads better at a page's width (an app's health: its chart, traffic, incidents)
   *  offers Expand: the drawer fills the page until Collapse. */
  expandable?: boolean;
}) {
  const ref = useDialog(onClose);
  const [expanded, setExpanded] = React.useState(false);
  return (
    <OnTop>
    <div className="inf-drawer-back" data-expanded={expanded || undefined}
         onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <aside ref={ref as React.RefObject<HTMLElement>} className="inf-drawer" data-width={width}
             data-expanded={expanded || undefined} role="dialog"
             aria-modal="true" aria-label={label} data-testid={testId}>
        <header className="inf-drawer-head">
          {head}
          <span className="ag-spacer" />
          {expandable ? (
            <button type="button" className="inf-link inf-drawer-expand" aria-pressed={expanded} data-testid={`${testId}-expand`}
                    title={expanded ? "Back to the panel" : "Use the whole page"}
                    onClick={() => setExpanded((v) => !v)}>{expanded ? "Collapse" : "Expand"}</button>
          ) : null}
          <button type="button" className="ag-act-close" aria-label="Close" data-testid={`${testId}-close`} onClick={onClose}>×</button>
        </header>
        <div className="inf-drawer-body">{children}</div>
      </aside>
    </div>
    </OnTop>
  );
}

/** A drawer section: an 11 px caps head and its body. */
export function DrawerSection({ label, testId, children, actions }: { label: string; testId: string; children: React.ReactNode; actions?: React.ReactNode }) {
  const id = useId();
  return (
    <section className="inf-dsec" aria-labelledby={id} data-testid={testId}>
      <header className="inf-dsec-head"><h4 id={id}>{label}</h4><span className="ag-spacer" />{actions}</header>
      {children}
    </section>
  );
}

/** A centred sheet for a form (`+ Add › Host`, `+ Deploy`, `Retire…`). */
export function Sheet({ title, testId, onClose, children, wide = false }: { title: string; testId: string; onClose: () => void; children: React.ReactNode;
  /** A one-screen flow (Add a server) reads at a page's width, not a form's. */
  wide?: boolean }) {
  const ref = useDialog(onClose);
  return (
    <OnTop>
    <div className="inf-sheet-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section ref={ref as React.RefObject<HTMLElement>} className="inf-sheet" data-wide={wide || undefined} role="dialog" aria-modal="true"
               aria-label={title} data-testid={testId}>
        <header className="inf-sheet-head">
          <h2>{title}</h2>
          <button type="button" className="ag-act-close" aria-label="Close" onClick={onClose}>×</button>
        </header>
        {children}
      </section>
    </div>
    </OnTop>
  );
}

/** `Type <name> to confirm`, the press stays disabled until the name matches exactly. The
 *  server checks the name too (test_retiring_a_host_needs_the_name). */
export function TypedConfirm({ name, value, onChange, testId }: { name: string; value: string; onChange: (v: string) => void; testId: string }) {
  return (
    <label className="inf-field">
      <span>Type <b data-copy="data">{name}</b> to confirm</span>
      <input value={value} onChange={(e) => onChange(e.target.value)} data-testid={testId} autoComplete="off" spellCheck={false} />
    </label>
  );
}
export const confirmed = (name: string, typed: string) => typed.trim() === name && name.length > 0;

/** A `View as table` toggle every chart and strip carries (§7). */
export function TableToggle({ on, onToggle, testId }: { on: boolean; onToggle: () => void; testId: string }) {
  return (
    <button type="button" className="inf-link" aria-pressed={on} data-testid={testId} onClick={onToggle}>
      {on ? "View as chart" : "View as table"}
    </button>
  );
}

/** One polite live region per area: announces state words that change, at most once per 10 s;
 *  counts are never announced (§7). */
export function useAnnouncer(): [string, (text: string) => void] {
  const [text, setText] = useState("");
  const last = useRef(0);
  const say = (t: string) => {
    const now = performance.now();
    if (last.current && now - last.current < 10_000) return;
    last.current = now;
    setText(t);
  };
  return [text, say];
}
export function LiveRegion({ text }: { text: string }) {
  return <div className="ag-sr" aria-live="polite" role="status">{text}</div>;
}
