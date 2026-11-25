"use client";

/**
 * THE CHANGE'S OWN PREVIEW, BESIDE ITS MERGE (operator, 2026-11-19: "can't find a change's
 * preview to verify it before merging"). A Work row and its drawer's top card show the preview of
 * the build whose merge is open — "Open preview" while it runs, "Recreate preview" once it is
 * stopped or asleep — through the Your app card's one open (useOpenApp: startPreviewAction with
 * consent and step-up, then it opens in a new tab).
 */
import React from "react";
import { ArrowClockwise, ArrowSquareOut, Play } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { opensDirectly, type YourApp, type YourAppState } from "../../lib/your-app";
import { previewWebHref } from "../../lib/browser-links";
import { useOpenApp } from "./your-app";

/** A build page's preview (its Outputs row) as the Your app card's app: never had one → Start
 *  preview; stopped, expired or failed → Recreate preview; running or starting → Open preview. */
export function buildPreviewApp(runId: string, p: { id: string; status: string } | null | undefined, name = ""): YourApp {
  const st = String(p?.status ?? "");
  const state: YourAppState = !p ? "none" : st === "running" || st === "verified" ? "running" : st === "starting" ? "starting"
    : st === "expired" ? "asleep" : st === "stopped" ? "stopped" : "failed";
  return {
    run_id: runId, name, title: "", version: "change", version_label: "", version_words: "", pr_number: null, pr_url: "",
    preview_id: p?.id ?? "", state, words: "", error: "", live_url: "",
    href: p && (state === "running" || state === "starting") ? previewWebHref(p.id) : "",
    recreate: state === "asleep" || state === "stopped" || state === "failed",
  };
}

export function previewWord(app: YourApp, busy: boolean): string {
  if (busy) return "Starting…";
  if (opensDirectly(app)) return "Open preview";
  return app.recreate ? "Recreate preview" : "Start preview";
}

export function WorkPreviewButton({ app, projectId, actor, testid, onStarted, disabled, why, className = "px-btn" }: {
  app: YourApp; projectId: string; actor?: string; testid: string; onStarted?: () => void; className?: string;
  /** It cannot start yet (no branch to preview), and why. */
  disabled?: boolean; why?: string;
}) {
  const o = useOpenApp(app, actor, onStarted);
  const direct = opensDirectly(app);
  return (
    <GatedButton permission="preview:manage" projectId={projectId} className={className} data-size="s" data-testid={testid}
                 data-state={app.state} disabled={o.busy || disabled} title={o.error ? `Couldn't start it: ${o.error.slice(0, 140)}` : why || app.words || undefined}
                 onClick={(e: React.MouseEvent) => { e.stopPropagation(); void o.open(); }}>
      {direct ? <ArrowSquareOut size={12} weight="bold" aria-hidden /> : app.recreate ? <ArrowClockwise size={12} weight="bold" aria-hidden /> : <Play size={12} weight="fill" aria-hidden />}
      {" "}{previewWord(app, o.busy)}
    </GatedButton>
  );
}
