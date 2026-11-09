"use client";

import { GatedButton } from "../../components/gated";
import React, { useState } from "react";

import { apiWrite } from "../../lib/api-write";
import type { CustomStep, Draft } from "../delivery-workflows-ui";

/**
 * Give an agent a skill: the one grant write the workflow screens make.
 *
 * ONE FUNCTION FOR EVERY GRANT BUTTON. The builder row below and the inspector's `Grant` row
 * both call this, so the body (and the signed-in actor it carries) cannot drift between them.
 */
export async function grantSkill({ projectId, actor, roleKey, skillId }: {
  projectId: string; actor: string; roleKey: string; skillId: string;
}): Promise<{ ok: true } | { ok: false; error: string }> {
  try {
    const r = await apiWrite("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "assign", project_id: projectId, role_key: roleKey,
                             skill_id: skillId, actor }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: String(body?.error ?? `HTTP ${r.status}`) };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

/**
 * Whether the agent named on a step is permitted to run it, said in the row.
 *
 * WHY IT IS ITS OWN COMPONENT. It owns a request, granting the skill, and putting that state
 * in the builder would mean one `busy` flag shared by every row.
 *
 * WHAT IT IS CHECKING. `skill_runtime` lets a step's captain call a skill on one of two
 * grounds: the skill's own `allowed_callers` names it, or the agent HOLDS the skill in
 * `role_skills`. The second is the one an operator controls, so it is the one offered here.
 * Silent when there is nothing to say, a row cluttered with green ticks teaches people to stop
 * reading it.
 */
export function CaptainCanRunIt({ projectId, actor, step, mine, holdings, onGranted }: {
  projectId: string;
  /** The signed-in actor the grant is made as (it was the literal 'executive_console'). */
  actor: string;
  step: Draft;
  mine?: CustomStep;
  holdings: Record<string, string[]>;
  onGranted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");

  const captain = String(step.captain ?? "").trim();
  const skillId = String(mine?.skill_id ?? "").trim();
  // Nothing to check: no agent named, or the step runs an MCP tool rather than a skill, a
  // connector's authority comes from the connection, not from a grant on the agent.
  if (!captain || !skillId) return null;
  if ((holdings[captain] ?? []).includes(skillId)) {
    return (
      <span className="dw-draft-holds" data-testid={`dw-holds-${step.id}`}>
        {captain} holds {skillId}
      </span>
    );
  }

  const grant = async () => {
    setBusy(true);
    setFailed("");
    const result = await grantSkill({ projectId, actor, roleKey: captain, skillId });
    setBusy(false);
    if (!result.ok) { setFailed(result.error); return; }
    onGranted();
  };

  return (
    <span className="dw-draft-grant" data-testid={`dw-grant-${step.id}`}>
      {captain} does not hold {skillId}, this step will be refused when it runs.
      <GatedButton permission="agent_ability:assign" type="button" className="linklike" disabled={busy}
              data-testid={`dw-grant-do-${step.id}`} onClick={() => void grant()}>
        {busy ? "giving…" : `Give ${skillId} to ${captain}`}
      </GatedButton>
      {failed ? <span className="dw-draft-forced"> {failed}</span> : null}
    </span>
  );
}
