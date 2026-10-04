"use client";

/**
 * The right drawer (a bottom sheet under 640px). Used for Connect GitHub and Add a model on the
 * board; in first-run mode the same forms render inline in their row instead.
 */
import React, { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "@phosphor-icons/react";

export function Drawer({ title, onClose, children, testid }: {
  title: string; onClose: () => void; children: ReactNode; testid?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  // Once per opening: focus moves in when the sheet opens, not on every keystroke inside it.
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") close.current(); };
    document.addEventListener("keydown", escape);
    ref.current?.querySelector<HTMLElement>("input, button, select")?.focus();
    return () => document.removeEventListener("keydown", escape);
  }, []);
  // On the page body, so a transformed ancestor in the console cannot clip the sheet.
  const target = typeof document !== "undefined" ? document.body : null;
  const sheet = (
    <div className="su-drawer-scrim" onClick={onClose} data-testid={`${testid ?? "su-drawer"}-scrim`}>
      <div ref={ref} className="su-drawer" role="dialog" aria-modal="true" aria-label={title}
           data-testid={testid ?? "su-drawer"} onClick={(event) => event.stopPropagation()}>
        <header className="su-drawer-head">
          <h2>{title}</h2>
          <button type="button" className="su-icon-btn" aria-label="Close" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <div className="su-drawer-body">{children}</div>
      </div>
    </div>
  );
  return target ? createPortal(sheet, target) : sheet;
}
