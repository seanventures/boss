"use client";

/**
 * REVIEW UPDATE on a Marketplace install you changed. Unmodified installs update by themselves;
 * a modified one is never overwritten and asks here: take the new version keeping your changes
 * (only when they apply cleanly), take the new version only (your changes stay in the version
 * list), or not now. Runs already going keep the version they started on.
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useEffect, useState } from "react";

import { useCan } from "../../components/identity/identity-provider";
import { fetchSpec, updateInstalled } from "../../lib/workflow-api";
import type { InstalledAct, InstalledFrom } from "../../lib/workflow-editor-types";

export function UpdateReview({ projectId, workflowKey, definitionId, installed, blocked, onDone, onClose }: {
  projectId: string;
  workflowKey: string;
  definitionId: string;
  /** With the update's detail (the editor has it); without, it is read here. */
  installed?: InstalledFrom | null;
  blocked?: string | null;
  onDone: (after: InstalledFrom | null, act: InstalledAct) => void;
  onClose: () => void;
}) {
  const gate = useCan("workflow:install");
  blocked = blocked || (gate.allowed ? null : gate.reason);
  const [from, setFrom] = useState<InstalledFrom | null>(
    installed?.update?.changes ? installed : null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<InstalledAct | null>(null);
  useEffect(() => {
    if (from) return;
    let live = true;
    fetchSpec(projectId, workflowKey).then(
      (got) => { if (live) setFrom(got.meta.marketplace ?? null); },
      (e) => { if (live) setError(String((e as Error)?.message ?? e)); });
    return () => { live = false; };
  }, [from, projectId, workflowKey]);

  const update = from?.update ?? null;
  const act = async (which: InstalledAct) => {
    if (busy || blocked) return;
    setBusy(which);
    setError("");
    try {
      onDone(await updateInstalled(projectId, definitionId, which), which);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="wf-update" role="dialog" aria-label="Review update" data-testid="wf-update"
         onClick={(e) => e.stopPropagation()}
         onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <div className="wf-std-row">
        <strong>Update{update?.version ? ` v${update.version}` : ""}</strong>
        <span className="wf-std-hint">{from ? `${from.pack_name}, you changed this` : "…"}</span>
        <span className="wf-std-sp" />
        <button type="button" className="wf-link" onClick={onClose} data-testid="wf-update-close">Close</button>
      </div>
      {update?.changes?.length ? (
        <ul className="wf-std-diff" data-testid="wf-update-changes" data-user-text>
          {update.changes.map((c, i) => <li key={`${c.step_id ?? "s"}-${i}`}>{c.text}</li>)}
        </ul>
      ) : null}
      {update && update.applies_cleanly === false && update.conflicts?.length ? (
        <div className="wf-std-hint" data-testid="wf-update-clash">
          <span>Clashes with your changes:</span>{" "}
          <span data-user-text>{update.conflicts.slice(0, 3).join(" · ")}</span>
        </div>
      ) : null}
      {error ? <p className="wf-publish-error" role="alert" data-testid="wf-update-error">{error}</p> : null}
      {from && !update ? <p className="wf-std-hint">Nothing waiting.</p> : null}
      {update ? (
        <div className="wf-std-row">
          <button type="button" className="wf-btn wf-btn-primary" data-testid="wf-update-take-keep"
                  disabled={Boolean(blocked) || update.applies_cleanly === false || busy !== null}
                  title={blocked ?? (update.applies_cleanly === false ? "Your changes and the update touch the same things"
                    : "Your changes, on the new version")}
                  onClick={() => void act("take_keep")}>
            {busy === "take_keep" ? "Taking…" : "Take it, keep my changes"}
          </button>
          <button type="button" className="wf-btn" data-testid="wf-update-take"
                  disabled={Boolean(blocked) || busy !== null}
                  title={blocked ?? "Your changes stay in the version list"} onClick={() => void act("take")}>
            {busy === "take" ? "Taking…" : "Take the new one only"}
          </button>
          <button type="button" className="wf-btn wf-btn-ghost" data-testid="wf-update-not-now"
                  disabled={Boolean(blocked) || busy !== null} title={blocked ?? undefined}
                  onClick={() => void act("dismiss")}>Not now</button>
        </div>
      ) : null}
    </div>
  );
}
