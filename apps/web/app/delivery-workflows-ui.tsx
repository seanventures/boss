"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";

import { resolveDecisionAction } from "./actions";

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

/** A step this customer defined, bound to one of their own skills or MCP tools. The risk
 *  tier is RESOLVED from whatever it binds — it is never something the customer typed. */
type CustomStep = {
  step_key: string;
  label: string;
  summary: string;
  binding_kind: "skill" | "mcp_tool";
  skill_id: string | null;
  mcp_tool_name: string | null;
  inputs: { name: string; required: boolean; description: string }[];
  produces: string[];
  enabled: boolean;
  risk_tier: number;
  risk_source: string | null;
  trusted: boolean;
  usable: boolean;
  unusable_reason: string | null;
  approval_forced_manual: boolean;
};

type Bindable = {
  skills: { skill_id: string; risk_tier: number; trusted: boolean; unclassified: boolean }[];
  mcp_servers: {
    id: string; name: string; risk_tier: number; trusted: boolean; reachable: boolean;
    error?: string;
    tools: { name: string; description: string; risk_tier: number }[];
  }[];
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
  // The API has returned these since the runs endpoint was written; the type omitted them, so
  // the one question every operator asks of a pipeline — how long did this take — could not be
  // answered from data already on the page.
  started_at: string | null;
  completed_at: string | null;
  // The gate's decision, which is what makes Approve/Deny answerable here rather than only in
  // the Approvals area.
  decision_id: string | null;
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
  /** What the run was started with — `title` is the project it is delivering. */
  input?: Record<string, unknown> | null;
  created_at?: string | null;
};

/** The project this run is delivering. Five runs of "Standard Product Delivery" are five
 *  identical rows unless the thing being delivered is on the row. */
function runSubject(run: Run): string | null {
  const title = (run.input ?? {})["title"];
  return typeof title === "string" && title.trim() ? title.trim() : null;
}

const LIVE = ["running", "waiting", "queued", "validating", "leased"];

/** What a step observed from the build, rather than started itself: the review seats and the
 *  quality gates the governed pipeline actually recorded. Without this, `review` and `test` were
 *  two rows that said only "succeeded" about the checks an operator most wants to read. */
function observedGates(step: RunStep): string | null {
  const out = (step.output ?? {}) as {
    status?: string; gates_passed?: unknown; targets?: unknown;
  };
  if (out.status !== "observed") return null;
  // The monitoring step reports targets rather than gates: which apps are under health probing
  // and whether a probe has run yet. "2 watched · app (production)" answers the question the
  // step exists to answer.
  if (Array.isArray(out.targets)) {
    const names = out.targets
      .map((t) => (t as { app_name?: string; environment?: string }))
      .map((t) => [t.app_name, t.environment].filter(Boolean).join(" · "))
      .filter(Boolean);
    return names.length ? `watching ${names.join(", ")}` : null;
  }
  const gates = Array.isArray(out.gates_passed) ? out.gates_passed.map(String) : [];
  return gates.length ? gates.join(" · ") : null;
}

/** A step the runtime completed by recording that its phase never happened. */
function notApplicable(step: RunStep): boolean {
  return (step.output as { status?: string } | undefined)?.status === "not_applicable";
}

/** Live runs first, newest first within each group. Sorting by `updated_at` alone floats a
 *  blocked run from last week above the project that started a minute ago, purely because
 *  something touched it more recently — which is how the page came to show an expired run
 *  while a build was in flight. */
function byLiveness(a: Run, b: Run): number {
  const live = (r: Run) => (LIVE.includes(r.status) ? 0 : 1);
  if (live(a) !== live(b)) return live(a) - live(b);
  return (b.created_at ?? b.updated_at ?? "").localeCompare(a.created_at ?? a.updated_at ?? "");
}

/** A step on the canvas. `kind` is "capability" for the work steps and a control kind
 *  ("timer", "decision", "subworkflow") for the blocks the runtime has always supported. */
type Draft = {
  id: string;
  kind: string;
  capability?: string;
  /** Set instead of `capability` when the step is one the customer defined. */
  customStep?: string;
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

/**
 * The builder's glyphs.
 *
 * Same approach as the nav's: inline SVG on a 16px grid, `currentColor` throughout, so an icon
 * dims and brightens with the label beside it instead of needing a second set of active rules.
 * A whole icon package for eleven glyphs is a lot of bytes for eleven glyphs.
 *
 * Each says what the control DOES rather than decorating it — import is an arrow landing in a
 * tray, reset is an arrow going back, create is a check. The words stay: an icon-only control
 * in a builder is a guessing game.
 */
const WF_ICONS: Record<string, React.ReactNode> = {
  // Editing — a pencil on a line.
  edit: <><path d="M11.3 2.2a1.7 1.7 0 0 1 2.4 2.4L6 12.3l-3.2.9.9-3.2 7.6-7.8Z" /><path d="M10.2 3.4l2.4 2.4" /></>,
  // New — a plus, boxed, because it makes a new thing rather than adding to this one.
  plus: <><rect x="1.8" y="1.8" width="12.4" height="12.4" rx="3" /><path d="M8 5.2v5.6M5.2 8h5.6" /></>,
  // Import — steps landing on the canvas.
  importIn: <><path d="M8 1.8v7.4" /><path d="M5.2 6.6 8 9.4l2.8-2.8" /><path d="M2.4 10.6v1.8a1.8 1.8 0 0 0 1.8 1.8h7.6a1.8 1.8 0 0 0 1.8-1.8v-1.8" /></>,
  // Import again — the same arrow, going round.
  again: <><path d="M13.6 8a5.6 5.6 0 1 1-1.7-4" /><path d="M13.8 1.9v3.4h-3.4" /></>,
  // Create — it exists now.
  check: <><path d="M2.6 8.4 6.2 12l7.2-8" /></>,
  // Cancel.
  x: <><path d="M4 4l8 8M12 4l-8 8" /></>,
  // Save a new version — a document with a tick.
  save: <><path d="M3.2 2.4h6.2l3.4 3.4v7.8a1 1 0 0 1-1 1H3.2a1 1 0 0 1-1-1V3.4a1 1 0 0 1 1-1Z" /><path d="M9.2 2.4v3.6h3.6" /><path d="M5.4 10.2 7 11.8l3-3.4" /></>,
  // Reset — back to how it was.
  reset: <><path d="M2.4 8a5.6 5.6 0 1 0 1.7-4" /><path d="M2.2 1.9v3.4h3.4" /></>,
  // A fact, not a warning.
  info: <><circle cx="8" cy="8" r="6.2" /><path d="M8 7.4v3.4M8 5.2h.01" /></>,
  // Steps — the shape the canvas draws.
  steps: <><rect x="2.2" y="2.2" width="11.6" height="3.4" rx="1.2" /><rect x="2.2" y="10.4" width="11.6" height="3.4" rx="1.2" /><path d="M8 5.6v4.8" /></>,
  // An empty canvas.
  empty: <><rect x="1.8" y="3" width="12.4" height="10" rx="2" strokeDasharray="2.6 2.2" /><path d="M8 6.6v2.8M6.6 8h2.8" /></>,
};

function WfIcon({ id }: { id: keyof typeof WF_ICONS | string }) {
  const d = WF_ICONS[id];
  // No placeholder box for an unknown id: an empty square beside a label reads as a broken
  // image, while the label on its own reads as a control without an icon, which is what it is.
  if (!d) return null;
  return (
    <svg className="dw-icon" viewBox="0 0 16 16" width="15" height="15" fill="none"
         stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden focusable="false">
      {d}
    </svg>
  );
}

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
  const [customSteps, setCustomSteps] = useState<CustomStep[]>([]);
  const [bindable, setBindable] = useState<Bindable>({ skills: [], mcp_servers: [] });
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
  // WHAT WAS JUST MADE, so the builder can SAY it was made.
  //
  // Import worked and looked like it had not: the form closed, the toolbar came back looking
  // exactly as before, and the only confirmation was a sticky line 104px up the page that is
  // easy to miss entirely. Returning to a toolbar that looks unchanged reads as "cancelled",
  // which is how a working feature got reported as broken.
  const [justMade, setJustMade] = useState<{ id: string; name: string; from?: string } | null>(null);

  const load = useCallback(async () => {
    const [caps, cat, inst, live, mine] = await Promise.all([
      get<{ capabilities: Capability[]; blocks: Block[]; manual_approval_risk_floor: number }>(
        "capabilities", projectId),
      get<{ items: CatalogueEntry[] }>("catalogue", projectId),
      get<{ items: Installed[] }>("installed", projectId),
      get<{ runs: Run[] }>("runs", projectId),
      get<{ steps: CustomStep[]; bindable: Bindable }>("custom_steps", projectId),
    ]);
    const failure = [caps, cat, inst, live, mine].find((r) => "error" in r) as { error: string } | undefined;
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
    setCustomSteps((mine as { steps?: CustomStep[] }).steps ?? []);
    setBindable((mine as { bindable?: Bindable }).bindable
                ?? { skills: [], mcp_servers: [] });
    setState("ready");
    setError(null);
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  // KEEP ASKING, EVEN WHEN NOTHING IS RUNNING.
  //
  // This used to stop polling when no run was live — "an idle console is idle". But a console
  // cannot know that work has STARTED without asking: a project kicked off from the Command
  // Center, from the Factory form, or by approving a PRD creates its run somewhere else
  // entirely. Anyone who had this page open when the only run was blocked saw a frozen list
  // and no sign of the project they had just started, because the page never asked again.
  //
  // Fast while something is in flight, slower when idle — idle still means "watch for work
  // appearing", not "stop looking".
  const anyLive = runs.some((r) => LIVE.includes(r.status));
  useEffect(() => {
    const t = window.setInterval(() => { void load(); }, anyLive ? 8000 : 15000);
    return () => window.clearInterval(t);
  }, [anyLive, load]);

  // OPEN THE RUN THE OPERATOR WAS SENT HERE TO SEE. Creating a project redirects here with
  // `?wfr=<id>`; expanding it on arrival is the difference between "your project is somewhere
  // in this list" and showing them the steps.
  useEffect(() => {
    const read = () => {
      const wanted = new URLSearchParams(window.location.search).get("wfr");
      if (wanted) setOpenRun(wanted);
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

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
          customSteps={customSteps} bindable={bindable}
          justMade={justMade} onDismissJustMade={() => setJustMade(null)}
          onDefineStep={async (fields) => {
            const result = await act(`Define ${fields.label}`, "define_step", fields);
            if (result.ok) await load();
            return result;
          }}
          onRemoveStep={async (stepKey) => {
            setBusy(`Remove ${stepKey}`);
            try {
              const r = await fetch(
                `/api/delivery-workflows?project_id=${encodeURIComponent(projectId)}`
                  + `&step_key=${encodeURIComponent(stepKey)}`
                  + `&actor=${encodeURIComponent(actor)}`,
                { method: "DELETE" },
              );
              const body = await r.json();
              if (!r.ok) { setError(body?.error ?? `API returned ${r.status}`); return; }
              setNotice(`Removed ${stepKey}`);
              await load();
            } finally {
              setBusy(null);
            }
          }}
          busy={busy} target={builderTarget} setTarget={setBuilderTarget}
          onSave={(definitionId, spec) => act("Save", "save", { definition_id: definitionId, spec })}
          onCreate={async (name, spec, fromKey) => {
            // TWO CALLS, AND THE ORDER MATTERS.
            //
            // When the steps came from somewhere, `fork` is what records where — which is the
            // only thing that makes "Reset to the standard" mean anything later. Fork copies
            // the SOURCE's steps though, so whatever the operator changed on the canvas is
            // written straight after as the next version. Starting from nothing needs no
            // lineage, so it is a plain create.
            const result = fromKey
              ? await act(`Create ${name}`, "fork", { workflow_key: fromKey, new_name: name })
              : await act(`Create ${name}`, "create", { name });
            if (!result.ok) return;
            const created = (result.data as { definition?: { id: string } })?.definition;
            if (!created?.id) return;

            const steps = (spec as { steps?: unknown[] })?.steps ?? [];
            if (steps.length) {
              // Silent on a no-op: forking already stored these exact steps, and the runtime
              // answers an identical spec with "unchanged" rather than a new version.
              await act(`Save ${name}`, "save", { definition_id: created.id, spec });
            }
            setBuilderTarget(created.id);
            setJustMade({
              id: created.id, name,
              from: fromKey
                ? installed.find((i) => i.workflow_key === fromKey)?.name
                : undefined,
            });
          }}
        />
      )}
    </div>
  );
}

/* ── Running ─────────────────────────────────────────────────────────────────── */

/** How long a step took, or has been going. The single most asked question of a pipeline. */
function stepDuration(step: { started_at?: string | null; completed_at?: string | null }): string {
  if (!step.started_at) return "";
  const start = Date.parse(step.started_at);
  const end = step.completed_at ? Date.parse(step.completed_at) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const secs = Math.round((end - start) / 1000);
  const text = secs < 60 ? `${secs}s`
    : secs < 3600 ? `${Math.floor(secs / 60)}m ${secs % 60}s`
    : `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  return step.completed_at ? text : `${text}…`;
}

/**
 * Where the thing a step produced actually lives.
 *
 * A run view that shows only a status makes an operator open five other screens to find the PR
 * it opened or the panel it convened. The ids are already in the step's output; these turn them
 * into somewhere to go.
 */
function stepLinks(output: Record<string, unknown>): Array<{ label: string; href: string; external?: boolean }> {
  const out: Array<{ label: string; href: string; external?: boolean }> = [];
  const str = (k: string) => (typeof output[k] === "string" ? (output[k] as string) : "");
  const awaited = (output.await as { kind?: string; id?: string } | undefined) ?? {};

  const codeTask = str("code_task_run_id") || (awaited.kind === "code_task" ? awaited.id ?? "" : "")
    || (output.capability === "build" ? str("run_id") : "");
  if (codeTask) {
    out.push({ label: "open the project", href: `?area=code_tasks&run=${encodeURIComponent(codeTask)}` });
    // Debugging a step means reading what it actually did, filtered to this work.
    out.push({ label: "activity log", href: `?area=activity&run=${encodeURIComponent(codeTask)}` });
    out.push({ label: "Ask BOSS", href: `?area=code_tasks&run=${encodeURIComponent(codeTask)}&agent=${encodeURIComponent(codeTask)}` });
  }
  const meeting = str("meeting_run_id") || (awaited.kind === "meeting" ? awaited.id ?? "" : "");
  if (meeting) out.push({ label: "open the panel", href: `?area=meetings` });
  const discovery = str("discovery_id") || (awaited.kind === "discovery" ? awaited.id ?? "" : "");
  if (discovery) out.push({ label: "open the research", href: `?area=discovery` });
  const pr = str("pull_request_url");
  if (pr) out.push({ label: "pull request", href: pr, external: true });
  const preview = str("preview_url");
  if (preview) out.push({ label: "preview", href: preview, external: true });
  return out;
}

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
      {[...runs].sort(byLiveness).map((run) => {
        const open = openRun === run.id;
        return (
          <article key={run.id} className="dw-run" data-status={run.status} data-testid="dw-run">
            <button type="button" className="dw-run-head" aria-expanded={open}
                    onClick={() => setOpenRun(open ? null : run.id)}>
              <span className="dw-badge" data-tone={tone(run.status)}>{run.status}</span>
              <span className="dw-run-name">
                {runSubject(run) ?? run.workflow_name}
                {runSubject(run) ? (
                  <em className="dw-run-wf" data-testid="dw-run-workflow">
                    {run.workflow_name} v{run.version}
                  </em>
                ) : null}
              </span>
              {runSubject(run) ? null : (
                <span className="detail dw-run-version">v{run.version}</span>
              )}
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
                    {/* A PHASE THAT DID NOT HAPPEN IS NOT A PHASE THAT SUCCEEDED. A project
                        started directly has no research and no panel; the run records those
                        steps as not applicable, and showing them as plain successes would claim
                        work that was never done. */}
                    <span className="dw-badge"
                          data-tone={notApplicable(step) ? "muted" : tone(step.status)}
                          data-testid={notApplicable(step) ? "dw-step-na" : undefined}>
                      {notApplicable(step) ? "not run" : step.status}
                    </span>
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
                    {/* HOW LONG IT TOOK. The first question anyone asks of a pipeline, and it
                        was the one thing the step row never answered — the timestamps were in
                        the payload all along, unused. */}
                    {stepDuration(step) ? (
                      <span className="dw-step-took" data-testid="dw-step-took">
                        {stepDuration(step)}
                      </span>
                    ) : null}
                    {step.error ? <p className="dw-step-error">{step.error}</p> : null}
                    {notApplicable(step) ? (
                      <p className="dw-step-note">{String(step.output?.note ?? "")}</p>
                    ) : null}
                    {observedGates(step) ? (
                      <span className="dw-step-gates" data-testid="dw-step-gates">
                        {observedGates(step)}
                      </span>
                    ) : null}
                    {/* ANSWER THE APPROVAL WHERE IT IS SEEN. A gate that can only be answered
                        in another area makes the operator leave the screen that told them it
                        was waiting, and find it again in a list of everything else. */}
                    {step.decision_id && step.status === "waiting" ? (
                      <span className="dw-step-decide" data-testid="dw-step-decide">
                        <form action={resolveDecisionAction}>
                          <input type="hidden" name="decision_id" value={step.decision_id} />
                          <input type="hidden" name="action" value="approve" />
                          <button type="submit" className="cap-btn primary dw-decide-btn">
                            Approve
                          </button>
                        </form>
                        <form action={resolveDecisionAction}>
                          <input type="hidden" name="decision_id" value={step.decision_id} />
                          <input type="hidden" name="action" value="decline" />
                          <button type="submit" className="cap-btn dw-decide-btn">Deny</button>
                        </form>
                      </span>
                    ) : null}
                    {/* WHERE THE WORK IT PRODUCED LIVES. Debugging a step means reading what it
                        actually did; these are the doors to it. */}
                    {stepLinks(step.output || {}).length ? (
                      <span className="dw-step-links" data-testid="dw-step-links">
                        {stepLinks(step.output || {}).map((l) => (
                          l.external ? (
                            <a key={l.label} href={l.href} target="_blank" rel="noreferrer"
                               className="dw-step-link">{l.label} ↗</a>
                          ) : (
                            <a key={l.label} href={l.href} className="dw-step-link">{l.label}</a>
                          )
                        ))}
                      </span>
                    ) : null}
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

/** Defining a step: name it, choose what it runs, say what it needs.
 *
 *  There is deliberately NO risk field. The tier comes from the skill or MCP tool being bound
 *  and is shown here as a consequence of that choice — a customer who could type their own
 *  number would have a documented way past every approval gate in the product.
 */
function DefineStepForm({ bindable, riskFloor, busy, projectId, onDefine, onCancel }: {
  bindable: Bindable;
  riskFloor: number;
  busy: string | null;
  projectId: string;
  onDefine: (fields: Record<string, unknown>) => Promise<{ ok: boolean; error?: string }>;
  onCancel: () => void;
}) {
  const [label, setLabel] = useState("");
  const [summary, setSummary] = useState("");
  // One control, not two. "skill:github.create_pr" / "mcp:<server>:<tool>" keeps the choice a
  // single decision — picking a kind and then a thing is two ways to get it half-made.
  const [bindingRef, setBindingRef] = useState("");
  const [inputs, setInputs] = useState("");
  const [failure, setFailure] = useState<string | null>(null);

  const chosen = useMemo(() => {
    if (bindingRef.startsWith("skill:")) {
      const id = bindingRef.slice(6);
      const skill = bindable.skills.find((s) => s.skill_id === id);
      return skill
        ? { risk: skill.risk_tier, trusted: skill.trusted, unclassified: skill.unclassified,
            what: id }
        : null;
    }
    if (bindingRef.startsWith("mcp:")) {
      const [, serverId, ...rest] = bindingRef.split(":");
      const server = bindable.mcp_servers.find((m) => m.id === serverId);
      const tool = server?.tools.find((t) => t.name === rest.join(":"));
      return server && tool
        ? { risk: tool.risk_tier, trusted: server.trusted, unclassified: false,
            what: `${server.name} · ${tool.name}` }
        : null;
    }
    return null;
  }, [bindingRef, bindable]);

  const blocked = chosen !== null && chosen.risk >= riskFloor && !chosen.trusted;
  // How many things can ACTUALLY be picked, which is not the same as how many servers exist.
  const choices = bindable.skills.length
    + bindable.mcp_servers.reduce((n, server) => n + server.tools.length, 0);
  const unreachable = bindable.mcp_servers.filter((m) => !m.reachable).map((m) => m.name);

  return (
    <form className="dw-define-form" data-testid="dw-define-form" onSubmit={async (e) => {
      e.preventDefault();
      setFailure(null);
      const [kind, ...rest] = bindingRef.split(":");
      const fields: Record<string, unknown> = {
        project_id: projectId,
        label: label.trim(),
        summary: summary.trim(),
        binding_kind: kind === "skill" ? "skill" : "mcp_tool",
        inputs: inputs.split(",").map((i) => i.trim()).filter(Boolean),
      };
      if (kind === "skill") {
        fields.skill_id = rest.join(":");
      } else {
        fields.mcp_server_id = rest[0];
        fields.mcp_tool_name = rest.slice(1).join(":");
      }
      const result = await onDefine(fields);
      if (!result.ok) setFailure(result.error ?? "could not define the step");
    }}>
      <label className="field">
        <span>Name</span>
        <input className="input" autoFocus value={label} data-testid="dw-define-label"
               placeholder="File a compliance ticket"
               onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label className="field">
        <span>Runs</span>
        <select className="input" value={bindingRef} data-testid="dw-define-binding"
                onChange={(e) => setBindingRef(e.target.value)}>
          <option value="">Choose a skill or tool</option>
          {bindable.skills.length ? (
            <optgroup label="Your skills">
              {bindable.skills.map((skill) => (
                <option key={skill.skill_id} value={`skill:${skill.skill_id}`}>
                  {skill.skill_id}{skill.unclassified ? " (unclassified)" : ""}
                </option>
              ))}
            </optgroup>
          ) : null}
          {bindable.mcp_servers.map((server) => (
            <optgroup key={server.id}
                      label={server.reachable ? server.name : `${server.name} (unreachable)`}>
              {server.tools.map((tool) => (
                <option key={tool.name} value={`mcp:${server.id}:${tool.name}`}>
                  {tool.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Needs</span>
        <input className="input" value={inputs} data-testid="dw-define-inputs"
               placeholder="ticket, severity"
               onChange={(e) => setInputs(e.target.value)} />
      </label>
      <label className="field">
        <span>What it does</span>
        <input className="input" value={summary} placeholder="Optional, one line"
               onChange={(e) => setSummary(e.target.value)} />
      </label>

      {/* The consequence of the binding, stated before the step is created rather than at
          publish time when the workflow is already drawn. */}
      {chosen ? (
        <p className="detail dw-define-risk" data-testid="dw-define-risk">
          Risk tier {chosen.risk}, from {chosen.what}
          {chosen.unclassified ? " — nobody has classified this skill, so it is treated as "
                                 + "the floor" : ""}
          {chosen.risk >= riskFloor
            ? ". This step will always stop for a person."
            : ". This step can run automatically."}
        </p>
      ) : null}
      {blocked ? (
        <p className="detail dw-define-blocked" data-testid="dw-define-blocked">
          This is untrusted and at tier {chosen?.risk}. You can define it, but a workflow using
          it will not publish until it has been reviewed.
        </p>
      ) : null}
      {failure ? <p className="detail dw-define-error">{failure}</p> : null}

      <div className="dw-define-actions">
        <button type="submit" className="cap-btn" data-testid="dw-define-save"
                disabled={!!busy || !label.trim() || !bindingRef || choices === 0}>
          Define
        </button>
        <button type="button" className="cap-btn" onClick={onCancel}>Cancel</button>
      </div>
      {choices === 0 ? (
        // Counted in CHOICES, not in servers. A project with three registered MCP servers that
        // are all unreachable has three empty groups in the dropdown and nothing to pick — and
        // saying nothing there reads as a broken control rather than an empty one.
        <p className="detail dw-define-empty" data-testid="dw-define-empty">
          Nothing to bind a step to yet.{" "}
          {unreachable.length
            ? `${unreachable.join(", ")} ${unreachable.length === 1 ? "is" : "are"} `
              + "registered but not answering, so their tools cannot be listed. "
            : ""}
          Register a skill, or connect an MCP server, and it will appear here.
        </p>
      ) : null}
    </form>
  );
}


function BuilderView({ capabilities, blocks, riskFloor, installed, projectId, busy,
                      customSteps, bindable, onDefineStep, onRemoveStep,
                      justMade, onDismissJustMade,
                      target, setTarget, onSave, onCreate }: {
  capabilities: Capability[];
  blocks: Block[];
  riskFloor: number;
  installed: Installed[];
  projectId: string;
  customSteps: CustomStep[];
  bindable: Bindable;
  onDefineStep: (fields: Record<string, unknown>) =>
    Promise<{ ok: boolean; error?: string }>;
  onRemoveStep: (stepKey: string) => void;
  /** The workflow created a moment ago, so the builder can confirm it rather than looking
   *  identical to the state before the click. */
  justMade: { id: string; name: string; from?: string } | null;
  onDismissJustMade: () => void;
  busy: string | null;
  target: string;
  setTarget: (id: string) => void;
  onSave: (definitionId: string, spec: Record<string, unknown>) => void;
  /** Creates the workflow AND writes the steps that are on the canvas. `fromKey` is the
   *  workflow the steps were imported from, so the lineage that makes "Reset to the standard"
   *  meaningful survives — importing into the canvas would otherwise lose it. */
  onCreate: (name: string, spec: Record<string, unknown>, fromKey?: string) => void;
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
  /** The canvas as it was before "New workflow" was pressed, so Cancel puts it back rather
   *  than leaving the operator on an empty canvas for a workflow they did not change. */
  const [stashed, setStashed] = useState<Draft[] | null>(null);
  /** Which workflow's steps are currently on the canvas, and how many came across. Set by
   *  Import, cleared by Cancel — this is what lets the panel say what it just did without
   *  anything having been saved. */
  const [importedFrom, setImportedFrom] = useState<{ key: string; name: string; steps: number } | null>(null);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);


  /** What an import would be called if the operator names nothing. Shown as the placeholder,
   *  so the name the workflow WILL get is visible before the button is pressed.
   *
   *  Two things it has to avoid, both of which happened:
   *    - COMPOUNDING. Copying a copy produced "Minor Feature (copy) (copy)", and the one after
   *      that would have been worse. The existing suffix is stripped before a new one is added.
   *    - COLLIDING. The workflow KEY was made unique but the NAME was not, so the picker
   *      listed two entries reading "Minor Feature (yours) (copy)" and neither said which was
   *      which. Names are now numbered against what the project already has.
   */
  const defaultImportName = useMemo(() => {
    if (!importFrom) return "";
    const source = installed.find((i) => i.workflow_key === importFrom);
    if (!source) return "";
    const base = source.name.replace(/\s*\(copy(?:\s+\d+)?\)\s*$/i, "").trim();
    const taken = new Set(installed.map((i) => i.name));
    let candidate = `${base} (copy)`;
    for (let n = 2; taken.has(candidate) && n < 200; n += 1) {
      candidate = `${base} (copy ${n})`;
    }
    return candidate;
  }, [importFrom, installed]);
  const [defining, setDefining] = useState(false);

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
        customStep: binding.custom_step ? String(binding.custom_step) : undefined,
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

  /** Open the new-workflow panel on an EMPTY canvas. */
  const beginCreating = useCallback(() => {
    setStashed(draft);
    setDraft([]);
    setImportedFrom(null);
    setImportError(null);
    setNewName("");
    setImportFrom("");
    setCreating(true);
  }, [draft]);

  const cancelCreating = useCallback(() => {
    setCreating(false);
    setNewName("");
    setImportFrom("");
    setImportedFrom(null);
    setImportError(null);
    if (stashed) setDraft(stashed);
    setStashed(null);
  }, [stashed]);

  /** IMPORT BRINGS THE STEPS HERE. IT DOES NOT SAVE ANYTHING.
   *
   *  It used to fork server-side on the spot: a workflow appeared in the project before the
   *  operator had decided anything, and the screen jumped into editing it. Nothing is written
   *  now until Create — Import only fills the canvas, so the steps can be changed first and
   *  abandoning the idea leaves nothing behind. */
  const importSteps = useCallback(async () => {
    if (!importFrom) return;
    setImporting(true);
    setImportError(null);
    try {
      const params = new URLSearchParams({
        view: "spec", project_id: projectId, key: importFrom,
      });
      const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
      const body = (await r.json()) as WorkflowSpec & { error?: string };
      if (!r.ok || !body?.spec?.steps) {
        setImportError(body?.error ?? "could not read that workflow's steps");
        return;
      }
      const steps = toDraft(body.spec.steps);
      setDraft(steps);
      setImportedFrom({
        key: importFrom,
        name: installed.find((i) => i.workflow_key === importFrom)?.name ?? importFrom,
        steps: steps.length,
      });
    } catch (e) {
      setImportError(String((e as Error)?.message ?? e));
    } finally {
      setImporting(false);
    }
  }, [importFrom, projectId, toDraft, installed]);

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
    // WHILE CREATING, THE CANVAS IS THE NEW WORKFLOW'S.
    //
    // Nothing exists on the server yet, so there is no spec to load — and loading the selected
    // workflow's steps over the ones just imported is exactly the confusion between "the one
    // you are editing" and "the one you are making" that this flow had.
    if (creating) return;
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
  }, [targetKey, projectId, toDraft, creating]);

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

  /** A step the customer defined. Its approval mode follows the resolved risk tier, so a
   *  tier-3 tool arrives on the canvas already marked manual rather than being switched
   *  silently at save time. */
  const addCustom = (step: CustomStep) =>
    setDraft((current) => [...current, {
      id: nextId(current, step.step_key),
      kind: "capability",
      customStep: step.step_key,
      approval: step.approval_forced_manual ? "manual" : "auto",
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
      if (step.customStep) {
        const defined = customSteps.find((c) => c.step_key === step.customStep);
        return {
          id: step.id,
          kind: "capability",
          binding: {
            custom_step: step.customStep,
            inputs: Object.fromEntries(
              (defined?.inputs ?? []).map((i) => [i.name, { from_input: i.name }]),
            ),
          },
          approval: { mode: step.approval },
          next,
        };
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
  }), [draft, capabilities, customSteps]);

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

        {/* THE CUSTOMER'S OWN STEPS, in the same palette as the built-in ones. A separate
            drawer for "your" steps would make people learn which of their steps BOSS wrote. */}
        <h4 className="dw-palette-heading">Your steps</h4>
        {customSteps.length === 0 ? (
          <p className="detail dw-palette-summary">
            None yet. A step of your own runs one of your skills or an MCP tool — filing a
            ticket, telling a compliance system, running your own scanner.
          </p>
        ) : null}
        {customSteps.map((c) => (
          <button key={c.step_key} type="button" className="dw-palette-item dw-palette-mine"
                  onClick={() => addCustom(c)} disabled={!!busy || !c.usable}
                  data-testid={`dw-palette-custom-${c.step_key}`}
                  title={c.usable
                    ? `${c.summary || c.label} · runs ${c.risk_source}`
                    : (c.unusable_reason ?? "")}>
            <span className="dw-palette-label">{c.label}</span>
            {c.approval_forced_manual ? (
              <span className="dw-gate">always approved</span>
            ) : null}
            <span className="detail dw-palette-summary">
              {/* What it RUNS, not what it was named. A step called "Cleanup" bound to a
                  tier-3 tool should not read as harmless in the palette. */}
              {c.usable ? `runs ${c.risk_source}` : c.unusable_reason}
            </span>
            <span className="dw-palette-remove" role="button" tabIndex={0}
                  aria-label={`Remove ${c.label}`}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault(); e.stopPropagation(); onRemoveStep(c.step_key);
                    }
                  }}
                  onClick={(e) => { e.stopPropagation(); onRemoveStep(c.step_key); }}>×</span>
          </button>
        ))}
        {defining ? (
          <DefineStepForm bindable={bindable} riskFloor={riskFloor} busy={busy}
                          projectId={projectId}
                          onCancel={() => setDefining(false)}
                          onDefine={async (fields) => {
                            const result = await onDefineStep(fields);
                            if (result.ok) setDefining(false);
                            return result;
                          }} />
        ) : (
          <button type="button" className="cap-btn dw-define" disabled={!!busy}
                  onClick={() => setDefining(true)} data-testid="dw-define-step">
            + Define a step
          </button>
        )}

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
        {/* SAY IT WORKED, WHERE THE PERSON IS LOOKING.
            Import created the workflow, copied its steps and selected it — and then showed a
            toolbar identical to the one before the click, so it read as "went back to editing",
            which is how a working feature got reported as broken. The confirmation belongs on
            the canvas that changed, not in a sticky line further up the page. */}
        {justMade && justMade.id === target ? (
          <p className="dw-made" data-testid="dw-made">
            <span className="dw-made-tick" aria-hidden>✓</span>
            <span>
              Created <strong>{justMade.name}</strong>
              {justMade.from ? <> from <strong>{justMade.from}</strong></> : null}
              {draft.length ? ` · ${draft.length} step${draft.length === 1 ? "" : "s"} copied` : ""}
              . You are editing it now — change what you want, then save.
            </span>
            <button type="button" className="dw-made-x" onClick={onDismissJustMade}
                    aria-label="Dismiss">×</button>
          </p>
        ) : null}
        {/* TWO TABS, BECAUSE THEY ARE TWO JOBS.
            Editing an existing workflow and authoring a new one were one screen separated only
            by a button: "Editing: Standard Product Delivery" sat beside "Start from: Backend
            Service Setup", and the operator had to work out which of the two workflows the
            controls acted on. The project has 54 definitions, a large share of them named
            "(copy)" and "(copy 2)" — accidental forks are the predictable result of a screen
            that does not say which mode it is in.

            The modes already existed; only the way in and out did not. `beginCreating` stashes
            the canvas and `cancelCreating` restores it, so the tabs are a faithful control for
            state that was always there — no behaviour changes, the choice is just visible. */}
        <div className="dw-mode-tabs" role="tablist" aria-label="What do you want to do?">
          <button type="button" role="tab" className="dw-mode-tab"
                  aria-selected={!creating} data-testid="dw-tab-edit"
                  disabled={!!busy}
                  onClick={() => { if (creating) cancelCreating(); }}>
            <WfIcon id="edit" /> Edit an existing workflow
          </button>
          <button type="button" role="tab" className="dw-mode-tab"
                  aria-selected={creating} data-testid="dw-tab-new"
                  disabled={!!busy}
                  onClick={() => { if (!creating) beginCreating(); }}>
            <WfIcon id="plus" /> Create a new one
          </button>
        </div>
        <div className="dw-canvas-head" data-mode={creating ? "creating" : "editing"}>
          {/* ONE QUESTION AT A TIME.
              "Editing: Standard Product Delivery" beside "Start from: Backend Service Setup"
              asks the operator to hold two different workflows in mind and says nothing about
              which the buttons act on — and the answer was "the one you are not making". While
              a new workflow is being named, the controls that belong to the old one (which one
              you are editing, reset it, save it) are not just noise, they are wrong. */}
          {/* TWO GROUPS, NOT FIVE CONTROLS IN A ROW.
              Left is what you are working on — which workflow, or the new one you are naming.
              Right is what happens to it. Flat and evenly weighted, the row gave "+ New
              workflow" the same presence as "Reset to the standard", so the one control that
              starts something read as the least important thing on the line. */}
          <div className="dw-head-left">
          {!creating ? (
            <div className="dw-head-picker">
              <label className="field">
                <span><WfIcon id="edit" /> Editing</span>
                <select className="input" value={target}
                        onChange={(e) => setTarget(e.target.value)} data-testid="dw-target">
                  {/* The tab is called "Build your own", and until now the only way in was to
                      fork something — so a screen that promised authorship offered only
                      editing. */}
                  {editable.length === 0
                    ? <option value="">Nothing of your own yet</option>
                    : null}
                  {editable.map((i) => (
                    <option key={i.id} value={i.id}>{i.name}</option>
                  ))}
                </select>
              </label>
              {/* A header made only of controls tells you nothing about the thing they act on.
                  Two facts, in the space the dead gap used to occupy: how big this workflow is,
                  and whether it came from a standard — which is also what decides whether
                  "Reset to the standard" beside it means anything. */}
              {target ? (
                <p className="detail dw-head-meta" data-testid="dw-head-meta">
                  {draft.length} step{draft.length === 1 ? "" : "s"}
                  {loaded?.forked_from
                    ? <> · from <strong>{loaded.forked_from.name}</strong></>
                    : " · written from scratch"}
                </p>
              ) : null}
            </div>
          ) : null}

          {creating ? (
            <form className="dw-new" onSubmit={(e) => {
              e.preventDefault();
              // CREATE IS THE ONLY THING THAT WRITES.
              //
              // Import used to fork on the spot, so a workflow appeared in the project before
              // the operator had decided anything and the screen jumped straight into editing
              // it. Now the canvas is filled first, changed if they want, and only saved here.
              const name = newName.trim() || defaultImportName;
              if (!name) return;
              onCreate(name, spec, importedFrom?.key);
            }}>
              <label className="field">
                <span><WfIcon id="edit" /> Name</span>
                <input className="input" autoFocus value={newName}
                       placeholder={defaultImportName || "Name it"}
                       onChange={(e) => setNewName(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Escape") cancelCreating(); }}
                       aria-label="Name for the new workflow" data-testid="dw-new-name" />
              </label>
              <label className="field dw-new-from">
                <span><WfIcon id="importIn" /> Start from</span>
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
              {/* The three buttons are ONE group, so they wrap together. Left loose they broke
                  apart at this width and Cancel dropped onto a line of its own, which reads as
                  a layout fault rather than a wrap. */}
              <span className="dw-new-actions">
              {/* Import is a SEPARATE, non-destructive step: it fills the canvas below and
                  nothing else. Pressing it twice, or picking a different source and pressing
                  it again, is safe — no workflow exists yet to duplicate. */}
              <button type="button" className="cap-btn" data-testid="dw-import-steps"
                      onClick={() => void importSteps()}
                      disabled={!!busy || !importFrom || importing}>
                <WfIcon id={importedFrom ? "again" : "importIn"} />
                {importing ? "Importing…" : importedFrom ? "Import again" : "Import steps"}
              </button>
              <button type="submit" className="cap-btn primary"
                      disabled={!!busy || !(newName.trim() || defaultImportName)}
                      data-testid="dw-new-confirm">
                <WfIcon id="check" /> Create
              </button>
              <button type="button" className="cap-btn" onClick={cancelCreating}>
                <WfIcon id="x" /> Cancel
              </button>
              </span>
              {importError ? (
                <span className="detail dw-new-error" data-testid="dw-import-error">
                  <WfIcon id="info" /> Could not import — {importError}
                </span>
              ) : importedFrom ? (
                // Said in the panel, because the panel is where the operator still is. The
                // point of the whole change is that they do NOT get moved somewhere else.
                <span className="detail dw-new-hint" data-testid="dw-imported">
                  <WfIcon id="steps" />
                  {importedFrom.steps} step{importedFrom.steps === 1 ? "" : "s"} from{" "}
                  <strong>{importedFrom.name}</strong> are on the canvas. Change them, then
                  press Create — nothing is saved until you do.
                </span>
              ) : !newName.trim() && !importFrom ? (
                <span className="detail dw-new-hint" data-testid="dw-new-hint">
                  <WfIcon id="info" /> Name it, or pick something to start from.
                </span>
              ) : null}
            </form>
          ) : null}
          {/* The "+ New workflow" button that used to live here is now the second TAB. A
              divider and an accent-tinted CTA were both attempts to say "this is a different
              intention" inside a row that still mixed the two; a tablist says it outright, and
              says which one you are in. */}
          </div>
          {/* Hidden while creating, for the same reason: both act on the workflow in the
              dropdown that is no longer on screen. */}
          <span className="dw-canvas-actions" hidden={creating}>
            {/* RESET TO THE STANDARD. Only offered when there is something to reset TO — a
                fork records what it came from, and a workflow written from scratch has no
                original, so the control would be a lie on one of them. */}
            {!creating && loaded?.forked_from ? (
              <button type="button" className="cap-btn" disabled={!!busy}
                      onClick={reset} data-testid="dw-reset"
                      title={`Put the steps back the way ${loaded.forked_from.name} has them`}>
                <WfIcon id="reset" /> Reset to the standard
              </button>
            ) : null}
            {!creating ? (
              <button type="button" className="cap-btn primary"
                      disabled={!!busy || !target || draft.length === 0}
                      onClick={() => onSave(target, spec)} data-testid="dw-save">
                <WfIcon id="save" />
                {busy === "Save" ? "Saving…" : "Save as a new version"}
              </button>
            ) : null}
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
          <div className="dw-canvas-empty" data-testid="dw-canvas-empty">
            <span className="dw-empty-mark" aria-hidden><WfIcon id="empty" /></span>
            <p className="detail">
              {target
                ? "No steps yet — add them from the left, in the order they should run."
                : "Start a new workflow, or fork one of the standards to change it."}
            </p>
          </div>
        ) : (
          <ol className="dw-draft">
            {draft.map((step, index) => {
              const capability = capabilities.find((c) => c.key === step.capability);
              const mine = customSteps.find((c) => c.step_key === step.customStep);
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
                    {step.kind !== "capability"
                      ? blocks.find((b) => b.kind === step.kind)?.label ?? step.kind
                      : step.customStep
                        ? mine?.label ?? step.customStep
                        : capability?.label ?? step.capability}
                  </span>
                  {step.customStep ? <span className="dw-draft-kind">yours</span> : null}
                  {/* A control block is visibly a different sort of thing from a work step. */}
                  {step.kind !== "capability" ? (
                    <span className="dw-draft-kind">flow</span>
                  ) : null}
                  <select className="input dw-draft-approval" value={step.approval}
                          disabled={step.kind !== "capability"
                                    || capability?.approval_forced_manual
                                    || mine?.approval_forced_manual}
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
