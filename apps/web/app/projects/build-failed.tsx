"use client";

/**
 * A FAILED BUILD SAYS SO (engine work.failed_build): the two answers every surface that shows
 * one offers — Retry (the build's own resume: POST /v1/code-tasks/{id}/retry, a new attempt on
 * the same branch with the failure carried forward, the Code Tasks screen's Retry) and See what
 * happened (the build's page). One component, so the header, Needs you and the Running tile
 * never offer different buttons for the same failure (recipe-box 2026-11-18: the page said
 * "Working" over a build that had failed, and offered nothing).
 */
import React, { useState } from "react";
import { ArrowClockwise, ArrowSquareOut } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { callAction } from "../../lib/call-action";
import { isPxDemo, navigateTo, pxHref } from "../../lib/project-experience";
import { retryBuildAction } from "../factory/floor-actions";
import { notifyChanged } from "../changed";

export function RetryBuildButton({ runId, projectId, actor, onDone, testid, size = "s" }: {
  runId: string; projectId: string; actor: string; onDone?: (said: string) => void; testid?: string; size?: "s" | "m";
}) {
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState("");
  const retry = async () => {
    if (busy) return;
    if (isPxDemo()) { setSaid("Sample mode — nothing was sent."); return; }
    setBusy(true);
    setSaid("");
    const form = new FormData();
    form.set("run_id", runId);
    form.set("project_id", projectId);
    form.set("actor", actor);
    try {
      const r = await callAction(retryBuildAction, undefined, "code_task:retry")(form);
      const words = r.ok ? (r.attempt ? `Attempt ${r.attempt} started` : "Retrying") : (r.error || "It didn't retry.");
      setSaid(words);
      if (r.ok) { notifyChanged("project.work"); onDone?.(words); }
    } catch (e) {
      setSaid(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <GatedButton permission="code_task:retry" projectId={projectId} className="px-btn" data-kind="primary" data-size={size}
                   data-testid={testid ?? "px-failed-retry"} disabled={busy} aria-busy={busy || undefined}
                   title="Try again from where it stopped: same branch, the failure carried forward"
                   onClick={(e) => { e.stopPropagation(); void retry(); }}>
        <ArrowClockwise size={12} aria-hidden /> {busy ? "Retrying…" : "Retry"}
      </GatedButton>
      {said ? <small className="px-faint" role="status" data-testid={`${testid ?? "px-failed-retry"}-said`}>{said}</small> : null}
    </>
  );
}

export function SeeWhatHappened({ href, testid }: { href: string; testid?: string }) {
  if (!href) return null;
  const to = pxHref(href);
  return (
    <a className="px-btn" data-size="s" href={to} data-testid={testid ?? "px-failed-see"} title="Open the build: its steps, its log and the error in full"
       onClick={(e) => {
         e.stopPropagation();
         if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
         e.preventDefault();
         navigateTo(to);
       }}>
      See what happened <ArrowSquareOut size={10} aria-hidden />
    </a>
  );
}
