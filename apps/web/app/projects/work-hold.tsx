"use client";

/**
 * PAUSE ALL / RESTORE (BOSSAPP-878), inside the Tree view's item panel. "Pause all" first shows
 * what it would do ("pauses 2 runs · holds 4 items" — a dry run, nothing changed), then holds the
 * item's whole tree. A held item says so, since when and why; "Restore" previews ("resumes 2
 * runs · leaves paused 1 run") and then puts the tree back exactly as the hold found it.
 */
import React, { useState } from "react";
import { PauseCircle, PlayCircle } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { holdTree, planWords, restoreHold, type HoldPlanMember, type WorkNode } from "../../lib/work-items";
import { Who } from "../who";

export function ItemHold({ projectId, item, demo, onChanged }: {
  projectId: string; item: WorkNode; demo: boolean; onChanged: () => void;
}) {
  const [plan, setPlan] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const hold = item.hold ?? null;
  const members = (r: { body?: unknown }) =>
    ((r.body as { hold?: { members?: HoldPlanMember[] } } | null)?.hold?.members ?? []);
  const preview = async () => {
    const r = hold ? await restoreHold(projectId, hold.id, true) : await holdTree(projectId, item.id, true);
    if (!r.ok) { setSaid(r.error); return; }
    setSaid(null);
    setPlan(planWords(members(r), Boolean(hold)));
  };
  const commit = async () => {
    const r = hold ? await restoreHold(projectId, hold.id, false) : await holdTree(projectId, item.id, false);
    setPlan(null);
    setSaid(r.ok ? (hold ? "Restored as it was." : "On hold. Nothing under it runs until you restore it.") : r.error);
    if (r.ok) onChanged();
  };
  const ownHold = hold && hold.root_id === item.id;
  return (
    <div className="wt-line" data-testid="wt-hold">
      {hold ? (
        <span className="wt-blocked" data-testid="wt-hold-banner">
          <PauseCircle size={12} aria-hidden /> On hold{hold.root_id !== item.id ? " with the tree above" : ""}
          {hold.reason ? ` — ${hold.reason}` : ""} · by <Who id={hold.created_by} face={false} />
        </span>
      ) : null}
      {!hold || ownHold ? (
        plan ? (
          <>
            <span className="wt-faint" data-testid="wt-hold-plan">{hold ? "Restore" : "Pause all"} {plan}.</span>
            <GatedButton permission="workflow_run:control" projectId={projectId} className="px-btn" disabled={demo}
                         onClick={() => void commit()} data-testid="wt-hold-confirm">Confirm</GatedButton>
            <button type="button" className="px-btn" onClick={() => setPlan(null)}>Cancel</button>
          </>
        ) : (
          <GatedButton permission="workflow_run:control" projectId={projectId} className="px-btn" disabled={demo}
                       onClick={() => void preview()} data-testid={hold ? "wt-restore" : "wt-pause-all"}
                       title={hold ? "Put the tree back exactly as the hold found it" : "Pause this item, everything under it, and their runs"}>
            {hold ? <><PlayCircle size={12} aria-hidden /> Restore</> : <><PauseCircle size={12} aria-hidden /> Pause all</>}
          </GatedButton>
        )
      ) : null}
      {said ? <span className="wt-said" role="status" data-testid="wt-hold-said">{said}</span> : null}
    </div>
  );
}
