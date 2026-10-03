"use client";

import React, { createContext, useContext, type ReactNode } from "react";

/**
 * Why the drawer's controls cannot edit right now, or null when they can: an old version is
 * open read-only, or the JSON does not parse (the store refuses every visual edit until it does,
 * so a control that stayed live would take clicks and typing and silently do nothing).
 */
const LockReason = createContext<string | null>(null);

export const LockProvider = LockReason.Provider;

export function useLockReason(): string | null {
  return useContext(LockReason);
}

/**
 * The controls that edit, disabled together with the reason as their tooltip. Everything that
 * only explains or navigates — section labels, HelpTip `(i)`s, the drawer's × — sits OUTSIDE
 * one of these, so it keeps working while the editing is locked.
 */
export function Editable({ children, inline }: { children: ReactNode; inline?: boolean }) {
  const why = useLockReason();
  return (
    <fieldset className={inline ? "wf-editable wf-editable-inline" : "wf-editable"}
              disabled={why !== null} title={why ?? undefined} data-locked={why !== null || undefined}>
      {children}
    </fieldset>
  );
}
