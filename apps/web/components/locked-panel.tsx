"use client";

/**
 * A PANEL THE VIEWER MAY NOT READ IS LOCKED, NEVER AN ERROR (round 2, R2-WEB). When the read gate
 * (lib/identity/read-gates.ts) refuses a panel's read, the panel renders this: a lock, a short
 * label, and why in the words useCan gives ("Needs “Read hosts” · ask an Admin"), in the tooltip
 * and to a screen reader. Inline (a row's body) or as a card.
 */
import React from "react";
import { LockSimple } from "@phosphor-icons/react";

export function LockedPanel({ label = "Locked", reason, inline = false, testId }: {
  label?: string; reason: string; inline?: boolean; testId?: string;
}) {
  const Tag = inline ? "span" : "div";
  return (
    <Tag className="locked-panel" data-locked="" data-testid={testId ?? "locked-panel"} title={reason}
         role="note" aria-label={`${label}. ${reason}`}
         style={{ display: inline ? "inline-flex" : "flex", alignItems: "center", gap: 6, color: "var(--faint)",
                  fontSize: 13, ...(inline ? {} : { padding: "10px 12px", border: "1px dashed var(--border)", borderRadius: 8 }) }}>
      <LockSimple size={14} weight="bold" aria-hidden />
      <span>{label}</span>
      <span className="sr-only">{reason}</span>
    </Tag>
  );
}

/** The reason a locked read carries (lib/identity/read-gates.ts lockedResponse), or a fallback. */
export async function lockedReason(response: Response): Promise<string> {
  try {
    const body = (await response.clone().json()) as { reason?: unknown };
    return typeof body.reason === "string" && body.reason ? body.reason : "You can't read this · ask an Admin";
  } catch {
    return "You can't read this · ask an Admin";
  }
}

/** A read that failed, said for what it was (P1-11/12/13: "can't reach" only when nothing answered).
 *  `status` 0: nothing answered → "Can't reach BOSS API". 401/403: the viewer may not read it →
 *  locked. Anything else: the API answered with a problem → "Couldn't load". */
export type ReadProblem = { text: string; status: number };

export function readFailureLabel(status: number): "locked" | "unreachable" | "failed" {
  if (status === 401 || status === 403) return "locked";
  return status === 0 || status === 502 || status === 503 || status === 504 ? "unreachable" : "failed";
}

export function ReadFailure({ problem, onRetry, testId, retryTestId, className = "kit-bar-err" }: {
  problem: ReadProblem; onRetry?: () => void; testId?: string; retryTestId?: string; className?: string;
}) {
  const kind = readFailureLabel(problem.status);
  if (kind === "locked") return <LockedPanel reason={problem.text || "You can't read this · ask an Admin"} testId={testId} />;
  return (
    <div className={className} role="alert" data-testid={testId}>
      <span data-tip={problem.text || undefined} title={problem.text || undefined}>
        {kind === "unreachable" ? "Can't reach BOSS API" : "Couldn't load"}
      </span>
      {onRetry ? <button type="button" className="kit-link" data-testid={retryTestId} onClick={onRetry}>Retry</button> : null}
    </div>
  );
}
