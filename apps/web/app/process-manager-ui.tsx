"use client";

import React, { useEffect, useState, useTransition } from "react";

import { formatDateTime } from "../lib/datetime";
import {
  loadProcessManagerAction,
  tickProcessManagerAction,
} from "./process-manager-actions";
import { SubmitButton } from "./submit-button";

import { callAction } from "../lib/call-action";
type ProcessManagerView = {
  apiReachable: boolean;
  blockedReason: string;
  lastTick: Record<string, unknown> | null;
  metrics: Record<string, unknown> | null;
  proposals: Array<Record<string, unknown>>;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asRecords(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const row = asRecord(item);
    return row ? [row] : [];
  });
}

function displayValue(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return "—";
  }
}

function mapSnapshot(data: Record<string, unknown> | undefined): ProcessManagerView {
  const lastTick = asRecord(data?.last_tick);
  const metrics = asRecord(data?.metrics);
  const proposals = asRecords(data?.proposals);
  const hasMetrics = Boolean(metrics && Object.keys(metrics).length > 0);
  return {
    apiReachable: true,
    blockedReason: "",
    lastTick,
    metrics: hasMetrics ? metrics : null,
    proposals,
  };
}

export function ProcessManager({
  projectId,
  actor,
}: {
  projectId: string;
  actor: string;
}) {
  const [state, setState] = useState<ProcessManagerView>({
    apiReachable: false,
    blockedReason: "Loading process manager…",
    lastTick: null,
    metrics: null,
    proposals: [],
  });
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const applyResult = (data: Record<string, unknown> | undefined) => {
    setState(mapSnapshot(data));
    setError(null);
  };

  const reload = () =>
    callAction(loadProcessManagerAction, undefined, "autopilot:read")(projectId).then((result) => {
      if (result.ok && result.data) {
        applyResult(result.data);
        return;
      }
      setState({
        apiReachable: false,
        blockedReason: result.error ?? "Process manager runtime is unavailable.",
        lastTick: null,
        metrics: null,
        proposals: [],
      });
      setError(result.error ?? "Process manager runtime is unavailable.");
    });

  useEffect(() => {
    let alive = true;
    callAction(loadProcessManagerAction, undefined, "autopilot:read")(projectId).then((result) => {
      if (!alive) return;
      if (result.ok && result.data) {
        applyResult(result.data);
        return;
      }
      setState({
        apiReachable: false,
        blockedReason: result.error ?? "Process manager runtime is unavailable.",
        lastTick: null,
        metrics: null,
        proposals: [],
      });
      setError(result.error ?? "Process manager runtime is unavailable.");
    });
    return () => {
      alive = false;
    };
  }, [projectId]);

  const metricEntries = Object.entries(state.metrics ?? {}).filter(
    ([, value]) => value !== undefined,
  );
  const lastTickAt =
    typeof state.lastTick?.at === "string"
      ? state.lastTick.at
      : typeof state.lastTick?.ticked_at === "string"
        ? state.lastTick.ticked_at
        : null;

  return (
    <article className="stack-card" data-testid="process-manager">
      <div className="section-head">
        <div>
          <span className="eyebrow">Steward</span>
          <h2>Project manager</h2>
        </div>
      </div>
      <p className="detail">
        Operate, test, validate, and propose improvements to agent workflows.
        Tick is fail-closed. Decisions stay human. No invented scores.
        Tick never spends, never auto-builds, never rewrites a published template.
      </p>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      <form
        action={(formData) => {
          setError(null);
          startTransition(async () => {
            const result = await callAction(tickProcessManagerAction, undefined, "autopilot:operate")(formData);
            if (result.ok && result.data) {
              applyResult(result.data);
              await reload();
              return;
            }
            setError(result.error ?? "Tick failed.");
          });
        }}
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <SubmitButton permission="autopilot:operate" className="button" type="submit" disabled={pending}>
          Tick now
        </SubmitButton>
      </form>

      <h4>Last tick</h4>
      {!state.apiReachable ? (
        <p className="empty-state" data-testid="process-manager-unavailable">
          {state.blockedReason || "Process manager runtime is unavailable."}
        </p>
      ) : null}
      {state.apiReachable && !state.lastTick ? (
        <p className="empty-state" data-testid="process-manager-tick-empty">
          No tick recorded yet. Use Tick now or wait for the worker cycle.
        </p>
      ) : null}
      {state.lastTick ? (
        <div className="feed-row" data-testid="process-manager-last-tick">
          <div>
            <p className="detail">
              {typeof state.lastTick.status === "string" ? state.lastTick.status : "recorded"}
              {lastTickAt ? ` · ${formatDateTime(lastTickAt)}` : ""}
            </p>
            {typeof state.lastTick.operate === "string" ? (
              <p className="detail">{state.lastTick.operate}</p>
            ) : null}
            {typeof state.lastTick.canary_run_id === "string" && state.lastTick.canary_run_id ? (
              <p className="detail">
                Simulated canary <code>{state.lastTick.canary_run_id}</code>
              </p>
            ) : null}
            {Array.isArray(state.lastTick.rejected) && state.lastTick.rejected.length > 0 ? (
              <p className="detail" data-testid="process-manager-rejected">
                Rejected {state.lastTick.rejected.length} move
                {state.lastTick.rejected.length === 1 ? "" : "s"} (never silent).
              </p>
            ) : null}
          </div>
        </div>
      ) : null}

      <h4>Metrics</h4>
      {state.apiReachable && metricEntries.length === 0 ? (
        <p className="empty-state" data-testid="process-manager-metrics-empty">
          No metrics recorded yet.
        </p>
      ) : null}
      {metricEntries.length > 0 ? (
        <dl className="detail" data-testid="process-manager-metrics">
          {metricEntries.map(([key, value]) => (
            <div key={key}>
              <dt>{key.replaceAll("_", " ")}</dt>
              <dd>{displayValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      <h4>Proposals</h4>
      {state.apiReachable && state.proposals.length === 0 ? (
        <p className="empty-state" data-testid="process-manager-proposals-empty">
          No improvement proposals.
        </p>
      ) : null}
      {state.proposals.map((proposal, index) => (
        <div
          className="feed-row"
          key={String(proposal.id || proposal.key || index)}
          data-testid="process-manager-proposal"
        >
          <div>
            <h4>{String(proposal.title || proposal.summary || "proposal")}</h4>
            <p className="detail">
              {typeof proposal.status === "string" ? proposal.status : "read-only"}
              {typeof proposal.kind === "string" ? ` · ${proposal.kind}` : ""}
            </p>
            {typeof proposal.summary === "string" && proposal.title ? (
              <p className="detail">{proposal.summary}</p>
            ) : null}
          </div>
        </div>
      ))}
    </article>
  );
}
