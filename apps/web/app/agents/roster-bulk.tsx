"use client";

/**
 * Signal bench: one action sets the model connection and default model
 * on the agents you tick. A row that does not take the change is listed.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

export type RosterAgent = {
  roleKey: string;
  name: string;
  connectionId: string | null;
  connectionLabel: string;
  defaultModel: string | null;
  lifecycle: string;
};

export type RosterConnection = {
  id: string;
  label: string;
  models: string[];
};

export type ApplyRequest = {
  dedupeKey: string;
  connectionId: string | null;
  defaultModel: string | null;
  agents: Array<{
    roleKey: string;
    expected: { connectionId: string | null; defaultModel: string | null };
  }>;
};

export type ApplyResult = {
  receiptId: string;
  dedupeKey: string;
  replayed: boolean;
  applied: Array<{ roleKey: string; connectionId: string | null; defaultModel: string | null }>;
  failed: Array<{ roleKey: string; error: string }>;
};

type Pin = { connectionId: string | null; defaultModel: string | null; connectionLabel?: string };

const BENCH_CSS = `
.rb-bench {
  --ground: #102027;
  --paper: #F7F4EF;
  --filament: #F2C14E;
  --phosphor: #7CDEDC;
  --fault: #E85D4C;
  --metal: #8AA0A8;
  --ink: #1A2428;
  background: var(--ground);
  color: var(--paper);
  font-family: "Sora", "Avenir Next", "Segoe UI", sans-serif;
  padding: 28px 22px 40px;
  min-height: 100%;
}
.rb-bench * { box-sizing: border-box; }
.rb-kicker {
  margin: 0;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--phosphor);
}
.rb-title {
  margin: 8px 0 10px;
  font-size: 34px;
  font-weight: 600;
  letter-spacing: -0.04em;
  line-height: 1;
}
.rb-intro { max-width: 40rem; margin: 0; line-height: 1.5; font-size: 16px; }
.rb-bus {
  display: flex;
  align-items: center;
  gap: 14px;
  margin: 22px 0 16px;
}
.rb-rail {
  flex: 1;
  height: 3px;
  border-radius: 999px;
  background-image: linear-gradient(90deg, var(--phosphor), var(--filament), var(--phosphor));
  background-size: 200% 100%;
  box-shadow: 0 0 14px var(--phosphor);
  animation: rb-sweep 4.8s linear infinite;
}
.rb-count {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  color: var(--filament);
  font-size: 13px;
  white-space: nowrap;
}
.rb-go, .rb-retry {
  border: 0;
  cursor: pointer;
  font-family: "Sora", "Avenir Next", sans-serif;
  font-weight: 600;
  padding: 11px 16px;
}
.rb-go { background: var(--filament); color: var(--ink); }
.rb-retry { background: transparent; color: var(--paper); border: 1px solid var(--fault); }
.rb-go:disabled, .rb-retry:disabled { opacity: 0.4; cursor: not-allowed; }
.rb-go:focus-visible, .rb-retry:focus-visible,
.rb-bench input:focus-visible, .rb-bench select:focus-visible {
  outline: 2px solid var(--phosphor);
  outline-offset: 2px;
}
.rb-rows { display: flex; flex-direction: column; gap: 6px; }
.rb-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 1.1fr);
  gap: 12px;
  align-items: center;
  background: var(--paper);
  color: var(--ink);
  padding: 11px 14px;
}
.rb-row label { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.rb-meta {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 13px;
  color: #3d5258;
}
.rb-off { opacity: 0.55; }
.rb-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 18px 28px;
  align-items: flex-end;
  margin-top: 18px;
}
.rb-field { display: flex; flex-direction: column; gap: 6px; color: var(--metal); font-size: 13px; }
.rb-field select {
  background: var(--ground);
  color: var(--paper);
  border: 1px solid var(--phosphor);
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 13px;
  padding: 8px 10px;
  min-width: 220px;
}
.rb-check { display: flex; align-items: center; gap: 8px; color: var(--paper); }
.rb-blocked { color: var(--filament); max-width: 40rem; }
.rb-fault { color: var(--fault); margin: 8px 0 0; }
.rb-note { color: var(--metal); }
@media (max-width: 720px) {
  .rb-row { grid-template-columns: auto 1fr; }
  .rb-bus { flex-wrap: wrap; }
  .rb-title { font-size: 28px; }
}
@media (prefers-reduced-motion: reduce) {
  .rb-rail { animation: none; box-shadow: none; }
}
@keyframes rb-sweep {
  from { background-position: 0% 0; }
  to { background-position: 200% 0; }
}
`;

function freshKey(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `roster-${uuid}`;
  return `roster-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export async function applyRosterChange(
  projectId: string,
  actor: string,
  body: ApplyRequest,
): Promise<ApplyResult> {
  const { apiWrite } = await import("../../lib/api-write");
  const response = await apiWrite("/v1/agents/roster-bulk", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      project_id: projectId,
      actor,
      dedupe_key: body.dedupeKey,
      connection_id: body.connectionId,
      default_model: body.defaultModel,
      agents: body.agents.map((agent) => ({
        role_key: agent.roleKey,
        expected: {
          connection_id: agent.expected.connectionId,
          default_model: agent.expected.defaultModel,
        },
      })),
    }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || "The roster change did not save.");
  }
  const json = await response.json();
  return {
    receiptId: String(json.receipt_id ?? ""),
    dedupeKey: String(json.dedupe_key ?? body.dedupeKey),
    replayed: Boolean(json.replayed),
    applied: (json.applied ?? []).map((row: { role_key: string; connection_id: string | null; default_model: string | null }) => ({
      roleKey: row.role_key,
      connectionId: row.connection_id,
      defaultModel: row.default_model,
    })),
    failed: (json.failed ?? []).map((row: { role_key: string; error: string }) => ({
      roleKey: row.role_key,
      error: row.error,
    })),
  };
}

function mapSnapshot(body: {
  agents?: Array<Record<string, string | null>>;
  connections?: Array<{ id: string; label: string; models?: string[] }>;
}): { agents: RosterAgent[]; connections: RosterConnection[] } {
  return {
    agents: (body.agents ?? []).map((agent) => ({
      roleKey: String(agent.role_key ?? ""),
      name: String(agent.name ?? agent.role_key ?? ""),
      connectionId: agent.connection_id ?? null,
      connectionLabel: String(agent.connection_label ?? "Local credentials"),
      defaultModel: agent.default_model ?? null,
      lifecycle: String(agent.lifecycle ?? "active"),
    })),
    connections: (body.connections ?? []).map((item) => ({
      id: item.id,
      label: item.label,
      models: item.models ?? [],
    })),
  };
}

export function RosterBulk({
  agents: givenAgents,
  connections: givenConnections,
  projectId = "",
  actor = "ceo",
  onApply,
}: {
  agents?: RosterAgent[];
  connections?: RosterConnection[];
  projectId?: string;
  actor?: string;
  onApply?: (body: ApplyRequest) => Promise<ApplyResult>;
}) {
  const [loaded, setLoaded] = useState<{ agents: RosterAgent[]; connections: RosterConnection[] } | null>(null);
  const [loadError, setLoadError] = useState("");
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [pins, setPins] = useState<Record<string, Pin>>({});
  const [changeConnection, setChangeConnection] = useState(false);
  const [changeModel, setChangeModel] = useState(false);
  const [connectionId, setConnectionId] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [failed, setFailed] = useState<ApplyResult["failed"]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const flying = useRef(false);

  useEffect(() => {
    if (givenAgents || !projectId) return;
    let cancelled = false;
    fetch(`/v1/agents/roster-bulk?project_id=${encodeURIComponent(projectId)}`)
      .then(async (response) => {
        if (!response.ok) throw new Error("The roster did not load.");
        return response.json();
      })
      .then((body) => {
        if (!cancelled) setLoaded(mapSnapshot(body));
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : "The roster did not load.");
      });
    return () => {
      cancelled = true;
    };
  }, [givenAgents, projectId]);

  const agents = givenAgents ?? loaded?.agents ?? [];
  const connections = givenConnections ?? loaded?.connections ?? [];

  function view(agent: RosterAgent): RosterAgent {
    const pin = pins[agent.roleKey];
    if (!pin) return agent;
    return {
      ...agent,
      connectionId: pin.connectionId,
      defaultModel: pin.defaultModel,
      connectionLabel: pin.connectionLabel || agent.connectionLabel,
    };
  }

  const selected = agents.filter((agent) => picked[agent.roleKey] && agent.lifecycle !== "retired");

  const modelChoices = useMemo(() => {
    const source = changeConnection
      ? connections.filter((item) => item.id === connectionId)
      : connections;
    const seen = new Set<string>();
    const models: string[] = [];
    for (const item of source) {
      for (const model of item.models) {
        if (!seen.has(model)) {
          seen.add(model);
          models.push(model);
        }
      }
    }
    return models;
  }, [changeConnection, connectionId, connections]);

  const heldBack = selected.flatMap((agent) => {
    const pin = view(agent);
    if (!(changeConnection && !changeModel && connectionId && pin.defaultModel)) return [];
    const offered = connections.find((item) => item.id === connectionId)?.models ?? [];
    if (offered.length === 0 || offered.includes(pin.defaultModel)) return [];
    return [`${agent.name} keeps ${pin.defaultModel}`];
  });

  const canApply = selected.length > 0
    && (changeConnection || changeModel)
    && heldBack.length === 0
    && !busy
    && (!changeConnection || connectionId !== "")
    && (!changeModel || defaultModel !== "");

  async function send(roleKeys: string[]) {
    if (flying.current || roleKeys.length === 0) return;
    flying.current = true;
    setBusy(true);
    setError("");
    const body: ApplyRequest = {
      dedupeKey: freshKey(),
      connectionId: changeConnection ? connectionId : null,
      defaultModel: changeModel ? defaultModel : null,
      agents: roleKeys.map((roleKey) => {
        const agent = view(agents.find((item) => item.roleKey === roleKey)!);
        return {
          roleKey,
          expected: { connectionId: agent.connectionId, defaultModel: agent.defaultModel },
        };
      }),
    };
    const sender = onApply ?? ((request: ApplyRequest) => applyRosterChange(projectId, actor, request));
    try {
      const result = await sender(body);
      setFailed(result.failed);
      setPins((prev) => {
        const next = { ...prev };
        for (const row of result.applied) {
          const label = connections.find((item) => item.id === row.connectionId)?.label;
          next[row.roleKey] = {
            connectionId: row.connectionId,
            defaultModel: row.defaultModel,
            connectionLabel: label,
          };
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "The roster change did not save.");
    } finally {
      flying.current = false;
      setBusy(false);
    }
  }

  const countLabel = selected.length === 1 ? "1 agent" : `${selected.length} agents`;

  return (
    <section className="rb-bench">
      <style>{BENCH_CSS}</style>
      <p className="rb-kicker">One action</p>
      <h1 className="rb-title">Model bench</h1>
      <p data-testid="rb-intro">
        Choose agents on the bench, then set the connection and the default model they answer with.
      </p>
      {loadError ? <p className="rb-note">{loadError}</p> : null}
      <div className="rb-bus">
        <span className="rb-rail" aria-hidden="true" />
        <span className="rb-count">{selected.length} on the bus</span>
        <button
          type="button"
          className="rb-go"
          data-testid="rb-apply"
          disabled={!canApply}
          title="Set the ticked agents"
          onClick={() => void send(selected.map((agent) => agent.roleKey))}
        >
          {busy ? "Setting" : `Set ${countLabel}`}
        </button>
      </div>
      <div className="rb-rows">
        {agents.map((agent) => {
          const shown = view(agent);
          const retired = agent.lifecycle === "retired";
          return (
            <div
              key={agent.roleKey}
              className={retired ? "rb-row rb-off" : "rb-row"}
              data-testid={`rb-row-${agent.roleKey}`}
            >
              <label>
                <input
                  type="checkbox"
                  data-testid={`rb-agent-${agent.roleKey}`}
                  checked={Boolean(picked[agent.roleKey])}
                  disabled={retired}
                  title={retired ? "Retired agents stay off" : "Include this agent"}
                  onChange={(event) => {
                    const on = event.target.checked;
                    setPicked((prev) => ({ ...prev, [agent.roleKey]: on }));
                  }}
                />
                <span className="rb-name">{agent.name}</span>
              </label>
              <span className="rb-meta">{shown.connectionLabel}</span>
              <span className="rb-meta">{shown.defaultModel || "No model yet"}</span>
              <span className="rb-meta">{retired ? "Retired" : "On the bench"}</span>
            </div>
          );
        })}
      </div>
      <div className="rb-controls">
        <label className="rb-check">
          <input
            type="checkbox"
            data-testid="rb-change-connection"
            checked={changeConnection}
            title="Replace the connection"
            onChange={(event) => setChangeConnection(event.target.checked)}
          />
          Change connection
        </label>
        <label className="rb-field">
          Connection
          <select
            data-testid="rb-connection"
            value={connectionId}
            disabled={!changeConnection}
            onChange={(event) => setConnectionId(event.target.value)}
          >
            <option value="">Choose</option>
            {connections.map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
          </select>
        </label>
        <label className="rb-check">
          <input
            type="checkbox"
            data-testid="rb-change-model"
            checked={changeModel}
            title="Replace the default model"
            onChange={(event) => setChangeModel(event.target.checked)}
          />
          Change default model
        </label>
        <label className="rb-field">
          Default model
          <select
            data-testid="rb-model"
            value={defaultModel}
            disabled={!changeModel}
            onChange={(event) => setDefaultModel(event.target.value)}
          >
            <option value="">Choose</option>
            {modelChoices.map((model) => (
              <option key={model} value={model}>{model}</option>
            ))}
          </select>
        </label>
      </div>
      {heldBack.length > 0 ? (
        <p className="rb-blocked" data-testid="rb-blocked">
          {heldBack.join(". ")}. That model is not on this connection, so the bench will not send the change.
        </p>
      ) : null}
      {failed.map((row) => (
        <p key={row.roleKey} className="rb-fault" data-testid={`rb-failure-${row.roleKey}`}>
          {agents.find((agent) => agent.roleKey === row.roleKey)?.name ?? row.roleKey} stayed as it was. {row.error}
        </p>
      ))}
      {failed.length > 0 ? (
        <button
          type="button"
          className="rb-retry"
          data-testid="rb-retry"
          disabled={busy}
          title="Send the listed rows again"
          onClick={() => void send(failed.map((row) => row.roleKey))}
        >
          Retry listed rows
        </button>
      ) : null}
      {error ? <p className="rb-fault">{error}</p> : null}
    </section>
  );
}
