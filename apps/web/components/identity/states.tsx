"use client";

/**
 * THE IDENTITY SCREENS' TWO NON-HAPPY STATES, one definition each:
 *
 *   - ErrorState: a read failed. It says so in words, with Try again — never an empty list that
 *     pretends there is nothing, and never a raw code (lib/identity/words.ts).
 *   - EmptyState: there really is nothing. It says what this is for and offers the next step.
 */
import { useConsoleRefresh } from "../../app/use-console-refresh";
import React, { type ReactNode } from "react";
import { ArrowClockwise, WarningCircle } from "@phosphor-icons/react";

import { loadFailureWords } from "../../lib/identity/words";

/** A failed server read, as a page or a panel receives it (lib/identity/server.ts ServerRead). */
export type ReadFailure = { status: number; code: string; permission?: string | null };

export function ErrorState({ what, failure, compact = false, testId }: {
  /** What was being loaded, lower case: "people", "this group", "service accounts". */
  what: string; failure: ReadFailure; compact?: boolean; testId?: string;
}) {
  const refresh = useConsoleRefresh();
  const w = loadFailureWords(what, failure.status, failure.code, failure.permission);
  const retry = failure.status !== 403 && failure.status !== 404;
  return (
    <div className={`idn-state err${compact ? " compact" : ""}`} role="alert" data-testid={testId ?? "error-state"} data-status={failure.status}>
      <WarningCircle size={compact ? 18 : 26} weight="duotone" aria-hidden />
      <div className="idn-state-t">
        <b>{w.title}</b>
        <span data-copy="prose">{w.body}</span>
      </div>
      {failure.status === 401
        ? <a className="idn-btn sm" href="/login">Sign in</a>
        : retry ? <button type="button" className="idn-btn sm" onClick={() => refresh("now")}><ArrowClockwise size={13} aria-hidden /> Try again</button> : null}
    </div>
  );
}

export function EmptyState({ icon, title, children, action, compact = false, testId }: {
  icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode; compact?: boolean; testId?: string;
}) {
  return (
    <div className={`idn-state${compact ? " compact" : ""}`} data-testid={testId ?? "empty-state"}>
      {icon ? <span className="idn-state-i" aria-hidden>{icon}</span> : null}
      <div className="idn-state-t">
        <b>{title}</b>
        {children ? <span data-copy="prose">{children}</span> : null}
      </div>
      {action ? <div className="idn-state-a">{action}</div> : null}
    </div>
  );
}

/**
 * A page whose access could not be checked (the identity API is unreachable): calm, says what
 * happened, offers Retry — and the page itself is not drawn (fail closed).
 */
export function AccessUnchecked({ page }: { page: string }) {
  const refresh = useConsoleRefresh();
  return (
    <div className="idn-denied" data-testid="access-unchecked">
      <div className="idn-denied-card">
        <div className="idn-lock" aria-hidden><WarningCircle size={24} weight="duotone" /></div>
        <h1>Can't check your access right now</h1>
        <p className="idn-why-line idn-dim" data-copy="prose">BOSS could not reach its identity service, so {page} stays closed until it can.</p>
        <div className="idn-acts">
          <button type="button" className="idn-btn primary" onClick={() => refresh("now")}><ArrowClockwise size={13} aria-hidden /> Retry</button>
        </div>
      </div>
    </div>
  );
}
