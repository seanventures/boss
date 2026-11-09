"use client";

/**
 * A built-in step's ability on its read-only `Uses:` row (skills-mcp spec §3.3): the ability
 * chip, red with `Set up ↗` when it is not set up, so the step that will fail says so.
 */
import React from "react";

import type { SpecStep } from "../../../../lib/workflow-editor-types";
import { CAPABILITY_ABILITY, notSetUp, setUpHref, useKitAbilities } from "../../../../lib/use-kit-abilities";

export function StepAbility({ step, projectId }: { step: SpecStep; projectId: string }) {
  const abilities = useKitAbilities(projectId);
  const capability = String((step.binding as { capability?: string } | undefined)?.capability ?? "");
  const key = CAPABILITY_ABILITY[capability];
  const ability = key ? abilities?.find((a) => a.key === key) : undefined;
  if (!ability) return null;
  if (!notSetUp(ability)) {
    return <span className="wf-chip wf-chip-ro" title={ability.tip} data-testid="wf-step-ability">{ability.label}</span>;
  }
  return (
    <>
      <span className="wf-chip" data-tone="red" title={`${ability.label}, Not set up`} data-testid="wf-step-ability">
        {ability.label}, Not set up
      </span>
      <a className="wf-chip" data-tone="red" href={setUpHref(ability.key)} title="Open this ability in Skills & MCP"
         data-testid="wf-step-ability-setup">Set up ↗</a>
    </>
  );
}
