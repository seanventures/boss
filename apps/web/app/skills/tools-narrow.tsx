"use client";

/**
 * `Tools ▾` (spec §5.3 3): narrow which of an MCP-backed ability's tools ONE agent may call
 * (role_mcp_servers.tools). Rarely used, so it lives behind the agent's ⋯. Narrowing below what
 * the ability needs makes the agent stop holding the ability, said by its toggle afterwards.
 */
import { useGate } from "../../components/gated";
import React, { useEffect, useState } from "react";

import type { Ability } from "../../lib/kit-types";
import { mcpApi } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";

export function ToolsNarrow({ ability, role, projectId, actor, onClose }: {
  ability: Ability; role: string; projectId: string; actor: string; onClose: () => void;
}) {
  const [granted, setGranted] = useState<Record<string, string[]> | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    void mcpApi.grants(projectId, role).then((r) => {
      if (!r.ok) { setGranted({}); return; }
      setGranted(Object.fromEntries(r.data.assignments.map((a) => [a.mcp_server_id, a.tools])));
    });
  }, [projectId, role]);

  const toolsOf = (server: string) => ability.tools.filter((t) => t.ref.startsWith(`mcp:${server}.`))
    .map((t) => t.ref.slice(`mcp:${server}.`.length));

  const narrowGate = useGate("agent_ability:assign");
  const set = async (serverId: string, server: string, tool: string, on: boolean) => {
    const all = toolsOf(server);
    const current = granted?.[serverId]?.length ? granted[serverId] : all;
    const next = on ? [...new Set([...current, tool])] : current.filter((t) => t !== tool);
    if (next.length === 0) return;
    setBusy(true);
    const r = await mcpApi.roleTools(projectId, actor, serverId, role, next);
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    setGranted((g) => ({ ...(g ?? {}), [serverId]: next }));
    showToast("success", "Saved");
  };

  return (
    <span className="kit-pop" role="dialog" aria-label="Tools" data-testid={`kit-narrow-${role}`}>
      {ability.mcp.map((m) => (
        <span key={m.server_id} className="kit-narrow-list">
          {toolsOf(m.name).map((tool) => {
            const list = granted?.[m.server_id] ?? [];
            const on = list.length === 0 || list.includes(tool);
            return (
              <label key={tool} className="kit-check">
                <input {...narrowGate.props} type="checkbox" checked={on} disabled={(busy || granted === null) || !narrowGate.allowed}
                       onChange={(e) => void set(m.server_id, m.name, tool, e.target.checked)} />
                <span>{tool}</span>
              </label>
            );
          })}
        </span>
      ))}
      <span className="kit-pop-actions">
        <button type="button" className="kit-btn kit-btn-sm" onClick={onClose}>Done</button>
      </span>
    </span>
  );
}
