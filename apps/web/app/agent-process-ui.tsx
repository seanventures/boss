"use client";

import React, { useEffect, useState, useTransition } from "react";

import {
  loadAgentProcessRunsAction,
  startAgentProcessAction,
} from "./agent-process-actions";
import { SubmitButton } from "./submit-button";

type ProcessRun = {
  id: string;
  title: string;
  status: string;
  process_key?: string;
  template_name?: string;
};

export function AgentProcesses({
  projectId,
  actor,
}: {
  projectId: string;
  actor: string;
}) {
  const [runs, setRuns] = useState<ProcessRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<Record<string, unknown> | null>(null);
  const [pending, startTransition] = useTransition();

  const reload = () =>
    loadAgentProcessRunsAction(projectId).then((result) => {
      if (result.ok && result.data) {
        const items = Array.isArray(result.data.runs) ? result.data.runs : [];
        setRuns(
          items.flatMap((item) => {
            if (!item || typeof item !== "object") return [];
            const row = item as Record<string, unknown>;
            return [
              {
                id: String(row.id || ""),
                title: String(row.title || "process"),
                status: String(row.status || ""),
                process_key: typeof row.process_key === "string" ? row.process_key : undefined,
                template_name: typeof row.template_name === "string" ? row.template_name : undefined,
              },
            ];
          }),
        );
        setError(null);
        return;
      }
      setError(result.error ?? "Process list unavailable.");
    });

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const steps = Array.isArray(last?.steps) ? (last?.steps as Array<Record<string, unknown>>) : [];

  return (
    <article className="stack-card" data-testid="agent-processes">
      <div className="section-head">
        <div>
          <span className="eyebrow">Automation</span>
          <h2>Agent process</h2>
        </div>
      </div>
      <p className="detail">
        Deliver a PRD. Architect, CTO, and VP product review it. The PRD is
        improved, handed to the engineering captain for buildout, checked by QA
        and UI/UX, verified by the captain, certified by a panel, then accepted
        as closed work. Simulated mode never spends.
      </p>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      <form
        className="composer"
        data-testid="agent-process-start"
        action={(formData) => {
          setError(null);
          startTransition(async () => {
            const result = await startAgentProcessAction(formData);
            if (result.ok && result.data) {
              setLast(result.data);
              await reload();
              return;
            }
            setError(result.error ?? "Start failed.");
          });
        }}
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <label className="field">
          <span>Title</span>
          <input className="input" name="title" placeholder="HookWatch v1" disabled={pending} />
        </label>
        <label className="field">
          <span>PRD</span>
          <textarea
            className="input"
            name="prd"
            rows={8}
            required
            placeholder="Paste the PRD. Reviewers look for users, acceptance, data/API, risks, and success metrics."
            disabled={pending}
          />
        </label>
        <label>
          <input type="checkbox" name="simulated" value="true" defaultChecked />
          Simulated (no factory spend)
        </label>
        <label>
          <input type="checkbox" name="auto_build" value="true" />
          Auto-build (enqueue governed code.task)
        </label>
        <SubmitButton className="button" type="submit" disabled={pending}>
          Start PRD process
        </SubmitButton>
      </form>

      {steps.length > 0 ? (
        <div data-testid="agent-process-timeline">
          <h4>Last handoff timeline</h4>
          <ul className="detail">
            {steps.map((step) => (
              <li key={String(step.id || step.step_key)}>
                {String(step.step_key)} · {String(step.role_key)} · {String(step.action)} ·{" "}
                {String(step.status)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <h4>Recent runs</h4>
      {runs.length === 0 ? (
        <p className="empty-state" data-testid="agent-process-empty">
          No process runs yet.
        </p>
      ) : (
        <div className="feed-list">
          {runs.map((run) => (
            <div className="feed-row" key={run.id}>
              <div>
                <h4>{run.title}</h4>
                <p className="detail">
                  {run.template_name || run.process_key || "process"} · {run.status}
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}
