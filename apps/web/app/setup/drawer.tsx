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
  // Once per opening: focus goes to the first FIELD (not the Close button), Tab stays inside
  // the sheet, Escape closes it, and focus returns to whatever opened it.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const focusable = () => [...(ref.current?.querySelectorAll<HTMLElement>(
      "input, select, textarea, button, a[href]") ?? [])].filter((el) => !el.hasAttribute("disabled"));
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); close.current(); return; }
      if (event.key !== "Tab") return;
      const all = focusable();
      if (!all.length) return;
      const first = all[0];
      const last = all[all.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keys);
    (ref.current?.querySelector<HTMLElement>(
      ".su-drawer-body input, .su-drawer-body select, .su-drawer-body button")
      ?? focusable()[0])?.focus();
    return () => {
      document.removeEventListener("keydown", keys);
      if (opener && document.contains(opener)) opener.focus();
    };
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
