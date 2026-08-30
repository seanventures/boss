"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";

/**
 * Delivery workflows: choose one, watch it run, build your own.
 *
 * Three things an operator does here, in the order they do them:
 *   1. Pick a workflow from the twelve BOSS ships, and apply it to a class of work.
 *   2. Watch what is running — which step, waiting on whom, and what each step produced.
 *   3. Fork one and change it, by dragging steps around.
 *
 * The screen never invents a capability, a risk tier or an approval rule. Everything it draws
 * comes from the engine's registry, so the palette, the validation messages and the gates in
 * the plan are the same ones the runner will enforce. A builder that disagreed with the runtime
 * would let a customer save a workflow that cannot run.
 */

type Capability = {
  key: string;
  label: string;
  summary: string;
  risk_tier: number;
  required_inputs: string[];
  optional_inputs: string[];
  produces: string[];
  requires_project: string[];
  skills: string[];
  typical_minutes: number;
  is_gate: boolean;
  approval_forced_manual: boolean;
};

type CatalogueEntry = {
  key: string;
  name: string;
  description: string;
  work_class: string;
  step_count: number;
  steps: { id: string; capability: string }[];
  gates: string[];
  requires_project: string[];
  estimated_minutes: number;
};

type Installed = {
  id: string;
  workflow_key: string;
  name: string;
  is_catalogue: boolean;
  forked_from_version_id: string | null;
  namespace: string;
};

type RunStep = {
  step_id: string;
  kind: string;
  status: string;
  capability: string | null;
  output: Record<string, unknown>;
  evidence: Record<string, unknown>[];
  error: string | null;
  approval: Record<string, unknown>;
  due_at: string | null;
  timeout_applied: string | null;
  attempt: number;
};

type Run = {
  id: string;
  status: string;
  workflow_key: string;
  workflow_name: string;
  version: number;
  steps: RunStep[];
  step_count: number;
  steps_done: number;
  current_step: string | null;
  waiting_on: string | null;
  blocked_reason: string | null;
  error: string | null;
  updated_at: string | null;
};

type Draft = { id: string; capability: string; approval?: string };

const TABS = ["running", "catalogue", "builder"] as const;
type Tab = (typeof TABS)[number];

/** One value from a step's output, short enough for a single line. */
function summarise(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value.slice(0, 60);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
  if (typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>);
    // Prefer an id or a url if the object carries one — that is the thing an operator wants
    // to click, and it is buried one level down often enough to be worth looking for.
    const useful = keys.find((k) => /(^|_)(id|url)$/.test(k));
    if (useful) return String((value as Record<string, unknown>)[useful]).slice(0, 60);
    return keys.length ? `{${keys.slice(0, 3).join(", ")}}` : "{}";
  }
  return String(value).slice(0, 60);
}

/** Colour AND a word. A status column read only by hue does not survive a screenshot. */
function tone(status: string): string {
  if (status === "succeeded" || status === "completed") return "ok";
  if (status === "failed" || status === "deadlettered") return "bad";
  if (status === "waiting" || status === "blocked") return "wait";
  if (status === "running" || status === "leased" || status === "ready") return "live";
  return "idle";
}

async function get<T>(view: string, projectId: string): Promise<T | { error: string }> {
  const params = new URLSearchParams({ view });
  if (projectId) params.set("project_id", projectId);
  try {
    const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
    const body = await r.json();
    return r.ok ? body : { error: body?.error ?? `API returned ${r.status}` };
  } catch (e) {
    return { error: String((e as Error)?.message ?? e) };
  }
}

async function post(action: string, payload: Record<string, unknown>) {
  try {
    const r = await fetch("/api/delivery-workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...payload }),
    });
    const body = await r.json();
    return r.ok ? { ok: true as const, data: body } : { ok: false as const, error: body?.error };
  } catch (e) {
    return { ok: false as const, error: String((e as Error)?.message ?? e) };
  }
}

export function DeliveryWorkflows({ projectId, actor }: { projectId: string; actor: string }) {
  const [tab, setTab] = useState<Tab>("running");
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [riskFloor, setRiskFloor] = useState(2);
  const [catalogue, setCatalogue] = useState<CatalogueEntry[]>([]);
  const [installed, setInstalled] = useState<Installed[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  // Loading, failed and empty are three different things. A screen that renders nothing while
  // it loads and nothing when the API is down says "you have no workflows" in both cases.
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openRun, setOpenRun] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [caps, cat, inst, live] = await Promise.all([
      get<{ capabilities: Capability[]; manual_approval_risk_floor: number }>("capabilities", projectId),
      get<{ items: CatalogueEntry[] }>("catalogue", projectId),
      get<{ items: Installed[] }>("installed", projectId),
      get<{ runs: Run[] }>("runs", projectId),
    ]);
    const failure = [caps, cat, inst, live].find((r) => "error" in r) as { error: string } | undefined;
    if (failure) {
      setState("failed");
      setError(failure.error);
      return;
    }
    setCapabilities((caps as { capabilities: Capability[] }).capabilities ?? []);
    setRiskFloor((caps as { manual_approval_risk_floor: number }).manual_approval_risk_floor ?? 2);
    setCatalogue((cat as { items: CatalogueEntry[] }).items ?? []);
    setInstalled((inst as { items: Installed[] }).items ?? []);
    setRuns((live as { runs: Run[] }).runs ?? []);
    setState("ready");
    setError(null);
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  // Refresh while something is in flight. Stops when nothing is, so an idle console is idle.
  const anyLive = runs.some((r) => ["running", "waiting", "queued", "validating"].includes(r.status));
  useEffect(() => {
    if (!anyLive) return;
    const t = window.setInterval(() => { void load(); }, 8000);
    return () => window.clearInterval(t);
  }, [anyLive, load]);

  const act = useCallback(async (label: string, action: string, payload: Record<string, unknown>) => {
    setBusy(label);
    setNotice(null);
    const result = await post(action, { project_id: projectId, actor, ...payload });
    setBusy(null);
    // The engine's own sentence, not "something went wrong". "step 'd' is risk tier 2 and
    // always requires a person" is the whole value of the validation.
    setNotice(result.ok ? `${label} — done.` : `${label} — ${result.error}`);
    if (result.ok) await load();
    return result;
  }, [projectId, actor, load]);

  if (state === "loading") {
    return <p className="detail" data-testid="dw-loading">Loading delivery workflows…</p>;
  }
  if (state === "failed") {
    return (
      <p className="detail" style={{ color: "#f85149" }} data-testid="dw-error">
        Could not load delivery workflows — {error}
      </p>
    );
  }

  return (
    <div className="dw" data-testid="delivery-workflows">
      <div className="dw-tabs" role="tablist" aria-label="Delivery workflows">
        {TABS.map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t}
                  className="dw-tab" data-active={tab === t}
                  onClick={() => setTab(t)} data-testid={`dw-tab-${t}`}>
            {t === "running" ? `Running (${runs.length})`
              : t === "catalogue" ? `Catalogue (${catalogue.length})`
              : "Build your own"}
          </button>
        ))}
      </div>

      {notice ? <p className="dw-notice" data-testid="dw-notice">{notice}</p> : null}

      {tab === "running" ? (
        <RunsView runs={runs} openRun={openRun} setOpenRun={setOpenRun}
                  onAdvance={() => act("Advance", "advance", {})} busy={busy} />
      ) : tab === "catalogue" ? (
        <CatalogueView
          catalogue={catalogue} installed={installed} busy={busy}
          onInstall={() => act("Install the catalogue", "install", {})}
          onApply={(key, workClass) =>
            act(`Apply ${key}`, "apply", { workflow_key: key, work_class: workClass })}
          onStart={(key) =>
            act(`Start ${key}`, "start", {
              workflow_key: key,
              input: { directive: "Describe the work here", title: "New work" },
            })}
          onFork={(key) =>
            act(`Fork ${key}`, "fork", {
              workflow_key: key, new_key: `${key}_custom`, new_name: `${key} (yours)`,
            })}
        />
      ) : (
        <BuilderView
          capabilities={capabilities} riskFloor={riskFloor} installed={installed}
          busy={busy}
          onSave={(definitionId, spec) => act("Save", "save", { definition_id: definitionId, spec })}
        />
      )}
    </div>
  );
}

/* ── Running ─────────────────────────────────────────────────────────────────── */

function RunsView({ runs, openRun, setOpenRun, onAdvance, busy }: {
  runs: Run[];
  openRun: string | null;
  setOpenRun: (id: string | null) => void;
  onAdvance: () => void;
  busy: string | null;
}) {
  if (runs.length === 0) {
    return (
      <div className="dw-empty" data-testid="dw-no-runs">
        <p>Nothing is running on a delivery workflow yet.</p>
        <p className="detail">
          Install the catalogue, apply a workflow to a class of work, and start something.
        </p>
      </div>
    );
  }
  return (
    <div className="dw-runs">
      <div className="dw-runs-head">
        <span className="detail">{runs.length} run{runs.length === 1 ? "" : "s"}</span>
        <button type="button" className="cap-btn" onClick={onAdvance} disabled={!!busy}
                data-testid="dw-advance">
          {busy === "Advance" ? "Advancing…" : "Advance now"}
        </button>
      </div>
      {runs.map((run) => {
        const open = openRun === run.id;
        return (
          <article key={run.id} className="dw-run" data-status={run.status} data-testid="dw-run">
            <button type="button" className="dw-run-head" aria-expanded={open}
                    onClick={() => setOpenRun(open ? null : run.id)}>
              <span className="dw-badge" data-tone={tone(run.status)}>{run.status}</span>
              <span className="dw-run-name">{run.workflow_name}</span>
              <span className="detail dw-run-version">v{run.version}</span>
              {/* Progress as a count, not only a bar: "4 of 7" survives a screenshot. */}
              <span className="dw-run-progress">
                {run.steps_done} of {run.step_count}
                <i className="dw-bar" aria-hidden>
                  <i style={{ width: `${Math.round((run.steps_done / Math.max(1, run.step_count)) * 100)}%` }} />
                </i>
              </span>
              {run.waiting_on ? (
                <span className="dw-waiting" data-testid="dw-waiting">
                  waiting on {run.waiting_on}
                </span>
              ) : null}
            </button>
            {run.blocked_reason ? (
              <p className="dw-blocked">{run.blocked_reason}</p>
            ) : null}
            {open ? (
              <ol className="dw-steps">
                {run.steps.map((step) => (
                  <li key={step.step_id} className="dw-step" data-status={step.status}>
                    <span className="dw-badge" data-tone={tone(step.status)}>{step.status}</span>
                    <span className="dw-step-id">{step.step_id}</span>
                    <span className="detail">{step.capability ?? step.kind}</span>
                    {step.approval && (step.approval as { mode?: string }).mode === "manual" ? (
                      <span className="dw-gate" title="Requires a person">
                        gate
                        {(step.approval as { approver_role?: string }).approver_role
                          ? ` · ${(step.approval as { approver_role?: string }).approver_role}`
                          : ""}
                      </span>
                    ) : null}
                    {step.due_at ? (
                      <span className="detail dw-due">due {step.due_at.slice(0, 16).replace("T", " ")}</span>
                    ) : null}
                    {step.timeout_applied ? (
                      <span className="dw-timeout">{step.timeout_applied.replace("_", " ")}</span>
                    ) : null}
                    {step.error ? <p className="dw-step-error">{step.error}</p> : null}
                    {/* What the step PRODUCED. A run view that shows only status makes an
                        operator open five other screens to find the PR it opened. */}
                    {Object.keys(step.output || {}).length > 0 ? (
                      <p className="dw-step-out">
                        {Object.entries(step.output)
                          .filter(([k]) => k !== "capability" && k !== "tool" && k !== "inputs")
                          .slice(0, 4)
                          // `String(v)` on a nested object renders "[object Object]", which
                          // is the least useful string in software. Ids and urls are what an
                          // operator wants from a step's output; a nested object is summarised
                          // by its keys rather than pretended to be a value.
                          .map(([k, v]) => `${k}: ${summarise(v)}`)
                          .join(" · ")}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

/* ── Catalogue ───────────────────────────────────────────────────────────────── */

function CatalogueView({ catalogue, installed, busy, onInstall, onApply, onStart, onFork }: {
  catalogue: CatalogueEntry[];
  installed: Installed[];
  busy: string | null;
  onInstall: () => void;
  onApply: (key: string, workClass: string) => void;
  onStart: (key: string) => void;
  onFork: (key: string) => void;
}) {
  const have = useMemo(() => new Set(installed.map((i) => i.workflow_key)), [installed]);
  return (
    <div className="dw-catalogue">
      <div className="dw-runs-head">
        <span className="detail">
          {have.size} of {catalogue.length} installed in this project
        </span>
        <button type="button" className="cap-btn" onClick={onInstall} disabled={!!busy}
                data-testid="dw-install">
          {busy === "Install the catalogue" ? "Installing…" : "Install all"}
        </button>
      </div>
      <div className="dw-grid">
        {catalogue.map((entry) => (
          <article key={entry.key} className="dw-card" data-testid={`dw-card-${entry.key}`}>
            <header>
              <h4>{entry.name}</h4>
              <span className="dw-class">{entry.work_class}</span>
            </header>
            <p className="detail">{entry.description}</p>
            {/* The steps, in order, so the shape is legible before it is chosen. */}
            <ol className="dw-flow">
              {entry.steps.map((s) => (
                <li key={s.id} data-gate={entry.gates.includes(s.id) || undefined}>
                  {s.capability}
                </li>
              ))}
            </ol>
            <p className="dw-facts">
              <span>{entry.step_count} steps</span>
              <span>~{entry.estimated_minutes} min</span>
              {/* Gates are the number a customer chooses on. */}
              <span data-emphasis={entry.gates.length > 0 || undefined}>
                {entry.gates.length} approval{entry.gates.length === 1 ? "" : "s"}
              </span>
            </p>
            {entry.requires_project.length > 0 ? (
              <p className="detail dw-needs">
                Needs: {entry.requires_project.join(", ")}
              </p>
            ) : null}
            <div className="dw-card-actions">
              {have.has(entry.key) ? (
                <>
                  <button type="button" className="cap-btn" disabled={!!busy}
                          onClick={() => onApply(entry.key, entry.work_class)}
                          data-testid={`dw-apply-${entry.key}`}>
                    Apply to {entry.work_class}
                  </button>
                  <button type="button" className="cap-btn" disabled={!!busy}
                          onClick={() => onStart(entry.key)}>Start one</button>
                  {/* Fork, never edit: the standard stays pristine so "reset" means something. */}
                  <button type="button" className="cap-btn" disabled={!!busy}
                          onClick={() => onFork(entry.key)}
                          data-testid={`dw-fork-${entry.key}`}>Fork to edit</button>
                </>
              ) : (
                <span className="detail">Install the catalogue to use this.</span>
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

/* ── Builder ─────────────────────────────────────────────────────────────────── */

function BuilderView({ capabilities, riskFloor, installed, busy, onSave }: {
  capabilities: Capability[];
  riskFloor: number;
  installed: Installed[];
  busy: string | null;
  onSave: (definitionId: string, spec: Record<string, unknown>) => void;
}) {
  // Only a customer's OWN workflows can be edited. Offering a catalogue one here and refusing
  // on save would be a control that lies about what it does.
  const editable = useMemo(() => installed.filter((i) => !i.is_catalogue), [installed]);
  const [target, setTarget] = useState<string>("");
  const [draft, setDraft] = useState<Draft[]>([]);
  const [dragging, setDragging] = useState<number | null>(null);

  useEffect(() => {
    if (!target && editable.length) setTarget(editable[0].id);
  }, [editable, target]);

  const add = (capability: Capability) =>
    setDraft((current) => [
      ...current,
      {
        // Unique per drop, so adding "build" twice does not produce two steps with one id —
        // which the runtime rejects, at save time, with a message about a duplicate.
        id: `${capability.key}_${current.filter((s) => s.capability === capability.key).length + 1}`,
        capability: capability.key,
        approval: capability.approval_forced_manual ? "manual" : "auto",
      },
    ]);

  const move = (from: number, to: number) =>
    setDraft((current) => {
      if (to < 0 || to >= current.length || from === to) return current;
      const next = [...current];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      return next;
    });

  const spec = useMemo(() => ({
    entry_step: draft[0]?.id,
    steps: draft.map((step, index) => {
      const capability = capabilities.find((c) => c.key === step.capability);
      return {
        id: step.id,
        kind: "capability",
        binding: {
          capability: step.capability,
          // Required inputs are read from the run's input by name. The builder does not invent
          // values — a workflow that hard-codes the thing being built only works once.
          inputs: Object.fromEntries(
            (capability?.required_inputs ?? []).map((name) => [name, { from_input: name }]),
          ),
        },
        approval: { mode: step.approval },
        next: index + 1 < draft.length ? [draft[index + 1].id] : [],
      };
    }),
  }), [draft, capabilities]);

  return (
    <div className="dw-builder">
      <aside className="dw-palette" aria-label="Steps you can add">
        <h4>Steps</h4>
        {capabilities.map((c) => (
          <button key={c.key} type="button" className="dw-palette-item"
                  onClick={() => add(c)} disabled={!!busy}
                  data-testid={`dw-palette-${c.key}`}
                  title={`${c.summary}${c.requires_project.length ? ` · needs ${c.requires_project.join(", ")}` : ""}`}>
            <span className="dw-palette-label">{c.label}</span>
            {/* Risk is shown in the palette, before the step is placed. Finding out at save
                time that a step can never be automatic is finding out too late. */}
            {c.approval_forced_manual ? (
              <span className="dw-gate">always approved</span>
            ) : null}
            <span className="detail dw-palette-summary">{c.summary}</span>
          </button>
        ))}
        <p className="detail dw-floor">
          Anything at risk tier {riskFloor} or above always requires a person. That is not
          configurable.
        </p>
      </aside>

      <section className="dw-canvas" aria-label="Your workflow">
        <div className="dw-canvas-head">
          <label className="field">
            <span>Editing</span>
            <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}
                    data-testid="dw-target">
              {editable.length === 0 ? <option value="">Fork a workflow first</option> : null}
              {editable.map((i) => (
                <option key={i.id} value={i.id}>{i.name}</option>
              ))}
            </select>
          </label>
          <button type="button" className="cap-btn primary"
                  disabled={!!busy || !target || draft.length === 0}
                  onClick={() => onSave(target, spec)} data-testid="dw-save">
            {busy === "Save" ? "Saving…" : `Save as a new version`}
          </button>
        </div>

        {draft.length === 0 ? (
          <p className="detail dw-canvas-empty">
            Add steps from the left. Drag them to reorder — the order is the order they run in.
          </p>
        ) : (
          <ol className="dw-draft">
            {draft.map((step, index) => {
              const capability = capabilities.find((c) => c.key === step.capability);
              return (
                <li key={step.id} className="dw-draft-step" draggable
                    onDragStart={() => setDragging(index)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => { if (dragging !== null) move(dragging, index); setDragging(null); }}
                    data-testid={`dw-draft-${step.id}`}>
                  <span className="dw-draft-grip" aria-hidden>⋮⋮</span>
                  <span className="dw-draft-n">{index + 1}</span>
                  <span className="dw-draft-label">{capability?.label ?? step.capability}</span>
                  <select className="input dw-draft-approval" value={step.approval}
                          disabled={capability?.approval_forced_manual}
                          onChange={(e) => setDraft((c) =>
                            c.map((s, i) => (i === index ? { ...s, approval: e.target.value } : s)))}
                          aria-label={`Approval for ${step.id}`}>
                    <option value="auto">auto</option>
                    <option value="manual">manual</option>
                  </select>
                  {/* Keyboard equivalents for the drag. A reorder control reachable only by
                      pointer is a builder half the operators cannot use. */}
                  <span className="dw-draft-moves">
                    <button type="button" className="cap-btn" aria-label={`Move ${step.id} earlier`}
                            onClick={() => move(index, index - 1)} disabled={index === 0}>↑</button>
                    <button type="button" className="cap-btn" aria-label={`Move ${step.id} later`}
                            onClick={() => move(index, index + 1)}
                            disabled={index === draft.length - 1}>↓</button>
                    <button type="button" className="cap-btn" aria-label={`Remove ${step.id}`}
                            onClick={() => setDraft((c) => c.filter((_, i) => i !== index))}>×</button>
                  </span>
                </li>
              );
            })}
          </ol>
        )}
      </section>
    </div>
  );
}
