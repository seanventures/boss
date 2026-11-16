"use client";

/**
 * Installed coding adapters (BOSSAPP-852). One row per adapter the server installed, and whether
 * skills and MCP config are delivered to it. Hidden when none are installed. The words are the
 * server's (`harness_adapters` on GET /v1/setup).
 */
import React from "react";

import type { InstalledAdapter } from "../../lib/setup-types";
import { SectionLabel } from "../section-label";
import { useBoard } from "./parts";

function delivered(kind: InstalledAdapter["kit"]["mcp_config"], yes: string, no: string): string {
  return kind === "delivered" ? yes : no;
}

export function HarnessAdaptersRow() {
  const list = useBoard().view.harness_adapters;
  if (!list?.length) return null;
  return (
    <section className="su-row" data-testid="su-row-installed-adapters" aria-label="Installed adapters"
             id="su-installed-adapters">
      <div className="su-row-label">
        <SectionLabel as="span" tip="MCP servers and skills, handed over on each run.">Installed adapters</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        {list.map((adapter) => (
          <span key={adapter.id} className="su-line" data-testid={`installed-adapter-${adapter.id}`}>
            <span data-user-text>{adapter.label || adapter.name}</span>
            <span data-testid={`installed-adapter-${adapter.id}-mcp`}>
              {delivered(adapter.kit.mcp_config, "MCP config delivered", "MCP config not delivered")}
            </span>
            <span data-testid={`installed-adapter-${adapter.id}-skills`}>
              {delivered(adapter.kit.skills, "Skills delivered", "Skills not delivered")}
            </span>
          </span>
        ))}
      </div>
    </section>
  );
}
