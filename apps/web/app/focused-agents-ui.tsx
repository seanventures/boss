"use client";

import React, { useEffect, useState, useTransition } from "react";

import { formatDateTime } from "../lib/datetime";
import {
  FOCUS_KIND_OPTIONS,
  FOCUS_MODE_OPTIONS,
  emptyFocusedAgents,
  mapFocusedAgentList,
  type FocusedAgentState,
} from "../lib/focused-agents";
import {
  activateFocusedAgentAction,
  createFocusedAgentAction,
  loadFocusedAgentsAction,
  pauseFocusedAgentAction,
  wakeFocusedAgentAction,
} from "./focused-agents-actions";
import { SubmitButton } from "./submit-button";

export function FocusedAgents({
  projectId,
  actor,
}: {
  projectId: string;
  actor: string;
}) {
  const [state, setState] = useState<FocusedAgentState>(
    emptyFocusedAgents("Loading focused agents…"),
  );
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const reload = () =>
    loadFocusedAgentsAction(projectId).then((result) => {
      if (result.ok && result.data) {
        setState(mapFocusedAgentList(result.data));
        setError(null);
        return;
      }
      setState(emptyFocusedAgents(result.error ?? "Focused agents unavailable."));
      setError(result.error ?? "Focused agents unavailable.");
    });

  useEffect(() => {
    let alive = true;
    loadFocusedAgentsAction(projectId).then((result) => {
      if (!alive) return;
      if (result.ok && result.data) {
        setState(mapFocusedAgentList(result.data));
        setError(null);
        return;
      }
      setState(emptyFocusedAgents(result.error ?? "Focused agents unavailable."));
      setError(result.error ?? "Focused agents unavailable.");
    });
    return () => {
      alive = false;
    };
  }, [projectId]);

  return (
    <article className="stack-card" data-testid="focused-agents">
      <div className="section-head">
        <div>
          <span className="eyebrow">Optional</span>
          <h2>On-duty automations</h2>
        </div>
      </div>
      <p className="detail">
        Schedule a focused agent to watch this project or wake it on demand.
        Actions still pass policy and approval checks.
      </p>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      <form
        className="composer"
        data-testid="focused-agents-builder"
        action={(formData) => {
          setError(null);
          startTransition(async () => {
            const result = await createFocusedAgentAction(formData);
            if (result.ok) {
              await reload();
              return;
            }
            setError(result.error ?? "Create failed.");
          });
        }}
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <label className="field">
          <span>Template</span>
          <select className="input" name="kind" defaultValue="preview_keeper" disabled={pending}>
            {FOCUS_KIND_OPTIONS.map((kind) => (
              <option key={kind} value={kind}>
                {kind.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Name</span>
          <input
            className="input"
            name="name"
            required
            placeholder="Preview keeper"
            disabled={pending}
          />
        </label>
        <label className="field">
          <span>Focus</span>
          <textarea
            className="input"
            name="focus"
            rows={2}
            required
            placeholder="Keep verify-green apps previewed and reachable."
            disabled={pending}
          />
        </label>
        <label className="field">
          <span>Mode</span>
          <select className="input" name="mode" defaultValue="always_on" disabled={pending}>
            {FOCUS_MODE_OPTIONS.map((mode) => (
              <option key={mode} value={mode}>
                {mode.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Interval seconds</span>
          <input
            className="input"
            name="interval_seconds"
            type="number"
            min={60}
            max={3600}
            defaultValue={120}
            disabled={pending}
          />
        </label>
        <fieldset className="field">
          <legend>Allowed actions</legend>
          <label>
            <input type="checkbox" name="allowed_actions" value="restart_preview" defaultChecked />
            restart preview
          </label>
          <label>
            <input type="checkbox" name="allowed_actions" value="launch_factory_job" />
            launch factory job
          </label>
          <label>
            <input type="checkbox" name="allowed_actions" value="propose_review" />
            propose review
          </label>
        </fieldset>
        <SubmitButton className="button" type="submit" disabled={pending}>
          Create and start
        </SubmitButton>
      </form>

      <h4>On duty</h4>
      {!state.apiReachable ? (
        <p className="empty-state" data-testid="focused-agents-unavailable">
          {state.blockedReason || "Focused agents are unavailable. No mock roster was invented."}
        </p>
      ) : null}
      {state.apiReachable && state.agents.length === 0 ? (
        <p className="empty-state" data-testid="focused-agents-empty">
          No on-duty automations yet. Create one above to schedule checks or wake it on demand.
        </p>
      ) : null}
      {state.agents.map((agent) => (
        <div className="feed-row" key={agent.id} data-testid="focused-agent-row">
          <div>
            <h4>{agent.name}</h4>
            <p className="detail">
              {agent.kind} · {agent.mode} · {agent.status}
              {agent.lastStatus ? ` · ${agent.lastStatus}` : ""}
              {agent.lastTickAt ? ` · ${formatDateTime(agent.lastTickAt)}` : ""}
            </p>
            <p className="detail">{agent.focus}</p>
          </div>
          <form
            action={(formData) => {
              startTransition(async () => {
                const result = await wakeFocusedAgentAction(formData);
                if (!result.ok) setError(result.error ?? "Wake failed.");
                else await reload();
              });
            }}
          >
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="actor" value={actor} />
            <input type="hidden" name="agent_id" value={agent.id} />
            <SubmitButton className="button secondary" type="submit" disabled={pending}>
              Wake now
            </SubmitButton>
          </form>
          {agent.status === "active" ? (
            <form
              action={(formData) => {
                startTransition(async () => {
                  const result = await pauseFocusedAgentAction(formData);
                  if (!result.ok) setError(result.error ?? "Pause failed.");
                  else await reload();
                });
              }}
            >
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="actor" value={actor} />
              <input type="hidden" name="agent_id" value={agent.id} />
              <SubmitButton className="button secondary" type="submit" disabled={pending}>
                Pause
              </SubmitButton>
            </form>
          ) : (
            <form
              action={(formData) => {
                startTransition(async () => {
                  const result = await activateFocusedAgentAction(formData);
                  if (!result.ok) setError(result.error ?? "Activate failed.");
                  else await reload();
                });
              }}
            >
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="actor" value={actor} />
              <input type="hidden" name="agent_id" value={agent.id} />
              <SubmitButton className="button secondary" type="submit" disabled={pending}>
                Activate
              </SubmitButton>
            </form>
          )}
        </div>
      ))}
    </article>
  );
}
