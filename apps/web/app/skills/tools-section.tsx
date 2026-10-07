"use client";

/**
 * Tools (spec §5.3 2): one row per skill or MCP tool behind the ability, with its risk word and
 * Allow · Ask · Off. A tool that changes the world (tier >= 2) shows Ask locked — the runtime
 * asks a person whatever is chosen here, so offering Allow would be a lie. An unreviewed server
 * puts a Review row on top: one press records who vouched for it.
 */
import { GatedButton } from "../../components/gated";
import React, { useState } from "react";
import { Lock } from "@phosphor-icons/react";

import type { Ability, KitTool, ToolPolicy } from "../../lib/kit-types";
import type { Permission } from "../../lib/identity/permissions.gen";
import { kitApi, mcpApi } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { plural, POLICY_LABEL, POLICY_TIP, riskOf, shortError, TIP } from "./kit-copy";

const POLICIES: ToolPolicy[] = ["allow", "ask", "off"];

/** `permission`: what setting this tool's policy needs — skill:manage for an ability's tools
 *  (POST /v1/kit/{ability}/policy), mcp_server:review for an MCP server's (…/servers/{id}/policy). */
export function PolicySeg({ tool, disabled, onPick, permission = "skill:manage" }: {
  tool: KitTool; disabled?: boolean; onPick: (p: ToolPolicy) => void; permission?: Permission;
}) {
  const locked = tool.floor === "ask";
  return (
    <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label={`${tool.label} policy`}
          data-testid={`kit-policy-${tool.ref}`}>
      {POLICIES.map((p) => {
        const below = locked && p === "allow";
        // A LOCKED ROW SAYS WHY WHERE THE POINTER IS (K16). A disabled button gets no hover, so
        // its tooltip never showed: the tip sits on a wrapper that does, and Ask (the lock)
        // carries the same sentence.
        const tip = locked && p !== "off" ? TIP.locked
          : p === "ask" && tool.kind === "mcp" ? `${POLICY_TIP.ask} · ${TIP.askInCode}` : POLICY_TIP[p];
        const button = (
          <GatedButton permission={permission} key={p} type="button" role="radio" aria-checked={tool.policy === p}
                  disabled={disabled || below} data-tip={below ? undefined : tip}
                  data-testid={`kit-policy-${tool.ref}-${p}`}
                  onClick={() => { if (tool.policy !== p) onPick(p); }}>
            {p === "ask" && locked ? <Lock size={11} aria-hidden="true" /> : null}
            {POLICY_LABEL[p]}
          </GatedButton>
        );
        return below
          ? <span key={p} className="kit-seg-lock" data-tip={tip} data-testid={`kit-policy-${tool.ref}-${p}-tip`}>{button}</span>
          : button;
      })}
    </span>
  );
}

export function RiskDot({ tier }: { tier: number }) {
  const risk = riskOf(tier);
  return <span className="kit-risk" data-tone={risk.tone} data-tip={risk.tip}>{risk.label}</span>;
}

export function ReviewRow({ server, projectId, actor, onReviewed, tools }: {
  server: { server_id: string; name: string; url?: string }; projectId: string; actor: string;
  onReviewed: () => void; tools: KitTool[];
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const top = tools.reduce((m, t) => Math.max(m, t.tier), 0);
  const review = async () => {
    setBusy(true);
    const r = await mcpApi.review(projectId, actor, server.server_id);
    setBusy(false); setOpen(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", "Marked reviewed");
    onReviewed();
  };
  return (
    <div className="kit-row kit-review" data-testid={`kit-review-${server.name}`}>
      <span className="kit-chip" data-tone="amber" data-tip={TIP.review}>Unreviewed</span>
      <span className="kit-fact" data-tip={server.name}>{server.name}</span>
      <span className="kit-confirm-wrap">
        <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm" data-tip={TIP.markReviewed}
                data-testid={`kit-review-open-${server.name}`} onClick={() => setOpen(true)}>Mark reviewed</GatedButton>
        {open ? (
          <span className="kit-pop" role="dialog" aria-label="Mark reviewed" data-testid="kit-review-pop">
            <strong data-tip={server.name}>{server.name}</strong>
            {server.url ? <span className="kit-fact" data-tip={server.url} data-testid="kit-review-url" data-user-text>{server.url}</span> : null}
            <span data-testid="kit-review-facts">{plural(tools.length, "tool")} · Highest: {riskOf(top).label}</span>
            <span className="kit-pop-actions">
              <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-primary kit-btn-sm" disabled={busy}
                      data-testid="kit-review-confirm" onClick={() => void review()}>Mark reviewed</GatedButton>
              <button type="button" className="kit-btn kit-btn-sm" onClick={() => setOpen(false)}>Cancel</button>
            </span>
          </span>
        ) : null}
      </span>
    </div>
  );
}

export function ToolsSection({ ability, projectId, actor, onChanged }: {
  ability: Ability; projectId: string; actor: string; onChanged: (a?: Ability) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const pick = async (tool: KitTool, policy: ToolPolicy) => {
    setBusy(tool.ref);
    const r = await kitApi.policy(projectId, actor, ability.key, tool.ref, policy);
    setBusy(null);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `${tool.label} · ${POLICY_LABEL[policy]}`);
    onChanged(r.data);
  };
  const unreviewed = ability.mcp.filter((m) => !m.trusted);
  return (
    <div className="kit-tools" data-testid="kit-tools">
      {unreviewed.map((m) => (
        <ReviewRow key={m.server_id} server={m} projectId={projectId} actor={actor}
                   tools={ability.tools.filter((t) => t.ref.startsWith(`mcp:${m.name}.`))}
                   onReviewed={() => onChanged()} />
      ))}
      {ability.tools.length === 0 ? <div className="kit-faint">No tools yet</div> : null}
      {ability.tools.map((t) => (
        <div key={t.ref} className="kit-tool" data-testid={`kit-tool-${t.ref}`}>
          <span className="kit-tool-label" data-tip={t.ref.replace(/^(skill|mcp):/, "")}>{t.label}</span>
          <RiskDot tier={t.tier} />
          <PolicySeg tool={t} permission="skill:manage" disabled={busy === t.ref} onPick={(p) => void pick(t, p)} />
        </div>
      ))}
    </div>
  );
}
