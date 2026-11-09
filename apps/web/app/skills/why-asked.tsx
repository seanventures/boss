"use client";

/**
 * `Why asked ↗` on an approval a Kit tool raised (skills-mcp spec §5.7): an agent's call to a
 * skill or MCP tool set to Ask (or above the floor) opens the ability it belongs to, where the
 * tool's Allow, Ask, Off lives.
 */
import React from "react";

import { setUpHref, useKitAbilities } from "../../lib/use-kit-abilities";

export function WhyAsked({ recommendation, projectId }: { recommendation: Record<string, unknown>; projectId: string }) {
  const kind = String(recommendation.kind ?? "");
  const abilities = useKitAbilities(kind === "skill" || kind === "mcp" ? projectId : null);
  if (!abilities) return null;
  const skill = String(recommendation.skill_id ?? "");
  const server = String(recommendation.server_id ?? "");
  const ability = abilities.find((a) => (kind === "skill"
    ? a.tools.some((t) => t.ref === `skill:${skill}`)
    : a.mcp.some((m) => m.server_id === server)));
  if (!ability) return null;
  return (
    <a className="kit-link" href={setUpHref(ability.key)} title={`${ability.label}, its tools' Allow, Ask, Off`}
       data-testid="decision-why-asked">Why asked ↗</a>
  );
}
