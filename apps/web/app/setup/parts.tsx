"use client";

/**
 * Small pieces every row shares: the row menu (⋯), the database-consent checkbox, the Ask BOSS
 * button, and the board's context (one view, replaced whole by every write).
 */
import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import { DotsThree, Sparkle } from "@phosphor-icons/react";

import { TOAST_EVENT } from "../../lib/api-write";
import { openDockAbout } from "../../lib/dock-subject";
import type { SetupView } from "../../lib/setup-types";
import type { SigninStatus } from "../../lib/identity/github-signin";

export type SetupCtx = {
  projectId: string;
  view: SetupView;
  /** Replace the board's state with the server's answer. */
  apply: (view: SetupView) => void;
  /** Rows a check is running on right now (the board shows `Checking…` for them). */
  checking: Set<string>;
  setChecking: (key: string, on: boolean) => void;
  /** "Sign in with GitHub" here (null: not offered by this API, or not read yet). */
  signin?: SigninStatus | null;
};

export const BoardContext = createContext<SetupCtx | null>(null);

export function useBoard(): SetupCtx {
  const ctx = useContext(BoardContext);
  if (!ctx) throw new Error("useBoard outside the Repo and models board");
  return ctx;
}

export function toast(message: string, type: "success" | "error" = "success"): void {
  try {
    window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type, message } }));
  } catch { /* no window */ }
}

export type MenuItem = { label: string; onSelect: () => void; danger?: boolean;
                         disabled?: boolean; testid?: string };

/** `⋯`: the row's secondary actions. Closes on outside click or Escape. */
export function RowMenu({ items, label, testid, open: openFromUrl = false }: {
  items: MenuItem[]; label: string; testid?: string; open?: boolean;
}) {
  const [open, setOpen] = useState(openFromUrl);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (openFromUrl) setOpen(true); }, [openFromUrl]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div className="su-menu" ref={ref}>
      <button type="button" className="su-icon-btn" aria-label={label} aria-haspopup="menu"
              aria-expanded={open} data-testid={testid} onClick={() => setOpen((v) => !v)}>
        <DotsThree size={18} weight="bold" />
      </button>
      {open ? (
        <div className="su-menu-list" role="menu">
          {items.map((item) => (
            <button key={item.label} type="button" role="menuitem" disabled={item.disabled}
                    className={item.danger ? "su-menu-danger" : undefined}
                    data-testid={item.testid}
                    onClick={() => { setOpen(false); item.onSelect(); }}>
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const CONSENT_TIP = "No secret store on this server. Encrypted in Postgres.";
/** What a press waiting on the consent box says: the REASON, visibly (not the box's own label
 *  again); the tooltip keeps the detail. */
export const CONSENT_REFUSAL = { state: "needs_consent", headline: "Tick the box to store it",
                                 fix: `Tick Store in BOSS database. ${CONSENT_TIP}` };

/** `☐ Store in BOSS database`, asked once, before submit, when there is no secret store. */
export function Consent({ checked, onChange }: { checked: boolean;
                                                 onChange: (v: boolean) => void }) {
  const tip = CONSENT_TIP;
  return (
    <label className="su-consent" title={tip} data-tip={tip} data-testid="su-consent">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      Store in BOSS database
    </label>
  );
}

/** Ask BOSS, only on a failing row, and only when a model works (else it could not answer). */
export function AskBoss({ row, pillLabel, tip }: { row: string; pillLabel: string; tip: string }) {
  const { view } = useBoard();
  if (!view.models.some((m) => m.pill.tone === "green")) return null;
  return (
    <button type="button" className="su-ask" title="Ask BOSS about this problem."
            data-tip="Ask BOSS about this problem." data-testid={`su-ask-${row}`}
            onClick={() => openDockAbout({
              subject: null, role: null, fresh: true,
              question: `Setting up BOSS: ${row} shows "${pillLabel}", ${tip} What do I do?`,
            })}>
      <Sparkle size={14} aria-hidden="true" />Ask BOSS
    </button>
  );
}
