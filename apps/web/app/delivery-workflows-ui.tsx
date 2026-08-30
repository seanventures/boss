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

/** A step on the canvas. `kind` is "capability" for the work steps and a control kind
 *  ("timer", "decision", "subworkflow") for the blocks the runtime has always supported. */
type Draft = {
  id: string;
  kind: string;
  capability?: string;
  approval?: string;
  config?: Record<string, unknown>;
};

type Block = {
  kind: string;
  label: string;
  summary: string;
  canvas_required: boolean;
  defaults?: Record<string, unknown>;
  configure?: { field: string; label: string; type: string }[];
};

type WorkflowSpec = {
  definition_id: string;
  workflow_key: string;
  name: string;
  is_catalogue: boolean;
  version: number;
  spec: { entry_step?: string; steps: Record<string, unknown>[] };
  forked_from: { workflow_key: string; name: string; version: number;
                 spec: { steps: Record<string, unknown>[] } } | null;
};

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
  const [blocks, setBlocks] = useState<Block[]>([]);
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
  // Lifted out of the builder so forking can land the operator on the thing they just made.
  const [builderTarget, setBuilderTarget] = useState<string>("");

  const load = useCallback(async () => {
    const [caps, cat, inst, live] = await Promise.all([
      get<{ capabilities: Capability[]; blocks: Block[]; manual_approval_risk_floor: number }>(
        "capabilities", projectId),
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
    setBlocks((caps as { blocks?: Block[] }).blocks ?? []);
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
          onFork={async (key) => {
            const entry = catalogue.find((c) => c.key === key);
            // A UNIQUE KEY PER FORK.
            //
            // `${key}_custom` collided the second time anyone forked the same workflow, and a
            // 409 with the notice scrolled off the top of a long page is indistinguishable
            // from a button that does nothing — which is exactly how this was reported.
            const taken = new Set(installed.map((i) => i.workflow_key));
            let candidate = `${key}_custom`;
            for (let n = 2; taken.has(candidate); n += 1) candidate = `${key}_custom_${n}`;
            const result = await act(`Fork ${entry?.name ?? key}`, "fork", {
              workflow_key: key,
              new_key: candidate,
              new_name: `${entry?.name ?? key} (yours)`,
            });
            // IT IS CALLED "FORK TO EDIT", SO GO AND EDIT IT.
            //
            // On success the only visible change was a line of text at the top of the page:
            // the new workflow appears in the BUILDER's dropdown, on a tab the operator is not
            // looking at. Landing them in the editor with it selected is what the button says
            // it does.
            if (result.ok) {
              const created = (result.data as { definition?: { id: string } })?.definition;
              if (created?.id) setBuilderTarget(created.id);
              setTab("builder");
            }
          }}
        />
      ) : (
        <BuilderView
          capabilities={capabilities} blocks={blocks} riskFloor={riskFloor}
          installed={installed} projectId={projectId}
          busy={busy} target={builderTarget} setTarget={setBuilderTarget}
          onSave={(definitionId, spec) => act("Save", "save", { definition_id: definitionId, spec })}
          onCreate={async (name) => {
            const result = await act(`Create ${name}`, "create", { name });
            // Select what was just made. Creating something and leaving the operator to find it
            // in a dropdown is the same failure "fork to edit" had.
            if (result.ok) {
              const created = (result.data as { definition?: { id: string } })?.definition;
              if (created?.id) setBuilderTarget(created.id);
            }
          }}
          onImport={async (sourceKey, name) => {
            const result = await act(`Import ${name}`, "fork", {
              workflow_key: sourceKey, new_name: name,
            });
            if (result.ok) {
              const created = (result.data as { definition?: { id: string } })?.definition;
              if (created?.id) setBuilderTarget(created.id);
            }
          }}
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
  // Only the CATALOGUE ones count toward "installed". `installed` also holds the customer's own
  // forks, so counting all of them read "14 of 12 installed in this project" — a number that
  // cannot be true and makes the reader doubt everything else on the card.
  const have = useMemo(
    () => new Set(installed.filter((i) => i.is_catalogue).map((i) => i.workflow_key)),
    [installed],
  );
  const mine = useMemo(() => installed.filter((i) => !i.is_catalogue).length, [installed]);
  return (
    <div className="dw-catalogue">
      <div className="dw-runs-head">
        <span className="detail">
          {have.size} of {catalogue.length} standard workflows installed
          {mine > 0 ? ` · ${mine} of your own` : ""}
        </span>
        <button type="button" className="cap-btn" onClick={onInstall} disabled={!!busy}
                data-testid="dw-install">
          {busy === "Install the catalogue" ? "Installing…" : "Install all"}
        </button>
      </div>
      <div className="dw-grid">
        {catalogue.map((entry) => (
          <article key={entry.key} className="dw-card" data-class={entry.work_class}
                   data-testid={`dw-card-${entry.key}`}>
            <header className="dw-card-head">
              <h4>{entry.name}</h4>
              {/* The class of work this is FOR, as a chip rather than grey mono text. It is
                  what a customer matches against when choosing, so it should be findable at a
                  glance across twelve cards. */}
              <span className="dw-class" data-class={entry.work_class}>{entry.work_class}</span>
            </header>
            <p className="detail dw-card-desc">{entry.description}</p>

            {/* THE STEPS ARE A SEQUENCE, AND A WRAPPED LIST OF CHIPS SAYS THEY ARE A SET.
                Order is the whole meaning of a workflow, so the card draws it as a rail: each
                step joined to the next, gates marked, and the shape readable before anyone
                commits to it. */}
            <ol className="dw-rail" aria-label="Steps, in order">
              {entry.steps.map((step, index) => {
                const gate = entry.gates.includes(step.id);
                return (
                  <li key={step.id} className="dw-rail-step" data-gate={gate || undefined}>
                    <span className="dw-rail-dot" aria-hidden />
                    <span className="dw-rail-label">{step.capability}</span>
                    {index < entry.steps.length - 1 ? (
                      <span className="dw-rail-link" aria-hidden />
                    ) : null}
                  </li>
                );
              })}
            </ol>

            {/* Three numbers a customer chooses on, each with a label — "10 · ~112 min · 2" is
                unreadable without them. */}
            <dl className="dw-stats">
              <div><dt>Steps</dt><dd>{entry.step_count}</dd></div>
              <div><dt>Typically</dt><dd>{entry.estimated_minutes}<small>min</small></dd></div>
              <div data-emphasis={entry.gates.length > 0 || undefined}>
                <dt>Approvals</dt><dd>{entry.gates.length}</dd>
              </div>
            </dl>

            {entry.requires_project.length > 0 ? (
              <p className="dw-needs">
                <span className="dw-needs-label">Needs</span>
                {entry.requires_project.map((need) => (
                  <span key={need} className="dw-need">{need.replace(/_/g, " ")}</span>
                ))}
              </p>
            ) : <p className="dw-needs dw-needs-none">Nothing to set up first</p>}

            <div className="dw-card-actions">
              {have.has(entry.key) ? (
                <>
                  {/* One primary action. Three equal buttons make a customer read all three
                      every time to find the one they want. */}
                  <button type="button" className="cap-btn primary" disabled={!!busy}
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

function BuilderView({ capabilities, blocks, riskFloor, installed, projectId, busy,
                      target, setTarget, onSave, onCreate, onImport }: {
  capabilities: Capability[];
  blocks: Block[];
  riskFloor: number;
  installed: Installed[];
  projectId: string;
  busy: string | null;
  target: string;
  setTarget: (id: string) => void;
  onSave: (definitionId: string, spec: Record<string, unknown>) => void;
  onCreate: (name: string) => void;
  onImport: (sourceKey: string, name: string) => void;
}) {
  // Only a customer's OWN workflows can be edited. Offering a catalogue one here and refusing
  // on save would be a control that lies about what it does.
  const editable = useMemo(() => installed.filter((i) => !i.is_catalogue), [installed]);
  const [draft, setDraft] = useState<Draft[]>([]);
  const [dragging, setDragging] = useState<number | null>(null);
  // What the selected workflow currently IS, and what it was forked from.
  const [loaded, setLoaded] = useState<WorkflowSpec | null>(null);
  const [loadingSpec, setLoadingSpec] = useState(false);
  const [specError, setSpecError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [importFrom, setImportFrom] = useState("");

  useEffect(() => {
    if (!target && editable.length) setTarget(editable[0].id);
  }, [editable, target, setTarget]);

  /** Turn a stored spec into canvas steps. */
  const toDraft = useCallback((steps: Record<string, unknown>[]): Draft[] =>
    steps.map((step) => {
      const binding = (step.binding ?? {}) as Record<string, unknown>;
      const approval = (step.approval ?? {}) as Record<string, unknown>;
      return {
        id: String(step.id),
        kind: String(step.kind ?? "capability"),
        capability: binding.capability ? String(binding.capability) : undefined,
        approval: approval.mode ? String(approval.mode) : "auto",
        // Everything that is not id/kind/binding/approval/next is the block's own
        // configuration — wait_seconds, workflow_version_id — and must survive a round trip
        // or editing one step would quietly reset another.
        config: Object.fromEntries(
          Object.entries(step).filter(([k]) =>
            !["id", "kind", "binding", "approval", "next"].includes(k)),
        ),
      };
    }), []);

  // OPEN WITH WHAT IS THERE.
  //
  // The builder started empty, so changing one step of a ten-step workflow meant rebuilding
  // the other nine from memory — and saving would have silently replaced the workflow with
  // whatever partial thing had been assembled.
  // KEYED ON THE WORKFLOW, NOT ON THE ARRAY.
  //
  // `editable` is derived from `installed`, which the eight-second poll replaces with a new
  // array every time — so depending on it re-ran this effect constantly, and any hiccup in the
  // refetch cleared the canvas. A customer editing a ten-step workflow could lose it because a
  // background poll happened to fail. The KEY is what actually identifies what to load.
  const targetKey = useMemo(
    () => editable.find((i) => i.id === target)?.workflow_key ?? "",
    [editable, target],
  );

  useEffect(() => {
    if (!targetKey) { setLoaded(null); setDraft([]); return; }
    let live = true;
    setLoadingSpec(true);
    void (async () => {
      const params = new URLSearchParams({
        view: "spec", project_id: projectId, key: targetKey,
      });
      try {
        const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
        const body = (await r.json()) as WorkflowSpec & { error?: string };
        if (!live) return;
        if (r.ok && body?.spec?.steps) {
          setLoaded(body);
          setDraft(toDraft(body.spec.steps));
        }
        // NO `else` THAT CLEARS. A failed refetch is a reason to keep what is on the canvas,
        // not to throw it away — the steps already there are the customer's work, and an
        // empty canvas after a network blip is indistinguishable from having deleted it.
        else {
          setSpecError(
            (body as { error?: string })?.error ?? "could not read the current steps",
          );
        }
      } catch (e) {
        if (live) setSpecError(String((e as Error)?.message ?? e));
      } finally {
        if (live) setLoadingSpec(false);
      }
    })();
    return () => { live = false; };
  }, [targetKey, projectId, toDraft]);

  /** Put it back the way the standard has it. */
  const reset = useCallback(() => {
    if (!loaded?.forked_from) return;
    setDraft(toDraft(loaded.forked_from.spec.steps));
  }, [loaded, toDraft]);

  /** Unique per drop. Adding "build" twice with one id is rejected by the runtime, at save
   *  time, after a whole workflow has been laid out. */
  const nextId = (current: Draft[], stem: string) => {
    const taken = new Set(current.map((s) => s.id));
    let n = current.filter((s) => s.id.startsWith(`${stem}_`)).length + 1;
    while (taken.has(`${stem}_${n}`)) n += 1;
    return `${stem}_${n}`;
  };

  const add = (capability: Capability) =>
    setDraft((current) => [...current, {
      id: nextId(current, capability.key),
      kind: "capability",
      capability: capability.key,
      approval: capability.approval_forced_manual ? "manual" : "auto",
      config: {},
    }]);

  /** A control block: wait, an approval gate, or another workflow. */
  const addBlock = (block: Block) =>
    setDraft((current) => [...current, {
      id: nextId(current, block.kind),
      kind: block.kind,
      approval: "auto",
      config: { ...(block.defaults ?? {}) },
    }]);

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
      const next = index + 1 < draft.length ? [draft[index + 1].id] : [];
      if (step.kind !== "capability") {
        // A control block carries its own configuration at the top level, which is where the
        // runtime looks for `wait_seconds` and `workflow_version_id`.
        return { id: step.id, kind: step.kind, binding: {}, ...(step.config ?? {}), next };
      }
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
        next,
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

        {/* CONTROL BLOCKS. The runtime has supported these since it was written and the
            builder never offered them, so a customer could only ever author a straight line.
            The ones that need somewhere to draw a branch are listed but not placeable — a
            palette entry that produces a spec the runtime rejects is a promise the product
            cannot keep. */}
        <h4 className="dw-palette-heading">Flow</h4>
        {blocks.filter((b) => !b.canvas_required).map((b) => (
          <button key={b.kind} type="button" className="dw-palette-item"
                  onClick={() => addBlock(b)} disabled={!!busy}
                  data-testid={`dw-block-${b.kind}`} title={b.summary}>
            <span className="dw-palette-label">{b.label}</span>
            <span className="detail dw-palette-summary">{b.summary}</span>
          </button>
        ))}
        {blocks.some((b) => b.canvas_required) ? (
          <details className="dw-soon">
            <summary className="detail">
              {blocks.filter((b) => b.canvas_required).length} more, once the canvas can draw
              branches
            </summary>
            {blocks.filter((b) => b.canvas_required).map((b) => (
              <p key={b.kind} className="detail dw-soon-item">
                <strong>{b.label}</strong> — {b.summary}
              </p>
            ))}
          </details>
        ) : null}
      </aside>

      <section className="dw-canvas" aria-label="Your workflow">
        <div className="dw-canvas-head">
          <label className="field">
            <span>Editing</span>
            <select className="input" value={target} onChange={(e) => setTarget(e.target.value)}
                    data-testid="dw-target">
              {/* The tab is called "Build your own", and until now the only way in was to fork
                  something — so a screen that promised authorship offered only editing. */}
              {editable.length === 0
                ? <option value="">Nothing of your own yet</option>
                : null}
              {editable.map((i) => (
                <option key={i.id} value={i.id}>{i.name}</option>
              ))}
            </select>
          </label>

          {creating ? (
            <form className="dw-new" onSubmit={(e) => {
              e.preventDefault();
              const name = newName.trim();
              if (!name) return;
              // IMPORT IS A FORK WITH A NAME YOU CHOSE.
              //
              // Starting from an existing workflow and starting from nothing differ only in
              // where the steps come from, and forking already copies steps AND records what
              // they came from — which is what makes "reset to the standard" work on the
              // result. A separate import path would have been a second, worse fork.
              if (importFrom) onImport(importFrom, name);
              else onCreate(name);
              setNewName("");
              setImportFrom("");
              setCreating(false);
            }}>
              <input className="input" autoFocus value={newName} placeholder="Name it"
                     onChange={(e) => setNewName(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Escape") setCreating(false); }}
                     aria-label="Name for the new workflow" data-testid="dw-new-name" />
              <label className="field dw-new-from">
                <span>Start from</span>
                <select className="input" value={importFrom} data-testid="dw-new-from"
                        onChange={(e) => setImportFrom(e.target.value)}>
                  <option value="">Nothing — an empty workflow</option>
                  {/* Every workflow in the project, not only the customer's own: importing a
                      standard is the common case, and it is what "work on top of it" means. */}
                  {installed.map((i) => (
                    <option key={i.id} value={i.workflow_key}>
                      {i.name}{i.is_catalogue ? " (standard)" : ""}
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit" className="cap-btn" disabled={!!busy || !newName.trim()}
                      data-testid="dw-new-confirm">
                {importFrom ? "Import" : "Create"}
              </button>
              <button type="button" className="cap-btn"
                      onClick={() => { setCreating(false); setNewName(""); setImportFrom(""); }}>
                Cancel
              </button>
            </form>
          ) : (
            <button type="button" className="cap-btn" disabled={!!busy}
                    onClick={() => setCreating(true)} data-testid="dw-new">
              + New workflow
            </button>
          )}
          <span className="dw-canvas-actions">
            {/* RESET TO THE STANDARD. Only offered when there is something to reset TO — a
                fork records what it came from, and a workflow written from scratch has no
                original, so the control would be a lie on one of them. */}
            {loaded?.forked_from ? (
              <button type="button" className="cap-btn" disabled={!!busy}
                      onClick={reset} data-testid="dw-reset"
                      title={`Put the steps back the way ${loaded.forked_from.name} has them`}>
                Reset to the standard
              </button>
            ) : null}
            <button type="button" className="cap-btn primary"
                    disabled={!!busy || !target || draft.length === 0}
                    onClick={() => onSave(target, spec)} data-testid="dw-save">
              {busy === "Save" ? "Saving…" : `Save as a new version`}
            </button>
          </span>
        </div>

        {specError ? (
          <p className="detail dw-spec-error" data-testid="dw-spec-error">
            Could not re-read the saved steps — {specError}. What is on the canvas is still
            yours; saving will publish it.
          </p>
        ) : null}
        {loadingSpec && draft.length === 0 ? (
          <p className="detail dw-canvas-empty" data-testid="dw-canvas-loading">
            Reading the current steps…
          </p>
        ) : draft.length === 0 ? (
          <p className="detail dw-canvas-empty">
            {target
              ? "No steps yet — add them from the left, in the order they should run."
              : "Start a new workflow, or fork one of the standards to change it."}
          </p>
        ) : (
          <ol className="dw-draft">
            {draft.map((step, index) => {
              const capability = capabilities.find((c) => c.key === step.capability);
              return (
                <li key={step.id} className="dw-draft-step" draggable
                    data-dragging={dragging === index || undefined}
                    data-drop={dragging !== null && dragging !== index ? "true" : undefined}
                    onDragStart={() => setDragging(index)}
                    onDragEnd={() => setDragging(null)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => { if (dragging !== null) move(dragging, index); setDragging(null); }}
                    data-testid={`dw-draft-${step.id}`}>
                  <span className="dw-draft-grip" aria-hidden>⋮⋮</span>
                  <span className="dw-draft-n">{index + 1}</span>
                  <span className="dw-draft-label">
                    {step.kind === "capability"
                      ? capability?.label ?? step.capability
                      : blocks.find((b) => b.kind === step.kind)?.label ?? step.kind}
                  </span>
                  {/* A control block is visibly a different sort of thing from a work step. */}
                  {step.kind !== "capability" ? (
                    <span className="dw-draft-kind">flow</span>
                  ) : null}
                  <select className="input dw-draft-approval" value={step.approval}
                          disabled={step.kind !== "capability"
                                    || capability?.approval_forced_manual}
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
