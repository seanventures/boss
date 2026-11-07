"use client";

/**
 * THE project pages' confirm sheet: a small centred sheet that says what will happen, in rows
 * (what, where, which commit, who approves…), with Cancel and one primary press. Used wherever
 * a project page asks "are you sure" (Redeploy, Remove a value) — never the browser's confirm().
 * A refusal from the press shows inside the sheet as an error (red, role=alert), never as green.
 */
import React, { useEffect, useId, useRef, useState } from "react";
import { WarningCircle, X } from "@phosphor-icons/react";

export type ConfirmRow = { label: string; value: React.ReactNode; mono?: boolean };

export function ConfirmSheet({ title, icon, rows, lede, press, pressKind = "primary", onConfirm, onClose, testid = "px-confirm" }: {
  title: string; icon?: React.ReactNode; rows: ConfirmRow[]; lede?: string; press: string;
  pressKind?: "primary" | "danger";
  /** Resolves to null when it worked (the sheet closes), else the refusal in words (shown red). */
  onConfirm: () => Promise<string | null>;
  onClose: () => void; testid?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeRef.current(); } };
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("keydown", onKey, true); opener?.focus?.({ preventScroll: true }); };
  }, []);
  const go = async () => {
    setBusy(true); setError("");
    let why: string | null;
    try { why = await onConfirm(); } catch (e) { why = e instanceof Error ? e.message : "It did not go through."; }
    setBusy(false);
    if (why) setError(why); else onClose();
  };
  return (
    <div className="px-sheet-wrap" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div ref={ref} className="px-sheet px-confirm" role="dialog" aria-modal="true" aria-labelledby={titleId} data-testid={testid}>
        <div className="px-sheet-h">{icon}<h2 id={titleId}>{title}</h2><span className="px-spacer" />
          <button type="button" className="px-icon-btn" aria-label="Close" title="Close (Esc)" onClick={onClose}><X size={14} aria-hidden /></button></div>
        <div className="px-confirm-b">
          {lede ? <p className="px-confirm-lede">{lede}</p> : null}
          <dl className="px-confirm-rows">
            {rows.map((r) => (
              <React.Fragment key={r.label}><dt>{r.label}</dt><dd className={r.mono ? "px-mono" : undefined} data-testid={`${testid}-${r.label.toLowerCase().replace(/\W+/g, "-")}`}>{r.value}</dd></React.Fragment>
            ))}
          </dl>
          {error ? <p className="px-confirm-err" role="alert" data-testid={`${testid}-error`}><WarningCircle size={14} weight="fill" aria-hidden />{error}</p> : null}
        </div>
        <div className="px-drawer-f px-confirm-f">
          <span className="px-spacer" />
          <button type="button" className="px-btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" className="px-btn" data-kind={pressKind} data-autofocus onClick={() => void go()} disabled={busy} data-testid={`${testid}-go`}>
            {busy ? "Working…" : press}</button>
        </div>
      </div>
    </div>
  );
}
