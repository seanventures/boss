"use client";

/**
 * THE GRANT CARD (BOSSAPP-848): an agent asked for MCP tools it does not hold.
 *
 *   pm asks for 2 tools on linear
 *   "I need to file the bug I found in the checkout flow."
 *   create_issue   Writes · Ask each call
 *   read_issue     Reads  · Allow
 *   On approve: pm holds exactly these, pinned to the definitions shown; a tool that changes
 *   later is taken back until it is asked for again.
 *
 * Drawn inside its Approvals row for `recommendation.action === "grant_tool_access"`; the answer
 * goes through the inbox's own form, and the engine refuses an approval when a tool changed
 * since the card was drawn.
 */
import React from "react";

import { agentName } from "./kit-copy";

export const GRANT_ACTION = "grant_tool_access";

type GrantTool = { tool: string; tier: number; policy: "allow" | "ask"; description?: string; quarantined?: boolean };
export type GrantCard = { server: string; role_key: string; reason?: string; tools: GrantTool[] };

export function isGrantAccessDecision(decision: { recommendation?: Record<string, unknown> }): boolean {
  return decision.recommendation?.action === GRANT_ACTION;
}

export function grantOf(rec: Record<string, unknown>): GrantCard | null {
  if (rec.action !== GRANT_ACTION || !Array.isArray(rec.tools)) return null;
  const tools = (rec.tools as unknown[]).filter((t): t is GrantTool =>
    Boolean(t) && typeof t === "object" && typeof (t as GrantTool).tool === "string");
  return { server: String(rec.server ?? ""), role_key: String(rec.role_key ?? ""),
           reason: typeof rec.reason === "string" ? rec.reason : undefined, tools };
}

const TIER_WORDS = ["Reads", "Reads", "Writes", "Public"];

export function GrantAccessCard({ card }: { card: GrantCard }) {
  const n = card.tools.length;
  return (
    <div className="decision-grant-card" data-testid="decision-grant-card">
      <p className="decision-grant-head">
        <strong>{agentName(card.role_key)}</strong> asks for {n} tool{n === 1 ? "" : "s"} on <strong data-user-text>{card.server}</strong>
      </p>
      {card.reason ? <blockquote className="decision-grant-reason" data-user-text>{card.reason}</blockquote> : null}
      <ul className="decision-grant-tools">
        {card.tools.map((t) => (
          <li key={t.tool} data-testid={`decision-grant-tool-${t.tool}`}>
            <code data-tip={t.description || t.tool}>{t.tool}</code>
            <span className="kit-chip" data-tier={t.tier}>{TIER_WORDS[t.tier] ?? `Tier ${t.tier}`}</span>
            <span className="kit-chip">{t.policy === "allow" ? "Allow" : "Ask each call"}</span>
            {t.quarantined ? <span className="kit-chip" data-tip="Its definition changed and waits for review">Changed</span> : null}
          </li>
        ))}
      </ul>
      <p className="decision-grant-effect">
        On approve, {agentName(card.role_key)} holds exactly these, pinned to the definitions shown. A tool that
        changes later is taken back until it is asked for again.
      </p>
    </div>
  );
}
