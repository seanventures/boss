"use client";

/**
 * The Loadout's small popovers: a `+ Add ▾` menu, a chip's `⋯` menu, a picker. Anchored under
 * the button that opened it; a bottom sheet under 720px (agent-world.css `.ld-pop`). Esc or a
 * click outside closes it, and focus returns to the button.
 */
import React, { useEffect, useRef, useState } from "react";

export function Pop({ label, tip, testId, disabled, className, trigger, children, align = "left" }: {
  label: string; tip?: string; testId: string; disabled?: boolean; className?: string;
  /** What the button shows; `label` is its accessible name. */
  trigger?: React.ReactNode;
  children: (close: () => void) => React.ReactNode;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const close = () => { setOpen(false); button.current?.focus({ preventScroll: true }); };
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); close(); } };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps -- close is stable enough
  return (
    <span className="ld-pop-wrap" ref={wrap}>
      <button ref={button} type="button" className={className ?? "kit-btn kit-btn-sm"} aria-haspopup="menu"
              aria-expanded={open} aria-label={label} data-tip={tip} disabled={disabled} data-testid={testId}
              onClick={() => setOpen((o) => !o)}>
        {trigger ?? label}
      </button>
      {open ? (
        <span className="ld-pop kit-pop kit-menu" role="menu" data-align={align} data-testid={`${testId}-menu`}>
          {children(close)}
        </span>
      ) : null}
    </span>
  );
}
