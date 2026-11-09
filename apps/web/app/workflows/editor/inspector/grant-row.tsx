"use client";

import { GatedButton } from "../../../../components/gated";
import React, { useState } from "react";
import { Warning } from "@phosphor-icons/react";

import { TOAST_EVENT } from "../../../../lib/api-write";
import { grantSkill } from "../../captain-can-run-it";

/**
 * ` Lacks <skill> [Grant]`, one line per skill the named agent does not hold.
 *
 * The CaptainCanRunIt check as a row: the same grant write (`grantSkill`), said in three words
 * instead of a sentence. Which skills are lacking is the caller's, from `stepView`, so this row
 * and the node's warning badge always agree.
 */
export function GrantRow({ projectId, actor, roleKey, agentName, lacks, onGranted, labelOf = (id) => id }: {
  projectId: string;
  /** The signed-in member the grant is made as. Without one, Grant is offered but disabled. */
  actor?: string;
  roleKey: string;
  agentName: string;
  lacks: string[];
  /** Refresh what agents hold (the host's palette). Awaited before the row reports success, so
   *  `Lacks` goes because the agent now holds the skill, not because this row remembers a click. */
  onGranted(): Promise<void>;
  /** What the row calls a skill or server: the Kit's words ("Open a PR", "Library docs"). */
  labelOf?: (id: string) => string;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState<Record<string, string>>({});
  if (!lacks.length) return null;

  const grant = async (skillId: string) => {
    if (!actor || busy) return;
    setBusy(skillId);
    setFailed((f) => ({ ...f, [skillId]: "" }));
    const result = await grantSkill({ projectId, actor, roleKey, skillId });
    if (!result.ok) { setBusy(null); setFailed((f) => ({ ...f, [skillId]: result.error })); return; }
    await onGranted().catch(() => undefined);
    setBusy(null);
    try {
      window.dispatchEvent(new CustomEvent(TOAST_EVENT, {
        detail: { type: "success", message: `Granted ${labelOf(skillId)} to ${agentName}` },
      }));
    } catch { /* no window */ }
  };

  return (
    <div className="wf-grant" data-testid="wf-grant">
      {lacks.map((skill) => (
        <div key={skill} className="wf-grant-row">
          <Warning size={14} weight="bold" className="wf-amber" aria-hidden />
          <span className="wf-grant-text" title={`${agentName} lacks ${skill}`}>Lacks {labelOf(skill)}</span>
          {failed[skill] ? (
            <span className="wf-red wf-grant-fail" title={failed[skill]} role="alert">Not granted</span>
          ) : null}
          <GatedButton permission="agent_ability:assign" type="button" className="wf-btn wf-btn-sm" data-testid={`wf-grant-${skill}`}
                  disabled={!actor || busy !== null}
                  title={actor ? `Give ${labelOf(skill)} to ${agentName}` : "Sign in to grant skills"}
                  onClick={() => void grant(skill)}>
            {busy === skill ? "Granting…" : "Grant"}
          </GatedButton>
        </div>
      ))}
    </div>
  );
}
