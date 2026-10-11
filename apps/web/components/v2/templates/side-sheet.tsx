"use client";

/**
 * THE CONSOLE'S SIDE SHEET for the template screens (the gallery card, App ▸ Setup, a Kit
 * review): a panel from the right edge, over the whole console (a portal: a Factory panel is a
 * containing block for `position: fixed` and clipped sheets to its own height), full width at
 * 390 px. Esc or the scrim closes it; focus goes in on open and back to the opener on close.
 */
import React, { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export function SideSheet({ title, sub, onClose, children, foot, testId, wide = false, labelledTitle }: {
  title: ReactNode; sub?: ReactNode; onClose: () => void; children: ReactNode; foot?: ReactNode;
  testId?: string; wide?: boolean;
  /** A plain-text name for the dialog when `title` is not text. */
  labelledTitle?: string;
}) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  useEffect(() => { setHost(document.body); }, []);
  useEffect(() => {
    if (!host) return undefined;
    const opener = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("[data-autofocus], input, select, textarea, button:not(.tpl-x)")?.focus();
    const onKey = (e: KeyboardEvent) => {
      // The innermost sheet closes first: only the last one in the document answers Esc.
      if (e.key !== "Escape") return;
      const sheets = document.querySelectorAll(".tpl-sheet");
      if (sheets[sheets.length - 1] !== ref.current) return;
      e.preventDefault();
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); opener?.focus?.(); };
  }, [host, onClose]);
  const sheet = (
    <div className="tpl-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} className="tpl-sheet" data-wide={wide || undefined} role="dialog" aria-modal="true"
           aria-labelledby={labelledTitle ? undefined : titleId} aria-label={labelledTitle} data-testid={testId}>
        <header className="tpl-sheet-head">
          <h2 id={titleId}>{title}</h2>
          {sub ? <span className="tpl-sheet-sub" data-user-text>{sub}</span> : null}
          <button type="button" className="tpl-x" aria-label="Close" onClick={onClose}>×</button>
        </header>
        <div className="tpl-sheet-body">{children}</div>
        {foot ? <footer className="tpl-sheet-foot">{foot}</footer> : null}
      </div>
    </div>
  );
  if (!host) return null;
  return createPortal(sheet, host);
}

/** A value with a Copy button: what a setup guide hands out (redirect URI, webhook URL). */
export function CopyValue({ value, label, testId }: { value: string; label: string; testId?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard?.writeText(value).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }, () => undefined);
  };
  return (
    <span className="tpl-copy" data-testid={testId}>
      <code className="tpl-copy-v" title={value} data-user-text>{value}</code>
      <button type="button" className="tpl-copy-b" aria-label={`Copy ${label}`} title={`Copy ${label}`} onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
    </span>
  );
}
