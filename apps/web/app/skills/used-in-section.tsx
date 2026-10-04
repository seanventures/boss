"use client";

/** Used in (spec §5.3 4): the workflow steps that use this ability, each a link into the Studio. */
import React from "react";

import type { Ability } from "../../lib/kit-types";

export function UsedInSection({ ability }: { ability: Ability }) {
  if (ability.used_in.length === 0) return <div className="kit-faint" data-testid="kit-used-in-empty">Not in any workflow</div>;
  return (
    <ul className="kit-used" data-testid="kit-used-in">
      {ability.used_in.map((u) => (
        <li key={`${u.workflow_key}:${u.step_id}`}>
          <a href={`?area=delivery_workflows&wf=${encodeURIComponent(u.workflow_key)}&step=${encodeURIComponent(u.step_id)}`}
             data-tip={`${u.workflow_name} › ${u.step_title}`}>
            <span data-user-text>{u.workflow_name}</span> › <span data-user-text>{u.step_title}</span>
          </a>
        </li>
      ))}
    </ul>
  );
}
