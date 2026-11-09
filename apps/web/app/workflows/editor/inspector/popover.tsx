"use client";

import React, { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

import { hideTip } from "../../flow/tip-layer";

/** Room kept between a popover and the viewport's edge, and between it and its trigger. */
const EDGE = 12;
const GAP = 6;
/** Below this much room under the trigger, a popover flips above (if there is more room there). */
const MIN_BELOW = 200;

export type Placement = { top: number; left: number; width: number; maxHeight: number; above: boolean };

/**
 * Where a popover goes: UNDER its trigger, as wide as `width` (or the trigger, when
 * `matchAnchor` and the trigger is wider), always inside the viewport. It flips above only when
 * the room below cannot show `MIN_BELOW` (or all of it, if it is shorter) and there is more room
 * above; whichever side it takes, its height is capped to the room there and its content
 * scrolls, so it never runs off the screen or over the top bar to find space.
 */
export function placePopover(anchor: { top: number; bottom: number; left: number; width: number },
                             contentHeight: number, width: number, matchAnchor: boolean,
                             viewport: { width: number; height: number }): Placement {
  const w = Math.min(matchAnchor ? Math.max(width, anchor.width) : width, viewport.width - EDGE * 2);
  const left = Math.max(EDGE, Math.min(anchor.left, viewport.width - w - EDGE));
  const roomBelow = viewport.height - anchor.bottom - GAP - EDGE;
  const roomAbove = anchor.top - GAP - EDGE;
  const wanted = Math.min(contentHeight, MIN_BELOW);
  const above = roomBelow < wanted && roomAbove > roomBelow;
  const maxHeight = Math.max(0, above ? roomAbove : roomBelow);
  const h = Math.min(contentHeight, maxHeight);
  const top = above ? anchor.top - GAP - h : anchor.bottom + GAP;
  return { top, left, width: w, maxHeight, above };
}

/**
 * One floating panel under a trigger, for the inspector's pickers and the save/start choices.
 *
 * PORTALLED AND FIXED. The inspector is a scrolling 380px drawer; a popover positioned inside it
 * is clipped by the drawer's own overflow. This places itself against the trigger's rectangle
 * (`placePopover`), and closes on Esc or a press outside, the same contract as HelpTip, so the
 * console has one way a floating thing behaves.
 *
 * Its class is `wf-pop wf-pop-float`: the Library's in-card menus are also `.wf-pop`, absolute
 * and capped at 340px, and the float's rules must win over those wherever the two stylesheets
 * load. While it is open, its trigger is marked `data-pop-open`, the tooltip layer is told to
 * hide, and no tooltip shows for that trigger: a tip must never sit over the popover it opened.
 */
export function Popover({ anchor, open, onClose, children, label, width = 300, testId, role = "dialog",
                          matchAnchor = false }: {
  anchor: RefObject<HTMLElement | null>;
  open: boolean;
  onClose(): void;
  children: ReactNode;
  label: string;
  width?: number;
  testId?: string;
  role?: "dialog" | "listbox" | "menu";
  /** At least as wide as the trigger (a field-width picker). */
  matchAnchor?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<Placement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const placeRef = useRef(() => {});
  placeRef.current = () => {
    const a = anchor.current?.getBoundingClientRect();
    if (!a) return;
    // The content's own height (scrollHeight), not the box's: the box may already be capped.
    const next = placePopover(a, panel.current?.scrollHeight ?? 0, width, matchAnchor,
                              { width: window.innerWidth || 1024, height: window.innerHeight || 768 });
    setPos((prev) => (prev && prev.top === next.top && prev.left === next.left && prev.width === next.width
      && prev.maxHeight === next.maxHeight && prev.above === next.above ? prev : next));
  };

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (panel.current?.contains(target) || anchor.current?.contains(target)) return;
      closeRef.current();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Esc closes the popover only; the drawer behind it stays open.
      event.stopPropagation();
      closeRef.current();
      anchor.current?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open, anchor]);

  useLayoutEffect(() => {
    if (!open) { setPos(null); return; }
    const trigger = anchor.current;
    trigger?.setAttribute("data-pop-open", "true");
    hideTip();
    const place = () => placeRef.current();
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      trigger?.removeAttribute("data-pop-open");
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, anchor, width, matchAnchor]);

  // The content changed (a search narrowed the list): place again, so a popover shown above
  // its trigger stays against it. A no-op when nothing moved.
  useLayoutEffect(() => { if (open) placeRef.current(); });

  if (!open || typeof document === "undefined") return null;
  return createPortal(
    <div ref={panel} className="wf-pop wf-pop-float" role={role} aria-label={label} data-testid={testId}
         data-side={pos ? (pos.above ? "above" : "below") : undefined}
         style={pos ? { top: pos.top, left: pos.left, width: pos.width, maxHeight: pos.maxHeight }
           : { width, visibility: "hidden" }}>
      {children}
    </div>,
    document.body,
  );
}
