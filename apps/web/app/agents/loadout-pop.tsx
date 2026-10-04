"use client";

/**
 * The Loadout's small popovers: a `+ Add ▾` menu, a chip's `⋯` menu, a picker. Anchored under
 * the button that opened it; a bottom sheet under 720px (agent-world.css `.ld-pop`).
 *
 * A MENU FOR THE KEYBOARD TOO (independent test, D4): opening it focuses its first item (or its
 * search box); ↑/↓ move between items, Home/End jump; Esc closes it and puts focus back on the
 * button; Tab leaves it closed. A click outside closes it. While it is open, and for a moment
 * after, the Passport does not take an Esc as "close the page" (overlay.markOverlayClosed).
 */
import React, { useEffect, useRef, useState } from "react";

import { markOverlayClosed } from "../skills/overlay";

const ITEMS = '[role="menuitem"]:not([disabled]), [role="menuitemcheckbox"]:not([disabled]), input:not([disabled])';

let openPops = 0;
/** True while any Loadout popover is open. */
export function popOpen(): boolean { return openPops > 0; }

export function Pop({ label, tip, testId, disabled, className, trigger, children, align = "left" }: {
  label: string; tip?: string; testId: string; disabled?: boolean; className?: string;
  /** What the button shows; `label` is its accessible name. */
  trigger?: React.ReactNode;
  children: (close: () => void) => React.ReactNode;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLSpanElement>(null);
  const menu = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const close = () => { setOpen(false); markOverlayClosed(); button.current?.focus({ preventScroll: true }); };
  useEffect(() => {
    if (!open) return;
    openPops += 1;
    const first = menu.current?.querySelector<HTMLElement>(ITEMS);
    first?.focus({ preventScroll: true });
    const away = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) { setOpen(false); markOverlayClosed(); }
    };
    document.addEventListener("mousedown", away);
    return () => { openPops -= 1; document.removeEventListener("mousedown", away); };
  }, [open]);
  const onKey = (e: React.KeyboardEvent) => {
    if (!open) return;
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key === "Tab") { setOpen(false); markOverlayClosed(); return; }
    const items = [...(menu.current?.querySelectorAll<HTMLElement>(ITEMS) ?? [])];
    if (!items.length) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const go = (i: number) => { e.preventDefault(); items[(i + items.length) % items.length].focus(); };
    if (e.key === "ArrowDown") go(at + 1);
    else if (e.key === "ArrowUp") go(at < 0 ? items.length - 1 : at - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(items.length - 1);
  };
  return (
    <span className="ld-pop-wrap" ref={wrap} onKeyDown={onKey}>
      <button ref={button} type="button" className={className ?? "kit-btn kit-btn-sm"} aria-haspopup="menu"
              aria-expanded={open} aria-label={label} data-tip={tip} disabled={disabled} data-testid={testId}
              onClick={() => (open ? close() : setOpen(true))}>
        {trigger ?? label}
      </button>
      {open ? (
        <span ref={menu} className="ld-pop kit-pop kit-menu" role="menu" aria-label={label} data-align={align}
              data-testid={`${testId}-menu`}>
          {children(close)}
        </span>
      ) : null}
    </span>
  );
}
