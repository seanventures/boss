"use client";

import React, { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** Context for a label without making every form row a paragraph. */
export function HelpTip({ label, text }: { label: string; text: string }) {
  const id = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  // Escape moves focus back here. That focus must not open the tip again.
  const suppressFocusOpen = useRef(false);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);

  const close = useCallback(() => {
    setPinned(false);
    setOpen(false);
  }, []);

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!buttonRef.current?.contains(event.target as Node)
          && !tipRef.current?.contains(event.target as Node)) close();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      suppressFocusOpen.current = true;
      close();
      buttonRef.current?.focus();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [close, open]);

  useLayoutEffect(() => {
    if (!open) { setPosition(null); return; }
    const place = () => {
      const button = buttonRef.current;
      const tip = tipRef.current;
      if (!button || !tip) return;
      const anchor = button.getBoundingClientRect();
      const bubble = tip.getBoundingClientRect();
      const width = Math.min(bubble.width, window.innerWidth - 24);
      const left = Math.max(12, Math.min(anchor.left, window.innerWidth - width - 12));
      const above = anchor.bottom + bubble.height + 12 > window.innerHeight
        && anchor.top > bubble.height + 12;
      const top = above ? Math.max(12, anchor.top - bubble.height - 8)
        : Math.min(anchor.bottom + 8, window.innerHeight - bubble.height - 12);
      setPosition({ top, left });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, text]);

  return <>
    <button
      ref={buttonRef}
      type="button"
      className="help-tip-trigger"
      aria-label={`Help: ${label}`}
      aria-expanded={open}
      aria-describedby={open ? id : undefined}
      onPointerEnter={(event) => { if (event.pointerType === "mouse") setOpen(true); }}
      onPointerLeave={(event) => { if (event.pointerType === "mouse" && !pinned) setOpen(false); }}
      onFocus={() => { if (!suppressFocusOpen.current) setOpen(true); }}
      onBlur={(event) => {
        suppressFocusOpen.current = false;
        if (!pinned && !tipRef.current?.contains(event.relatedTarget as Node)) setOpen(false);
      }}
      onClick={() => {
        if (pinned) close();
        else { setPinned(true); setOpen(true); }
      }}
    >?</button>
    {open && typeof document !== "undefined" ? createPortal(
      <span
        ref={tipRef}
        id={id}
        role="tooltip"
        className="help-tip-bubble"
        style={position ? { top: position.top, left: position.left } : { visibility: "hidden" }}
      >{text}</span>,
      document.body,
    ) : null}
  </>;
}
