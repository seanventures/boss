"use client";

import React, { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";

/**
 * One floating panel under a trigger, for the inspector's pickers and the save/start choices.
 *
 * PORTALLED AND FIXED. The inspector is a scrolling 380px drawer; a popover positioned inside it
 * is clipped by the drawer's own overflow. This places itself against the trigger's rectangle,
 * flips above when there is no room below, and closes on Esc or a press outside — the same
 * contract as HelpTip, so the console has one way a floating thing behaves.
 */
export function Popover({ anchor, open, onClose, children, label, width = 300, testId, role = "dialog" }: {
  anchor: RefObject<HTMLElement | null>;
  open: boolean;
  onClose(): void;
  children: ReactNode;
  label: string;
  width?: number;
  testId?: string;
  role?: "dialog" | "listbox" | "menu";
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

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
    const place = () => {
      const a = anchor.current?.getBoundingClientRect();
      const p = panel.current?.getBoundingClientRect();
      if (!a) return;
      const h = p?.height ?? 0;
      const vw = window.innerWidth || 1024;
      const vh = window.innerHeight || 768;
      const w = Math.min(width, vw - 24);
      const left = Math.max(12, Math.min(a.left, vw - w - 12));
      const below = a.bottom + 6;
      const top = below + h + 12 > vh && a.top > h + 12 ? a.top - h - 6 : below;
      setPos({ top, left });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, anchor, width]);

  if (!open || typeof document === "undefined") return null;
  return createPortal(
    <div ref={panel} className="wf-pop" role={role} aria-label={label} data-testid={testId}
         style={{ width, ...(pos ? { top: pos.top, left: pos.left } : { visibility: "hidden" }) }}>
      {children}
    </div>,
    document.body,
  );
}
