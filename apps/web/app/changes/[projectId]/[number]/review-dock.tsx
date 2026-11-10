"use client";

/**
 * DOCKABLE PANES (BOSSAPP-954): the tree and the agent each docked, on a rail, or open as a
 * drawer over the code (lib/review-layout.ts is the state). [ ] \ toggle them and focus mode;
 * Full screen is focus mode plus the browser's fullscreen (Esc leaves both). Remembered per person
 * and per page type.
 */
import React, { createContext, useCallback, useContext, useEffect, useReducer, useRef, useState } from "react";
import { ArrowsIn, ArrowsOut, CaretDoubleLeft, CaretDoubleRight, PushPin, X } from "@phosphor-icons/react";

import { useIdentity } from "../../../../components/identity/identity-provider";
import {
  columns, DEFAULT_LAYOUT, effective, keyAction, loadLayout, reduce, saveLayout,
  type Action, type Layout, type Pane, type PaneState,
} from "../../../../lib/review-layout";

type Ctx = {
  layout: Layout; dispatch: (a: Action) => void; viewport: number;
  eff: { tree: PaneState | "sheet"; agent: PaneState | "sheet" };
  fullScreen: () => void; isFull: boolean;
};
const LayoutCtx = createContext<Ctx | null>(null);

export function useReviewLayout(): Ctx {
  const v = useContext(LayoutCtx);
  if (v) return v;
  // Outside the provider (a unit test of one pane): everything docked, nothing remembered.
  return { layout: DEFAULT_LAYOUT, dispatch: () => undefined, viewport: 1440, eff: { tree: "docked", agent: "docked" },
           fullScreen: () => undefined, isFull: false };
}

export function ReviewLayoutProvider({ page, children }: { page: string; children: React.ReactNode }) {
  const { me } = useIdentity();
  const person = String((me as { principal_id?: string } | null)?.principal_id ?? "");
  const [layout, dispatch] = useReducer(reduce, DEFAULT_LAYOUT);
  const loaded = useRef(false);
  useEffect(() => {
    dispatch({ type: "load", layout: loadLayout(person, page) });
    loaded.current = true;
  }, [person, page]);
  useEffect(() => { if (loaded.current) saveLayout(person, page, layout); }, [layout, person, page]);
  const [viewport, setViewport] = useState(1440);
  useEffect(() => {
    const on = () => setViewport(window.innerWidth);
    on();
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Escape") { ["tree", "agent"].forEach((p) => dispatch({ type: "close", pane: p as Pane })); return; }
      const a = keyAction(e.key, typing);
      if (a) { e.preventDefault(); dispatch(a); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const [isFull, setFull] = useState(false);
  useEffect(() => {
    const on = () => {
      const full = !!document.fullscreenElement;
      setFull(full);
      if (!full) dispatch({ type: "focus", on: false });
    };
    document.addEventListener("fullscreenchange", on);
    return () => document.removeEventListener("fullscreenchange", on);
  }, []);
  const fullScreen = useCallback(() => {
    if (document.fullscreenElement) { void document.exitFullscreen?.().catch(() => null); dispatch({ type: "focus", on: false }); return; }
    dispatch({ type: "focus", on: true });
    void document.documentElement.requestFullscreen?.().catch(() => null);
  }, []);
  const cols = columns(layout, viewport);
  return (
    <LayoutCtx.Provider value={{ layout, dispatch, viewport, eff: effective(layout, viewport), fullScreen, isFull }}>
      <div className="gr-layout" data-focus={layout.focus || undefined} data-testid="gr-layout"
           style={{ ["--gr-tree-col" as string]: cols.tree, ["--gr-agent-col" as string]: cols.agent } as React.CSSProperties}>
        {children}
      </div>
    </LayoutCtx.Provider>
  );
}

/** One dockable pane. Docked: the column, an edge button to collapse it, a drag handle. Rail:
 *  its icon (opens the drawer) and a pin (docks it). Drawer: over the code, pin or close. */
export function DockPane({ pane, label, icon, children, testid }: {
  pane: Pane; label: string; icon: React.ReactNode; children: React.ReactNode; testid?: string;
}) {
  const { layout, dispatch, eff } = useReviewLayout();
  const state = eff[pane];
  const width = pane === "tree" ? layout.treeW : layout.agentW;
  const start = useRef<{ x: number; w: number } | null>(null);
  const sign = pane === "tree" ? 1 : -1;
  const resize = (w: number) => dispatch({ type: "resize", pane, width: w });
  const edge = pane === "tree" ? <CaretDoubleLeft size={12} aria-hidden /> : <CaretDoubleRight size={12} aria-hidden />;
  const key = pane === "tree" ? "[" : "]";
  if (state === "docked") {
    return (
      <div className="gr-dock" data-pane={pane} data-state="docked" data-testid={testid}>
        <button type="button" className="gr-edge" onClick={() => dispatch({ type: "toggle", pane })}
                aria-label={`Collapse the ${label.toLowerCase()} (${key})`} title={`Collapse (${key})`} data-testid={`gr-collapse-${pane}`}>{edge}</button>
        {children}
        <div className="gr-resize" role="separator" aria-orientation="vertical" aria-label={`Resize the ${label.toLowerCase()}`} tabIndex={0}
             aria-valuenow={width} data-testid={`gr-resize-${pane}`}
             onPointerDown={(e) => { start.current = { x: e.clientX, w: width }; (e.target as HTMLElement).setPointerCapture?.(e.pointerId); }}
             onPointerMove={(e) => { if (start.current) resize(start.current.w + sign * (e.clientX - start.current.x)); }}
             onPointerUp={() => { start.current = null; }} onPointerCancel={() => { start.current = null; }}
             onKeyDown={(e) => { if (e.key === "ArrowLeft") resize(width - 16 * sign); if (e.key === "ArrowRight") resize(width + 16 * sign); }} />
      </div>
    );
  }
  const drawer = state === "overlay" || state === "sheet";
  return (
    <div className="gr-dock" data-pane={pane} data-state={state} data-testid={testid}>
      {state !== "sheet" ? (
        <div className="gr-rail" aria-label={`${label}, collapsed`}>
          <button type="button" className="lr-icon" aria-label={`Open the ${label.toLowerCase()}`} aria-expanded={drawer}
                  onClick={() => dispatch(drawer ? { type: "close", pane } : { type: "open", pane })} data-testid={`gr-rail-${pane}`} title={label}>{icon}</button>
          <button type="button" className="lr-icon" aria-label={`Dock the ${label.toLowerCase()}`} onClick={() => dispatch({ type: "pin", pane })}
                  data-testid={`gr-pin-${pane}`} title={`Dock (${key})`}><PushPin size={13} aria-hidden /></button>
        </div>) : null}
      {drawer ? (
        <div className="gr-drawer" data-pane={pane} role="dialog" aria-label={label} style={{ width: `min(${width}px, calc(100vw - 32px))` }}
             data-testid={`gr-drawer-${pane}`}>
          <div className="gr-drawer-h"><b>{label}</b><span className="cs-spacer" />
            {state === "overlay" ? <button type="button" className="lr-icon" aria-label={`Dock the ${label.toLowerCase()}`}
                                           onClick={() => dispatch({ type: "pin", pane })} data-testid={`gr-drawer-pin-${pane}`}><PushPin size={13} aria-hidden /></button> : null}
            <button type="button" className="lr-icon" aria-label="Close" onClick={() => dispatch({ type: "close", pane })}
                    data-testid={`gr-drawer-close-${pane}`}><X size={13} aria-hidden /></button></div>
          {children}
        </div>) : null}
    </div>
  );
}

/** Full screen: focus mode plus the browser's fullscreen; Esc (the browser's) leaves both. */
export function FullScreenButton() {
  const { fullScreen, isFull, layout } = useReviewLayout();
  return (
    <button type="button" className="lr-btn" data-kind="ghost" onClick={fullScreen} aria-pressed={isFull || layout.focus}
            data-testid="gr-fullscreen" title="Full screen (focus mode; Esc to leave)">
      {isFull ? <ArrowsIn size={13} aria-hidden /> : <ArrowsOut size={13} aria-hidden />}{isFull ? "Exit full screen" : "Full screen"}</button>
  );
}
