"use client";

import React, { useEffect, useMemo, useState, useTransition } from "react";

import {
  PROCESS_ACTIONS,
  PROCESS_KINDS,
  PROCESS_ROLES,
  builderStepsFromSpec,
  emptyBuilderStep,
  formatProcessStepLine,
  mapCompilePreview,
  mapProcessRuns,
  mapProcessSteps,
  mapProcessTemplates,
  slugProcessKey,
  specFromBuilderSteps,
  type BuilderStep,
  type ProcessRun,
  type ProcessSpecStep,
  type ProcessStep,
  type ProcessTemplate,
} from "../lib/agent-process";
import {
  compileAgentProcessAction,
  loadAgentProcessRunAction,
  loadAgentProcessRunsAction,
  loadAgentProcessTemplatesAction,
  saveAgentProcessTemplateAction,
  startAgentProcessAction,
} from "./agent-process-actions";
import { SubmitButton } from "./submit-button";

const DEFAULT_PROCESS_KEY = "prd_to_accepted_work";

function labelize(value: string): string {
  return value.replaceAll("_", " ");
}

export function AgentProcesses({
  projectId,
  actor,
}: {
  projectId: string;
  actor: string;
}) {
  const [templates, setTemplates] = useState<ProcessTemplate[]>([]);
  const [runs, setRuns] = useState<ProcessRun[]>([]);
  const [steps, setSteps] = useState<ProcessStep[]>([]);
  const [previewSteps, setPreviewSteps] = useState<ProcessSpecStep[]>([]);
  const [compiledSpecJson, setCompiledSpecJson] = useState("");
  const [builderSteps, setBuilderSteps] = useState<BuilderStep[]>([emptyBuilderStep(1)]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const reload = () =>
    Promise.all([
      loadAgentProcessTemplatesAction(projectId),
      loadAgentProcessRunsAction(projectId),
    ]).then(([templateResult, runResult]) => {
      const nextErrors: string[] = [];
      if (templateResult.ok && templateResult.data) {
        setTemplates(mapProcessTemplates(templateResult.data));
      } else {
        nextErrors.push(templateResult.error ?? "Process templates unavailable.");
      }
      if (runResult.ok && runResult.data) {
        setRuns(mapProcessRuns(runResult.data));
      } else {
        nextErrors.push(runResult.error ?? "Process list unavailable.");
      }
      setError(nextErrors[0] ?? null);
    });

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      loadAgentProcessTemplatesAction(projectId),
      loadAgentProcessRunsAction(projectId),
    ]).then(([templateResult, runResult]) => {
      if (!alive) return;
      const nextErrors: string[] = [];
      if (templateResult.ok && templateResult.data) {
        setTemplates(mapProcessTemplates(templateResult.data));
      } else {
        nextErrors.push(templateResult.error ?? "Process templates unavailable.");
      }
      if (runResult.ok && runResult.data) {
        setRuns(mapProcessRuns(runResult.data));
      } else {
        nextErrors.push(runResult.error ?? "Process list unavailable.");
      }
      setError(nextErrors[0] ?? null);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [projectId]);

  const templateOptions = useMemo(() => {
    if (templates.length > 0) return templates;
    return [
      {
        id: DEFAULT_PROCESS_KEY,
        processKey: DEFAULT_PROCESS_KEY,
        name: "PRD to accepted work",
        description: "",
      },
    ];
  }, [templates]);

  const builderSpec = useMemo(
    () =>
      specFromBuilderSteps({
        processKey: "custom",
        name: "Custom process",
        steps: builderSteps,
      }),
    [builderSteps],
  );

  const openRun = (runId: string) => {
    setSelectedRunId(runId);
    setDetailLoading(true);
    setError(null);
    startTransition(async () => {
      const result = await loadAgentProcessRunAction(projectId, runId);
      if (result.ok && result.data) {
        setSteps(mapProcessSteps(result.data));
        setDetailLoading(false);
        return;
      }
      setSteps([]);
      setDetailLoading(false);
      setError(result.error ?? "Process run unavailable.");
    });
  };

  const updateBuilderStep = (localId: string, patch: Partial<BuilderStep>) => {
    setBuilderSteps((current) =>
      current.map((step) => (step.localId === localId ? { ...step, ...patch } : step)),
    );
  };

  const moveBuilderStep = (index: number, delta: number) => {
    setBuilderSteps((current) => {
      const next = [...current];
      const target = index + delta;
      if (target < 0 || target >= next.length) return current;
      const [row] = next.splice(index, 1);
      next.splice(target, 0, row);
      return next;
    });
  };

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
        as closed work. Simulated mode never spends. Metatron can also build
        these from chat.
      </p>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      {loading ? (
        <p className="detail" data-testid="agent-process-loading">
          Loading process templates and runs…
        </p>
      ) : null}

      <form
        className="composer"
        data-testid="agent-process-nl"
        action={(formData) => {
          const intent = String(formData.get("intent") ?? "compile");
          setError(null);
          startTransition(async () => {
            if (intent === "compile") {
              const result = await compileAgentProcessAction(formData);
              if (!result.ok || !result.data) {
                setError(result.error ?? "Compile failed.");
                return;
              }
              const preview = mapCompilePreview(result.data);
              setPreviewSteps(preview.steps);
              if (preview.spec) {
                setCompiledSpecJson(JSON.stringify(preview.spec));
                setBuilderSteps(
                  preview.spec.steps.length
                    ? builderStepsFromSpec(preview.spec)
                    : [emptyBuilderStep(1)],
                );
              } else {
                setCompiledSpecJson("");
              }
              return;
            }
            let specJson = String(formData.get("spec") ?? "").trim();
            if (!specJson) {
              const compiled = await compileAgentProcessAction(formData);
              if (!compiled.ok || !compiled.data) {
                setError(compiled.error ?? "Compile failed.");
                return;
              }
              const preview = mapCompilePreview(compiled.data);
              setPreviewSteps(preview.steps);
              if (!preview.spec) {
                setError("Compile preview had no process spec to save.");
                return;
              }
              specJson = JSON.stringify(preview.spec);
              setCompiledSpecJson(specJson);
            }
            const saveData = new FormData();
            saveData.set("project_id", projectId);
            saveData.set("actor", actor);
            saveData.set(
              "process_key",
              String(formData.get("process_key") ?? "").trim() ||
                slugProcessKey(String(formData.get("name") ?? "custom")),
            );
            saveData.set(
              "name",
              String(formData.get("name") ?? "").trim() || "Custom process",
            );
            saveData.set("description", String(formData.get("description") ?? "").trim());
            saveData.set("spec", specJson);
            const saved = await saveAgentProcessTemplateAction(saveData);
            if (!saved.ok) {
              setError(saved.error ?? "Save failed.");
              return;
            }
            await reload();
          });
        }}
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <input type="hidden" name="spec" value={compiledSpecJson} />
        <label className="field">
          <span>Describe the handoff chain</span>
          <textarea
            className="input"
            name="description"
            rows={5}
            required
            placeholder="Architect reviews the PRD, QA checks the build, then the captain accepts."
            disabled={pending}
          />
        </label>
        <div className="input-grid">
          <label className="field">
            <span>Name</span>
            <input
              className="input"
              name="name"
              placeholder="PRD review chain"
              disabled={pending}
            />
          </label>
          <label className="field">
            <span>Process key</span>
            <input
              className="input"
              name="process_key"
              placeholder="prd_review_chain"
              disabled={pending}
            />
          </label>
        </div>
        <p className="detail">Compile previews steps. It does not spend.</p>
        <div className="inline-actions">
          <SubmitButton
            className="button"
            type="submit"
            name="intent"
            value="compile"
            disabled={pending}
          >
            Compile
          </SubmitButton>
          <SubmitButton
            className="button secondary"
            type="submit"
            name="intent"
            value="save"
            disabled={pending}
          >
            Save as process
          </SubmitButton>
        </div>
        {previewSteps.length > 0 ? (
          <div data-testid="agent-process-compile-preview">
            <h4>Compiled preview</h4>
            <ul className="detail">
              {previewSteps.map((step) => (
                <li key={step.id}>
                  {step.id} · {step.role} · {step.action} · {step.kind}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </form>

      <form
        className="composer"
        data-testid="agent-process-builder"
        action={(formData) => {
          setError(null);
          startTransition(async () => {
            const name = String(formData.get("name") ?? "").trim() || "Custom process";
            const processKey =
              String(formData.get("process_key") ?? "").trim() || slugProcessKey(name);
            const spec = specFromBuilderSteps({
              processKey,
              name,
              description: String(formData.get("description") ?? ""),
              steps: builderSteps,
            });
            if (!spec.steps.length) {
              setError("Add at least one step before saving.");
              return;
            }
            const saveData = new FormData();
            saveData.set("project_id", projectId);
            saveData.set("actor", actor);
            saveData.set("process_key", processKey);
            saveData.set("name", name);
            saveData.set("description", String(formData.get("description") ?? "").trim());
            saveData.set("spec", JSON.stringify(spec));
            const saved = await saveAgentProcessTemplateAction(saveData);
            if (!saved.ok) {
              setError(saved.error ?? "Save failed.");
              return;
            }
            await reload();
          });
        }}
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <input type="hidden" name="spec" value={JSON.stringify(builderSpec)} />
        <h3>Manual builder</h3>
        <p className="detail">
          Add, remove, and reorder role or panel steps. Save publishes a template.
          Decisions stay human.
        </p>
        <div className="input-grid">
          <label className="field">
            <span>Name</span>
            <input
              className="input"
              name="name"
              required
              placeholder="Custom handoff"
              disabled={pending}
            />
          </label>
          <label className="field">
            <span>Process key</span>
            <input
              className="input"
              name="process_key"
              placeholder="custom_handoff"
              disabled={pending}
            />
          </label>
        </div>
        <label className="field">
          <span>Description</span>
          <textarea
            className="input"
            name="description"
            rows={2}
            placeholder="Optional notes for this template."
            disabled={pending}
          />
        </label>
        {builderSteps.map((step, index) => (
          <div className="feed-row" key={step.localId} data-testid="agent-process-builder-step">
            <div className="input-grid" style={{ flex: 1 }}>
              <label className="field">
                <span>Step id</span>
                <input
                  className="input"
                  value={step.id}
                  onChange={(event) =>
                    updateBuilderStep(step.localId, { id: event.target.value })
                  }
                  disabled={pending}
                />
              </label>
              <label className="field">
                <span>Role</span>
                <select
                  className="input"
                  value={step.role}
                  onChange={(event) =>
                    updateBuilderStep(step.localId, {
                      role: event.target.value as BuilderStep["role"],
                    })
                  }
                  disabled={pending}
                >
                  {PROCESS_ROLES.map((role) => (
                    <option key={role} value={role}>
                      {labelize(role)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Action</span>
                <select
                  className="input"
                  value={step.action}
                  onChange={(event) =>
                    updateBuilderStep(step.localId, {
                      action: event.target.value as BuilderStep["action"],
                    })
                  }
                  disabled={pending}
                >
                  {PROCESS_ACTIONS.map((action) => (
                    <option key={action} value={action}>
                      {labelize(action)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>Kind</span>
                <select
                  className="input"
                  value={step.kind}
                  onChange={(event) =>
                    updateBuilderStep(step.localId, {
                      kind: event.target.value as BuilderStep["kind"],
                    })
                  }
                  disabled={pending}
                >
                  {PROCESS_KINDS.map((kind) => (
                    <option key={kind} value={kind}>
                      {kind}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="inline-actions">
              <button
                type="button"
                className="button ghost"
                disabled={pending || index === 0}
                onClick={() => moveBuilderStep(index, -1)}
              >
                Up
              </button>
              <button
                type="button"
                className="button ghost"
                disabled={pending || index === builderSteps.length - 1}
                onClick={() => moveBuilderStep(index, 1)}
              >
                Down
              </button>
              <button
                type="button"
                className="button secondary"
                disabled={pending || builderSteps.length === 1}
                onClick={() =>
                  setBuilderSteps((current) =>
                    current.filter((item) => item.localId !== step.localId),
                  )
                }
              >
                Remove
              </button>
            </div>
          </div>
        ))}
        <div className="inline-actions">
          <button
            type="button"
            className="button secondary"
            disabled={pending}
            onClick={() =>
              setBuilderSteps((current) => [...current, emptyBuilderStep(current.length + 1)])
            }
          >
            Add step
          </button>
          <SubmitButton className="button" type="submit" disabled={pending}>
            Save template
          </SubmitButton>
        </div>
      </form>

      <form
        className="composer"
        data-testid="agent-process-start"
        action={(formData) => {
          setError(null);
          startTransition(async () => {
            const result = await startAgentProcessAction(formData);
            if (result.ok && result.data) {
              setSelectedRunId(typeof result.data.id === "string" ? result.data.id : null);
              setSteps(mapProcessSteps(result.data));
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
          <span>Process</span>
          <select
            className="input"
            name="process_key"
            defaultValue={DEFAULT_PROCESS_KEY}
            disabled={pending}
          >
            {templateOptions.map((template) => (
              <option key={template.processKey} value={template.processKey}>
                {template.name} ({template.processKey})
              </option>
            ))}
          </select>
        </label>
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

      {detailLoading ? (
        <p className="detail" data-testid="agent-process-timeline-loading">
          Loading handoff timeline…
        </p>
      ) : null}

      {steps.length > 0 ? (
        <div data-testid="agent-process-timeline">
          <h4>Last handoff timeline</h4>
          <ul className="detail">
            {steps.map((step) => (
              <li key={step.id || step.stepKey}>{formatProcessStepLine(step)}</li>
            ))}
          </ul>
        </div>
      ) : selectedRunId && !detailLoading ? (
        <p className="empty-state" data-testid="agent-process-timeline-empty">
          No handoff steps recorded for this run.
        </p>
      ) : null}

      <h4>Recent runs</h4>
      {loading ? null : runs.length === 0 ? (
        <p className="empty-state" data-testid="agent-process-empty">
          No process runs yet.
        </p>
      ) : (
        <div className="feed-list">
          {runs.map((run) => (
            <button
              type="button"
              className="feed-row"
              key={run.id}
              data-testid="agent-process-run"
              aria-pressed={selectedRunId === run.id}
              disabled={pending}
              onClick={() => openRun(run.id)}
              style={{
                width: "100%",
                cursor: "pointer",
                background: selectedRunId === run.id ? "rgba(47, 111, 224, 0.08)" : "transparent",
                border: 0,
                color: "inherit",
                font: "inherit",
                textAlign: "left",
              }}
            >
              <div>
                <h4>{run.title}</h4>
                <p className="detail">
                  {run.templateName || run.processKey || "process"} · {run.status}
                </p>
              </div>
            </button>
          ))}
        </div>
      )}
    </article>
  );
}
