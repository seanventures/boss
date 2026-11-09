"use client";

/** Used in (spec §5.3 4): the workflow steps that use this ability, each a link into the Studio. */
import React from "react";

import type { Ability } from "../../lib/kit-types";

/** One row per step: the API lists a step once per part of the ability it uses (`via`), so a
 *  step scoped to both of Open pull requests' skills would otherwise show twice. */
export function stepsOf(used: Ability["used_in"]): Ability["used_in"] {
  const seen = new Set<string>();
  return used.filter((u) => {
    const key = `${u.workflow_key}:${u.step_id}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function UsedInSection({ ability }: { ability: Ability }) {
  const steps = stepsOf(ability.used_in);
  if (steps.length === 0) return <div className="kit-faint" data-testid="kit-used-in-empty">Not in any workflow</div>;
  return (
    <ul className="kit-used" data-testid="kit-used-in">
      {steps.map((u) => (
        <li key={`${u.workflow_key}:${u.step_id}`}>
          <a href={`?area=delivery_workflows&wf=${encodeURIComponent(u.workflow_key)}&step=${encodeURIComponent(u.step_id)}`}
             data-tip={`${u.workflow_name} › ${u.step_title}`}>
            <span data-user-text>{u.workflow_name}</span>, <span data-user-text>{u.step_title}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}
