"use client";

/**
 * GitHubWait — "Waiting for GitHub · your work is saved here and will push when GitHub is back ·
 * next try 14:32" (BOSSAPP-922). The one drawing of a build waiting for GitHub, on every surface
 * (the build page, Project › Work, Agents at work, Needs you). Calm blue, never red: the work is
 * done and saved; only the push waits. Words from lib/github-wait.ts.
 */
import React from "react";

import { waitingWords } from "../lib/github-wait";
import "./github-wait.css";

export function GitHubWait({ nextAt, compact = false, testId = "github-wait", now }: {
  nextAt?: string | null; compact?: boolean; testId?: string; now?: number;
}) {
  const w = waitingWords(nextAt, now ?? Date.now());
  return (
    <span className="gw" data-testid={testId} data-compact={compact || undefined} role="status"
          title={w.words}>
      <span className="gw-dot" aria-hidden="true" />
      <span className="gw-label">{w.label}</span>
      <span className="gw-detail">· {w.detail}</span>
      {w.next ? <span className="gw-next" data-testid={`${testId}-next`}>· {w.next}</span> : null}
    </span>
  );
}
