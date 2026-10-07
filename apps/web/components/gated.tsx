"use client";

/**
 * PRE-GREY A CONTROL THE VIEWER CANNOT USE — the console-wide primitive for buttons that invoke a
 * server action through lib/call-action.ts (P1-18).
 *
 * A control whose action needs a permission the viewer lacks stays visible and focusable, reads
 * as disabled (aria-disabled), does nothing when pressed, and says why — in its tooltip and to a
 * screen reader — in the words useCan gives: Needs “Publish workflows” · ask an Admin. When
 * allowed it is exactly the caller's button (class, title, disabled untouched).
 *
 * The API still decides every call; this only mirrors it (components/identity/identity-provider).
 * tests/every-call-action-button-is-pre-greyed.test.ts fails when a file presses a callAction
 * with a permission that the same file does not gate through here (or useCan/CanButton/Can).
 */
import React, { type ReactNode } from "react";

import { useCan } from "./identity/identity-provider";
import type { Permission } from "../lib/identity/permissions.gen";

export type Gate = {
  allowed: boolean;
  /** Why it is refused ("" when allowed). */
  reason: string;
  /** Spread on the control: aria-disabled + the reason as its tooltip when refused, nothing when allowed. */
  props: { "aria-disabled"?: true; title?: string; "data-permission": Permission; "data-can": "yes" | "no" };
  /** The screen-reader reason, to render inside the control when refused. */
  sr: ReactNode;
  /** Wrap a handler: refused, it prevents the default (a form submit) and does nothing. */
  guard: <E extends { preventDefault: () => void }>(fn?: (e: E) => unknown) => (e: E) => void;
};

/** The gate for one permission (at the console's active project for a project-scoped one). */
export function useGate(permission: Permission, projectId?: string | null): Gate {
  const r = useCan(permission, projectId);
  const allowed = r.allowed;
  return {
    allowed,
    reason: allowed ? "" : r.reason,
    props: allowed
      ? { "data-permission": permission, "data-can": "yes" }
      : { "aria-disabled": true, title: r.reason, "data-permission": permission, "data-can": "no" },
    sr: allowed ? null : <span className="sr-only">{r.reason}</span>,
    guard: (fn) => (e) => {
      if (!allowed) { e.preventDefault(); return; }
      fn?.(e);
    },
  };
}

type GatedButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  permission: Permission;
  projectId?: string | null;
};

/**
 * A <button> gated on `permission`: the caller's className, type, title and disabled are kept
 * when allowed; refused, it is aria-disabled, its title is the reason, and a press (click or
 * form submit) does nothing.
 */
export function GatedButton({ permission, projectId, onClick, title, children, ...rest }: GatedButtonProps) {
  const gate = useGate(permission, projectId);
  return (
    <button
      type="button"
      {...rest}
      title={gate.allowed ? title : gate.reason}
      {...gate.props}
      onClick={gate.guard(onClick)}
    >
      {children}
      {gate.sr}
    </button>
  );
}
