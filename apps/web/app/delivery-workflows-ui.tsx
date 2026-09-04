"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";

import { uniqueWorkflowName } from "./workflow-names";
import { consoleHref, readNewProjectIntent } from "./url-intent";
import { isLoopbackPreviewUrl, operatorPreviewOpenHref } from "../lib/code-tasks";

/** Where the API lives, for the proxy path a preview link resolves to. */
const PREVIEW_API = process.env.NEXT_PUBLIC_BOSS_API_URL ?? "";

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
  /** The preview running RIGHT NOW for this run's build, resolved when the runs are read.
   *  The one recorded in `output` was live when the step ran, and previews expire. The whole
   *  record, because turning one into an openable link is a rule the console already owns. */
  live_preview?: {
    id: string; status: string; url: string | null;
    /** The build actually serving it, and whether that is THIS step's build. A live preview
     *  of a later build of the same app is worth linking to — but only if we say so. */
    build_id?: string; same_build?: boolean;
  } | null;
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
    status?: string; gates_passed?: unknown; targets?: unknown; findings?: unknown;
  };
  if (out.status !== "observed") return null;
  // The monitoring step reports targets rather than gates: which apps are under health probing
  // and whether a probe has run yet. "2 watched · app (production)" answers the question the
  // step exists to answer.
  // Findings the build shipped with. Not a failure — the pipeline's own policy said surface
  // and continue — but not a clean pass either, and the two must not look the same.
  if (Array.isArray((out as { findings?: unknown }).findings)) {
    const found = ((out as { findings: unknown[] }).findings).map(String);
    if (found.length) return `findings: ${found.join(" · ")}`;
  }
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

/** A step whose work FAILED and whose run carried on regardless. */
function failedButContinued(step: RunStep): boolean {
  return (step.output as { status?: string } | undefined)?.status === "failed_continued";
}

/** A step the runtime completed by recording that its phase never happened. */
function notApplicable(step: RunStep): boolean {
  return (step.output as { status?: string } | undefined)?.status === "not_applicable";
}

/** Live runs first, newest first within each group. Sorting by `updated_at` alone floats a
 *  blocked run from last week above the project that started a minute ago, purely because
 *  something touched it more recently — which is how the page came to show an expired run
 *  while a build was in flight. */
/** "2 Sep 03:42Z" — cut straight from the ISO string rather than through `toLocaleString` or a
 *  "3 hours ago". Both of those read the clock or the timezone, which differ between the server
 *  render and the browser one, and React replaces the whole subtree when they disagree. This is
 *  the same characters everywhere, which is what a timestamp on a row needs to be. */
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function startedLabel(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso ?? "");
  if (!m) return "";
  const month = MONTHS[Number(m[2]) - 1] ?? m[2];
  return `${Number(m[3])} ${month} ${m[4]}:${m[5]}Z`;
}

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
  // WHERE THIS PROJECT CAN SHIP. Fetched once for the release gate: choosing a target is part
  // of the deploy decision, and a gate that only says yes/no cannot express "yes, to staging".
  const [targets, setTargets] = useState<DeployTarget[]>([]);
  const [hosts, setHosts] = useState<DeployHost[]>([]);
  useEffect(() => {
    let alive = true;
    // TWO INDEPENDENT LOADS, AND THEY MUST STAY INDEPENDENT.
    //
    // These were two try/catch blocks in ONE async function, each ending `if (!res.ok) return;`
    // — and `return` does not skip the rest of a block, it leaves the whole function. So when
    // /api/deployment-targets answered 503, the hosts fetch NEVER RAN. `hosts` stayed empty,
    // and the deploy step told the operator "this project has no verified host. Add one under
    // Infra and Models" while Infra showed hetzner-ci-01 VERIFIED and READY TO DEPLOY. They
    // reported it perhaps ten times; every fix went to the message, the picker and the wire
    // format, and the request that would have populated it was never being made.
    //
    // Separate functions, started separately: one endpoint being down cannot silence the
    // other. The catch blocks always intended this; the early `return` quietly undid it.
    const loadTargets = async () => {
      try {
        const res = await fetch(
          `/api/deployment-targets?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (!res.ok) return;
        const body = await res.json();
        if (alive) setTargets((body.items ?? []).filter(
          (t: DeployTarget) => (t.status ?? "active") === "active"));
      } catch { /* the gate still works; it just cannot offer a target */ }
    };
    const loadHosts = async () => {
      try {
        const res = await fetch(
          `/api/deployment-hosts?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (!res.ok) return;
        const body = await res.json();
        if (alive) setHosts(verifiedHostsFrom(body));
      } catch { /* no hosts offered; the operator can still deny */ }
    };
    void loadTargets();
    void loadHosts();
    return () => { alive = false; };
  }, [projectId]);
  // Loading, failed and empty are three different things. A screen that renders nothing while
  // it loads and nothing when the API is down says "you have no workflows" in both cases.
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // A PREVIEW IS ASKED FOR IN A SECOND AND ARRIVES IN A MINUTE.
  //
  // `act` clears `busy` when the POST returns, and start_preview returns "starting" straight
  // away — so the button flashed "Starting…" for about a second, went back to normal, and the
  // notice said "Preview — done." while nothing was serving yet. From the operator's side the
  // click did nothing, which is an invitation to click it again and boot a second copy.
  //
  // This holds the build's id from the click until a preview URL actually shows up (or the
  // wait is plainly too long), and the poll below refreshes so it appears without another click.
  const [previewStarting, setPreviewStarting] = useState<{ runId: string; at: number } | null>(null);
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

  // A RUN THAT IS ON ITS WAY. Arriving from Build, the run may not be in the list yet — it is
  // created by the press, and this page had loaded before it. Poll fast until it shows rather
  // than presenting an empty list, which reads as "nothing happened".
  const awaited = openRun && !runs.some((r) => r.id === openRun) ? openRun : null;
  useEffect(() => {
    if (!awaited) return;
    const t = window.setInterval(() => { void load(); }, 2000);
    return () => window.clearInterval(t);
  }, [awaited, load]);

  // SENT HERE TO START SOMETHING. `?new=1` is a one-shot command, consumed on read so a later
  // navigation cannot re-deliver it — the same rule the Factory form learned the hard way.
  useEffect(() => {
    const read = () => {
      const intent = readNewProjectIntent(window.location.search);
      if (!intent.open) return;
      setTab("running");
      window.setTimeout(() => {
        document.querySelector<HTMLTextAreaElement>('[data-testid="dw-new-directive"]')?.focus();
      }, 0);
      try {
        window.history.replaceState(null, "",
          `${window.location.pathname}${intent.nextSearch}${window.location.hash}`);
      } catch { /* non-browser — the composer is on screen either way */ }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

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

  // Poll while a preview is coming up, so the URL appears on its own. Bounded: after three
  // minutes we stop claiming it is on its way and let the step's own state speak.
  useEffect(() => {
    if (!previewStarting) return;
    const timer = window.setInterval(() => {
      if (Date.now() - previewStarting.at > 180_000) { setPreviewStarting(null); return; }
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [previewStarting, load]);

  const act = useCallback(async (label: string, action: string, payload: Record<string, unknown>) => {
    setBusy(label);
    setNotice(null);
    try {
      const result = await post(action, { project_id: projectId, actor, ...payload });
      // The engine's own sentence, not "something went wrong". "step 'd' is risk tier 2 and
      // always requires a person" is the whole value of the validation.
      setNotice(result.ok ? `${label} — done.` : `${label} — ${result.error}`);
      // RELOAD WHILE STILL BUSY. `busy` was cleared BEFORE this, so every action ended the
      // same way: the button snapped back to its normal label and the console then spent
      // seconds re-fetching with nothing on screen to say so. From the operator's side that
      // is a click that did nothing — reported as "I clicked and there's no indicator".
      if (result.ok) await load();
      return result;
    } finally {
      // In a finally so a throw cannot leave the console stuck showing "Advancing…" forever.
      setBusy(null);
    }
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

      {/* WHAT IS HAPPENING RIGHT NOW. Per-button labels are easy to forget on the next
          button added, and they say nothing during the reload that follows the request. This
          is one indicator every action gets for free, for as long as the action is running. */}
      {busy ? (
        <p className="dw-notice dw-working" data-testid="dw-working" role="status" aria-live="polite">
          <span className="dw-spin" aria-hidden />
          {busy}… <span className="detail">working — the list refreshes when it finishes</span>
        </p>
      ) : notice ? (
        <p className="dw-notice" data-testid="dw-notice">{notice}</p>
      ) : null}

      {tab === "running" ? (
        <NewProjectComposer
          projectId={projectId} actor={actor} workflows={installed}
          onStarted={(runId) => {
            if (runId) setOpenRun(runId);
            setNotice(runId ? `Started ${runId}.` : "Started.");
            void load();
          }}
        />
      ) : null}
      {tab === "running" && awaited ? (
        <p className="dw-awaiting detail" data-testid="dw-awaiting">
          Starting <code>{awaited}</code>… the run appears here the moment it is created.
        </p>
      ) : null}
      {tab === "running" ? (
        <RunsView
          runs={runs} openRun={openRun} setOpenRun={setOpenRun} busy={busy}
          capabilities={capabilities} targets={targets} hosts={hosts}
          // SAY WHAT IT DID. "Advance now" reported success while advancing nothing — a run
          // waiting on a person cannot be ticked forward — and a button that always says
          // "done" is indistinguishable from a button that does nothing.
          onAdvance={async () => {
            setBusy("Advance");
            const r = await post("advance", { project_id: projectId, actor });
            setBusy(null);
            const moved = ((r.ok ? (r.data as { performed?: unknown[] })?.performed : []) ?? []);
            setNotice(!r.ok ? `Advance — ${r.error}`
              : moved.length ? `Advanced ${moved.length} step${moved.length === 1 ? "" : "s"}.`
              : "Nothing to advance — every run is finished or waiting on a person.");
            if (r.ok) await load();
          }}
          onRetryStep={(runId, stepId) =>
            act("Retry", "retry_step", { run_id: runId, step_id: stepId })}
          onSetTarget={(runId, targetId) =>
            act("Set target", "set_input", { run_id: runId, values: { target_id: targetId } })}
          onPrepareTarget={(runId, hostId) =>
            act("Prepare target", "prepare_target",
                { run_id: runId, host_id: hostId, environment: "staging" })}
          onRunPreview={(buildRunId) => {
            setPreviewStarting({ runId: buildRunId, at: Date.now() });
            // "Starting a preview" — not "done". The old label claimed the work had finished
            // when only the request had been accepted.
            return act("Starting a preview", "start_preview", { run_id: buildRunId });
          }}
          previewStarting={previewStarting?.runId ?? ""}
          onBound={load}
          onRerun={(run) => act("Re-run", "start", {
            workflow_key: run.workflow_key, input: inputForRerun(run.input),
          })}
        />
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
              // Called what it is a copy OF. "(yours)" said nothing the row does not already
              // show — the console knows which workflows are catalogue templates and which are
              // the customer's own.
              new_name: uniqueWorkflowName(entry?.name ?? key, installed.map((i) => i.name)),
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
  // KEEP WHO YOU ARE. These hrefs were built from scratch — `?area=code_tasks&run=…` — which
  // dropped `?org=`, and the console gates on that: no org means the organisation picker. So
  // every one of these links threw the operator onto "Select your organization" and read as
  // the session expiring mid-run.
  const here = typeof window !== "undefined" ? window.location.search : "";
  const link = (params: Record<string, string | null | undefined>) => consoleHref(here, params);
  const str = (k: string) => (typeof output[k] === "string" ? (output[k] as string) : "");
  const awaited = (output.await as { kind?: string; id?: string } | undefined) ?? {};

  const codeTask = str("code_task_run_id") || (awaited.kind === "code_task" ? awaited.id ?? "" : "")
    || (output.capability === "build" ? str("run_id") : "");
  if (codeTask) {
    out.push({ label: "open the project", href: link({ area: "code_tasks", run: codeTask }) });
    // Debugging a step means reading what it actually did, filtered to this work.
    out.push({ label: "activity log", href: link({ area: "activity", run: codeTask }) });
    out.push({ label: "Ask BOSS",
               href: link({ area: "code_tasks", run: codeTask, agent: codeTask }) });
  }
  // THE PANEL, not the list of panels. This linked to `?area=meetings` — the landing page —
  // so "open the panel" meant "go and find it", on a screen listing every meeting the project
  // has ever held. The meeting room reads `?meeting=`, and the step knows which one it adopted.
  const meeting = str("meeting_run_id") || (awaited.kind === "meeting" ? awaited.id ?? "" : "");
  if (meeting) {
    out.push({ label: "open the panel", href: link({ area: "meetings", meeting }) });
  }
  const discovery = str("discovery_id") || (awaited.kind === "discovery" ? awaited.id ?? "" : "");
  if (discovery) {
    out.push({ label: "open the research", href: link({ area: "discovery", run: discovery }) });
  }
  const pr = str("pull_request_url");
  if (pr) {
    // Numbered, not a 60-character URL truncated mid-path. The number is what a person says
    // out loud, and the link still goes to the same place.
    const number = String(output.pull_request_number ?? "").trim() || pr.split("/").pop();
    out.push({ label: `pull request #${number}`, href: pr, external: true });
  }
  // START_PREVIEW NESTS ITS RESULT. The tool returns `{preview: {url, id, ...}}`, and this
  // looked only for a flat `preview_url` — so the one link an operator most wants, to the app
  // they just built, was the one that never appeared.
  //
  // THE LINK FACTORY WOULD BUILD, not a second attempt at one.
  //
  // Three times this was wrong, three different ways: no link at all, then a link to a preview
  // that had expired, then a link to 127.0.0.1:42543 — where the app really is listening, on
  // the host, which is not reachable from a browser anywhere else. `operatorPreviewOpenHref`
  // already encodes the whole rule (refuse expired, refuse loopback, else the API's own
  // /v1/previews/<id>/web/ proxy) and Factory has used it all along. Using it here is the fix;
  // writing a fourth version of it would not be.
  const live = output.live_preview as
    { id: string; status: string; url: string | null } | undefined;
  const nested = output.preview as { id?: string; url?: string } | undefined;
  const record = live ?? (nested?.id
    ? { id: nested.id, status: "running", url: nested.url ?? null }
    : null);
  const href = record ? operatorPreviewOpenHref(record) : null;
  if (href) {
    out.push({ label: "open the preview", external: true,
               href: href.startsWith("http") ? href : `${PREVIEW_API}${href}` });
  }
  return out;
}

/**
 * STARTING WORK, WHERE THE WORK IS WATCHED.
 *
 * "New project" lived in Factory because Factory was where projects were listed. Now the
 * workflow drives — the press starts a run, and the run convenes the panel, waits for the
 * approval, builds, previews and asks before it deploys — so the page that shows those steps
 * is the page that should start them. Factory keeps its own entry for changing an app that
 * already exists, which is a different act.
 */
function NewProjectComposer({ projectId, actor, workflows, onStarted }: {
  projectId: string; actor: string;
  workflows: Installed[];
  onStarted: (runId: string | null) => void;
}) {
  const [directive, setDirective] = useState("");
  const [workflowKey, setWorkflowKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    const text = directive.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      const title = text.length > 72 ? `${text.slice(0, 69)}…` : text;
      const r = await post("start", {
        project_id: projectId, actor,
        // No key means "whatever this project maps for a new project" — the honest default,
        // and the same resolution every other entry point uses.
        ...(workflowKey ? { workflow_key: workflowKey } : { work_class: "project.new" }),
        input: { title, directive: text },
      });
      if (!r.ok) { setError(r.error ?? "Could not start it."); return; }
      const run = (r.data as { run?: { id?: string }; id?: string } | undefined);
      setDirective("");
      onStarted(String(run?.run?.id ?? run?.id ?? "") || null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="dw-new" data-testid="dw-new-project">
      <div className="dw-new-head">
        <h3>Start a project</h3>
        <p className="detail">
          A panel specs it, you approve, then it builds, previews, and asks before it deploys.
        </p>
      </div>
      {/* ONE FIELD AND ONE BUTTON. The composer read as a form — a bare box, a floating label,
          a control the same weight as the thing it acts on. Everything lives inside one
          bordered surface that lights on focus, the way the Command Center's composer does,
          because they are the same act done from two places. */}
      <div className="dw-new-box">
        <textarea
          className="dw-new-input" rows={3} value={directive} data-testid="dw-new-directive"
          placeholder="A shift-swap board where staff can offer and claim shifts…"
          onChange={(e) => setDirective(e.target.value)}
          onKeyDown={(e) => {
            // Send on ⌘/Ctrl+Enter, like every other composer in this console. Plain Enter
            // stays a newline: a directive is usually more than one line.
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); void start(); }
          }}
        />
        <div className="dw-new-foot">
          <label className="dw-new-pick">
            <span>Workflow</span>
            <select className="mr-select" value={workflowKey} data-testid="dw-new-workflow"
                    onChange={(e) => setWorkflowKey(e.target.value)}>
              <option value="">This project&apos;s usual path</option>
              {/* Keyed by workflow_key, not a version id: `start` resolves the ACTIVE version
                  itself, so naming a version here would pin a run to whatever was current
                  when the page happened to load. */}
              {workflows.map((w) => (
                <option key={w.id} value={w.workflow_key}>{w.name}</option>
              ))}
            </select>
          </label>
          <span className="detail dw-new-hint">⌘↵ to start</span>
          <button type="button" className="cap-btn primary" disabled={busy || !directive.trim()}
                  onClick={() => void start()} data-testid="dw-new-start">
            {busy ? "Starting…" : "Start"}
          </button>
        </div>
      </div>
      {error ? <p className="dw-new-error" data-testid="dw-new-error">{error}</p> : null}
    </section>
  );
}

/**
 * The input a RE-RUN should start from.
 *
 * "Run this workflow again" copied the previous run's input whole — including the `adopt_*`
 * keys that tell the runtime "this work already happened, go and watch it". So re-running a
 * project adopted the same finished build: research recorded as not-run, the old panel and the
 * old approval adopted, and `build` green in seconds against code written yesterday. That is a
 * re-observation, not a re-run, and it is the opposite of what somebody pressing it wants.
 *
 * The directive, the title, the repository and the deploy target are kept — they are what the
 * work IS and where it ships. What is dropped is every claim about work that has already
 * happened, so the new run does its own research, convenes its own panel, and builds.
 */
export function inputForRerun(input: Record<string, unknown> | null | undefined) {
  const next: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(input ?? {})) {
    if (key.startsWith("adopt_")) continue;
    next[key] = value;
  }
  return next;
}

/** The four states an operator acts on differently, plus everything. */
/** Somewhere this project can ship to. Offered at the release gate so "deploy" is a choice
 *  about WHERE, not just whether. */
type DeployTarget = { id: string; app_name: string; environment: string; status?: string };

/** A machine this project can ship to. Choosing one is what MAKES a target: `deploy_app`
 *  refuses without an active target and never creates infrastructure itself, so a project
 *  built for the first time had nowhere to go. */
type DeployHost = { id: string; name: string; address?: string; status?: string };

/** How the runtime words a gate somebody said no to. */
const DENIED = /^step '(.+)' was rejected$/;

const BUCKETS = ["needs_you", "running", "blocked", "done"] as const;
type Bucket = typeof BUCKETS[number];
const BUCKET_LABEL: Record<Bucket | "all", string> = {
  all: "All",
  needs_you: "Needs you",
  running: "Running",
  blocked: "Stopped",
  done: "Done",
};

function RunsView({ runs, openRun, setOpenRun, onAdvance, busy, capabilities,
                   onRetryStep, onRerun, onSetTarget, onPrepareTarget, onRunPreview,
                   onBound,
                   previewStarting,
                   targets, hosts }: {
  runs: Run[];
  openRun: string | null;
  setOpenRun: (id: string | null) => void;
  onAdvance: () => void;
  busy: string | null;
  capabilities: Capability[];
  onRetryStep: (runId: string, stepId: string) => void;
  onRerun: (run: Run) => void;
  onSetTarget: (runId: string, targetId: string) => void;
  onPrepareTarget: (runId: string, hostId: string) => void;
  onRunPreview: (buildRunId: string) => void;
  /** Re-read the runs — used after a binding lands so the step shows it. */
  onBound: () => void;
  /** The build whose preview was just asked for and has not appeared yet. */
  previewStarting?: string;
  targets: DeployTarget[];
  hosts: DeployHost[];
}) {
  const [bucket, setBucket] = useState<Bucket | "all">("all");
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
  // WAITING, BLOCKED AND DEAD-LETTERED ARE NOT "RUNNING".
  //
  // Every run sat under one heading called Running, so a run stopped dead and a run waiting on
  // a person looked like a run making progress. These are the four states an operator acts on
  // differently: one needs them, one needs nothing, one needs a decision about a failure, and
  // one is history.
  const bucketOf = (run: Run): Bucket =>
    run.steps.some(needsYou) ? "needs_you"
      : ["failed", "deadlettered", "blocked", "cancelled"].includes(run.status) ? "blocked"
      : LIVE.includes(run.status) ? "running"
      : "done";
  const counts = BUCKETS.reduce((acc, b) => {
    acc[b] = runs.filter((r) => bucketOf(r) === b).length;
    return acc;
  }, {} as Record<Bucket, number>);
  const shown = [...runs].filter((r) => bucket === "all" || bucketOf(r) === bucket)
    .sort(byLiveness);

  return (
    <div className="dw-runs">
      <div className="dw-runs-head">
        <div className="dw-filters" role="tablist" aria-label="Run state">
          {(["all", ...BUCKETS] as const).map((b) => (
            <button key={b} type="button" role="tab" aria-selected={bucket === b}
                    className="dw-filter" data-active={bucket === b}
                    data-tone={b} onClick={() => setBucket(b)}
                    data-testid={`dw-filter-${b}`}>
              {BUCKET_LABEL[b]}
              <em>{b === "all" ? runs.length : counts[b as Bucket]}</em>
            </button>
          ))}
        </div>
        <button type="button" className="cap-btn" onClick={onAdvance} disabled={!!busy}
                data-testid="dw-advance">
          {busy === "Advance" ? "Advancing…" : "Advance now"}
        </button>
      </div>
      {shown.length === 0 ? (
        <p className="dw-empty detail" data-testid="dw-none-in-bucket">
          Nothing {BUCKET_LABEL[bucket].toLowerCase()}.
        </p>
      ) : null}
      {shown.map((run) => {
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
              {/* WHICH OF THE TWO IS THIS. "Run this workflow again" leaves the earlier run
                  alone, by design — so the list showed two rows with the same title, the same
                  "5 of 8" and the same "waiting on release_manager", and nothing an operator
                  could use to tell which was today's. The start time is the distinguishing
                  fact, so it is on the row rather than hidden in the expanded detail. */}
              {run.created_at ? (
                <span className="detail dw-run-started" data-testid="dw-run-started">
                  started {startedLabel(run.created_at)}
                </span>
              ) : null}
              {run.waiting_on ? (
                <span className="dw-waiting" data-testid="dw-waiting">
                  waiting on {run.waiting_on}
                </span>
              ) : null}
            </button>
            {run.blocked_reason ? (
              // A DENIED RELEASE IS A DECISION, NOT A FAULT. "step 'deploy' was rejected"
              // rendered in the same red as a crash, with no hint of what happens next — so a
              // deliberate no looked like a failure the operator had to debug.
              <p className="dw-blocked" data-denied={DENIED.test(run.blocked_reason)}
                 data-testid="dw-blocked">
                {DENIED.test(run.blocked_reason)
                  ? `You denied ${run.blocked_reason.replace(DENIED, "$1")}. Nothing shipped — `
                    + "reopen it with “Try … again”, or leave the run as it is."
                  : run.blocked_reason}
              </p>
            ) : null}
            {open ? (
              <StepPath run={run} capabilities={capabilities} targets={targets} hosts={hosts}
                        onRetryStep={(stepId) => onRetryStep(run.id, stepId)}
                        onRerun={() => onRerun(run)} busy={busy}
                        onSetTarget={(targetId) => onSetTarget(run.id, targetId)}
                        onPrepareTarget={(hostId) => onPrepareTarget(run.id, hostId)}
                        onRunPreview={onRunPreview}
                        onBound={onBound}
                        previewStarting={previewStarting} />
            ) : null}
          </article>
        );
      })}
    </div>
  );
}


/* ── The path the work takes ─────────────────────────────────────────────────── */

/** Where a step sits in the story: done, happening now, still ahead, or never happened. */
/** Findings the build shipped with: reported, not fatal, and never shown as clean. */
function hasFindings(step: RunStep): boolean {
  const f = (step.output as { findings?: unknown } | undefined)?.findings;
  return Array.isArray(f) && f.length > 0;
}

function phaseOf(step: RunStep):
    "done" | "now" | "ahead" | "skipped" | "failed" | "findings" | "failed_on" {
  // FAILED, AND THE WORK WENT ON. Research that died is not "not run" — that hides something
  // worth fixing — and it is not a stopped run either, because the project carried on without
  // it. Its own state, so neither reading is offered.
  if (failedButContinued(step)) return "failed_on";
  if (notApplicable(step) || step.status === "skipped") return "skipped";
  if (step.status === "failed" || step.status === "deadlettered" || step.status === "cancelled") {
    return "failed";
  }
  if (step.status === "succeeded") return hasFindings(step) ? "findings" : "done";
  if (["running", "leased", "ready", "waiting", "queued"].includes(step.status)) return "now";
  return "ahead";
}

/** A step needing a person, right now. The one thing on this page that is an instruction. */
function needsYou(step: RunStep): boolean {
  return Boolean(step.decision_id) && step.status === "waiting";
}

/**
 * THE WORKFLOW AS A PATH, not a list of rows.
 *
 * A pipeline is a shape — research, then a panel, then a build, then a preview — and an
 * operator reads "where has this got to" off that shape in one glance. Rendering it as nine
 * stacked rows of identical text made them read nine statuses and assemble the shape in their
 * head, every time.
 *
 * The rail carries the state: finished segments are solid, the one in flight animates, what is
 * ahead is dim, and a phase that never happened is dashed rather than green. Clicking a node
 * opens everything known about that step below it — duration, error, what it produced, and the
 * doors to the pages where it can be debugged.
 */
function StepPath({ run, capabilities, targets, hosts, onRetryStep, onRerun, onSetTarget,
                   onPrepareTarget, onRunPreview, onBound, previewStarting, busy }: {
  run: Run; capabilities: Capability[]; targets: DeployTarget[]; hosts: DeployHost[];
  onRetryStep: (stepId: string) => void; onRerun: () => void;
  onSetTarget: (targetId: string) => void; onPrepareTarget: (hostId: string) => void;
  onRunPreview: (buildRunId: string) => void;
  /** Re-read the runs — used after a binding lands so the step shows it. */
  onBound: () => void;
  previewStarting?: string;
  busy: string | null;
}) {
  // Has a preview been asked for on THIS build and not yet appeared? The URL is the finish
  // line; the POST returning is not.
  // THE BUILD A PREVIEW IS FOR. Read from whichever step produced it rather than a step named
  // "build", because a customer can rename their steps and the canonical field is the contract.
  const buildRunId = (() => {
    const fromInput = (run.input ?? {})["adopt_code_task_run_id"];
    if (typeof fromInput === "string" && fromInput.trim()) return fromInput.trim();
    for (const s of run.steps) {
      const produced = (s.output ?? {}) as Record<string, unknown>;
      const direct = produced.code_task_run_id;
      if (typeof direct === "string" && direct.startsWith("crun_")) return direct;
      const awaited = produced.await as { kind?: string; id?: string } | undefined;
      if (awaited?.kind === "code_task" && awaited.id) return awaited.id;
    }
    return null;
  })();
  const byKey = new Map(capabilities.map((c) => [c.key, c]));
  const steps = run.steps;
  // Open on the step that wants attention, then the one in flight, then the first. Landing on
  // "research succeeded" when a gate three steps along is waiting for you is the wrong answer
  // to "what is happening".
  const initial = steps.find(needsYou) ?? steps.find((s) => phaseOf(s) === "now")
    ?? steps.find((s) => phaseOf(s) === "failed") ?? steps[0];
  const [picked, setPicked] = useState<string | null>(initial?.step_id ?? null);
  const current = steps.find((s) => s.step_id === picked) ?? initial;

  return (
    <div className="dw-path" data-testid="dw-path">
      <ol className="dw-rail" aria-label="Workflow steps">
        {steps.map((step, index) => {
          const phase = phaseOf(step);
          const capability = step.capability ? byKey.get(step.capability) : undefined;
          const previous = index > 0 ? phaseOf(steps[index - 1]) : null;
          return (
            <li key={step.step_id} className="dw-node" data-phase={phase}
                data-picked={step.step_id === picked} data-testid="dw-node">
              {index > 0 ? (
                // The connector INTO this step: solid once the work has flowed through it,
                // animated while it is flowing, dim before.
                <i className="dw-link" aria-hidden
                   data-flow={["done", "skipped", "findings", "failed_on"].includes(previous ?? "")
                     ? (phase === "now" ? "moving" : "past") : "future"} />
              ) : null}
              <button type="button" className="dw-node-btn"
                      aria-current={phase === "now" ? "step" : undefined}
                      onClick={() => setPicked(step.step_id)}>
                <span className="dw-dot" aria-hidden>
                  {phase === "done" ? "✓" : phase === "failed" ? "!"
                    : phase === "failed_on" ? "✕"
                    : phase === "findings" ? "▲"
                    : phase === "skipped" ? "–" : index + 1}
                </span>
                <span className="dw-node-name">{capability?.label ?? step.capability ?? step.step_id}</span>
                <span className="dw-node-meta">
                  {needsYou(step) ? (
                    <em className="dw-node-you" data-testid="dw-node-you">needs you</em>
                  ) : phase === "skipped" ? "not run"
                    : stepDuration(step) || step.status}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      {current ? (
        <StepDetail
          step={current}
          capability={current.capability ? byKey.get(current.capability) : undefined}
          targets={targets}
          hosts={hosts}
          runInput={run.input ?? {}}
          onRetry={() => onRetryStep(current.step_id)}
          onSetTarget={(targetId) => onSetTarget(targetId)}
          onPrepareTarget={onPrepareTarget}
          buildRunId={buildRunId}
          onRunPreview={onRunPreview}
          onBound={onBound}
          previewStarting={previewStarting}
          busy={busy}
        />
      ) : null}
      {/* START IT AGAIN, FROM THE TOP. A run that failed early had no way back except
          retyping the directive into the New project form — the workflow, the title and the
          instructions are all right here. */}
      {/* NOT WHILE IT IS STILL GOING.
          This button was offered on every run, including one mid-flight, and it does exactly
          what it says: starts a SECOND run on the same directive. An operator whose deploy
          step was stuck pressed it expecting a retry and got a duplicate — two workflows for
          one project, both running, both at step 6, one of them titled from the raw directive
          and the other from the cleaned name, so they did not even look like the same thing.
          Re-running is for a run that has STOPPED; a step that needs another go has "Try …
          again", which reruns that step and keeps everything before it. */}
      <div className="dw-run-actions">
        {ACTIVE_RUN_STATES.has(run.status) ? (
          <>
            <button type="button" className="cap-btn" disabled data-testid="dw-rerun-blocked">
              Run this workflow again
            </button>
            <span className="detail">
              This run is still {run.status}. Starting it again would make a second workflow
              for the same project, not retry this one — use “Try … again” on the step that
              needs another go, or cancel this run first.
            </span>
          </>
        ) : (
          <>
            <button type="button" className="cap-btn" onClick={onRerun} disabled={!!busy}
                    data-testid="dw-rerun">
              {busy === "Re-run" ? "Starting…" : "Run this workflow again"}
            </button>
            <span className="detail">
              Starts {run.workflow_name} again on the same directive — its own research, its
              own panel, its own build. The existing run is left as it is.
            </span>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * WHERE THIS SHIPS — the half of the release decision that was missing.
 *
 * `deploy_app` refuses unless an ACTIVE target exists for the environment, and it never creates
 * one: it does not make infrastructure. So a project built for the first time had nowhere to
 * go, and the gate could only offer targets belonging to OTHER apps — picking one would have
 * shipped this app over somebody else's.
 *
 * Choosing a HOST is the act that was missing. A target is that host plus this app's name, its
 * environment and a free port, and the button that makes one also points the run at it.
 */
function DeployWhere({ targets, hosts, runInput, buildRunId, busy,
                      onSetTarget, onPrepareTarget, onBound }: {
  targets: DeployTarget[]; hosts: DeployHost[]; runInput: Record<string, unknown>;
  /** The code task whose build is being shipped — the app the target binds to. */
  buildRunId: string;
  busy: string | null;
  onSetTarget: (targetId: string) => void; onPrepareTarget: (hostId: string) => void;
  onBound: () => void;
}) {
  const chosenId = typeof runInput.target_id === "string" ? runInput.target_id : "";
  const chosen = targets.find((t) => t.id === chosenId);
  const appName = typeof runInput.title === "string" ? runInput.title : "";
  // A target for THIS app, not one of the others. Matching by name is how the API keys them.
  const mine = targets.filter((t) => t.app_name === appName);

  if (chosen) {
    return (
      <p className="dw-target-set" data-testid="dw-deploy-chosen">
        Ships to <b>{chosen.app_name}</b> · {chosen.environment}
      </p>
    );
  }
  if (mine.length) {
    return (
      <label className="mr-field-label dw-target">
        Ship to
        <select className="mr-select" data-testid="dw-deploy-target" defaultValue=""
                disabled={!!busy}
                onChange={(e) => { if (e.target.value) onSetTarget(e.target.value); }}>
          <option value="">Choose a target…</option>
          {mine.map((t) => (
            <option key={t.id} value={t.id}>{t.app_name} · {t.environment}</option>
          ))}
        </select>
      </label>
    );
  }
  if (hosts.length) {
    return (
      <BindToHost hosts={hosts} appName={appName} buildRunId={buildRunId} busy={busy}
                  onPrepareTarget={onPrepareTarget} onBound={onBound} />
    );
  }
  return (
    <p className="dw-target-set detail" data-testid="dw-deploy-nowhere">
      Nowhere to ship: this project has no verified host. Add one under Infra and Models, then
      come back — approving now would fail at the first connection.
    </p>
  );
}

/** Everything known about one step, in the order an operator asks for it. */
/** Is this the deploy step?
 *
 *  `step.capability` IS NULL on the wire — the runs endpoint does not populate it — so
 *  `step.capability === "deploy"` was false every time, and the target picker never rendered
 *  in ANY of the three versions shipped. Three fixes to a control whose only real problem was
 *  that its condition could not be true.
 *
 *  step_id is what the runtime actually carries; the capability prop resolved from the
 *  catalogue is the fallback for a workflow whose author renamed the step.
 */
//: A run that has not finished. Re-running one of these produces a duplicate workflow rather
//: than a retry, which is exactly what an operator hit: two runs for one project, both live.
const ACTIVE_RUN_STATES = new Set(["running", "waiting", "ready", "leased", "queued", "paused"]);

/** Open the console's side dock with a question already typed.
 *
 *  An operator reading a step that refuses to ship should not have to copy the error, find the
 *  Agents area and retype the context. The dock already exists at the shell level and is
 *  driven by two search params — this is the same handoff the Infra panel uses, so the
 *  workflow view gains the agent without growing a second chat surface of its own.
 *
 *  `agent` carries the RUN, so the conversation resolves what it is about server-side rather
 *  than the console shipping a context blob.
 */
function askBossAbout(runRef: string, question: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("agent", runRef || "general");
    url.searchParams.set("dock_ask", question);
    window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

/** BIND THIS APP TO A HOST, ON THE STEP WHERE SHIPPING IS DECIDED.
 *
 *  Asked for repeatedly, and what shipped each time was less than what was asked for: a bare
 *  host dropdown that fired on change. That is not a binding, it is a guess — it picks the
 *  name and the port for you and gives you nothing to confirm. The project card has had the
 *  real control all along (host · name · port · Set target), and the deploy step, which is
 *  exactly where an operator is deciding where this build goes, had a lesser version.
 *
 *  Same three fields, same explicit button, same endpoint the project card posts to. The
 *  operator sets the target and THEN approves the release; nothing is chosen on their behalf.
 */
function BindToHost({ hosts, appName, buildRunId, busy, onPrepareTarget, onBound }: {
  hosts: DeployHost[]; appName: string; buildRunId: string; busy: string | null;
  onPrepareTarget: (hostId: string) => void;
  /** Re-read the runs once the binding lands, so the step SHOWS what it is bound to. */
  onBound: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  return (
    <form
      className="dw-bind"
      data-testid="dw-bind-form"
      onSubmit={async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const fd = new FormData(e.currentTarget as HTMLFormElement);
        const hostId = String(fd.get("host_id") || "");
        if (!hostId) { setMsg("choose a host first"); return; }
        setSaving(true); setMsg(null);
        // WITHOUT A BUILD THERE IS NO APP TO BIND, so fall back to the run-scoped path that
        // creates the target from the workflow's own title. Both end at the same target row.
        if (buildRunId) {
          try {
            const r = await fetch("/api/app-deploy", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                op: "set-target", actor: "executive_console", run_id: buildRunId,
                host_id: hostId, app_name: fd.get("app_name"), app_port: fd.get("app_port"),
              }),
              cache: "no-store",
            });
            const d = await r.json().catch(() => ({}));
            setMsg(r.ok
              ? `bound to ${d?.host ?? "the host"} — approve the release below to ship it`
              : (d?.error ?? `could not set the target (HTTP ${r.status})`));
            // WITHOUT THIS THE SCREEN DOES NOT MOVE. The write succeeded and the step went on
            // showing the same empty form, which reads exactly like a button that did nothing.
            if (r.ok) onBound();
          } catch (err) {
            setMsg(String((err as Error)?.message ?? err));
          }
        } else {
          onPrepareTarget(hostId);
          setMsg("binding…");
        }
        setSaving(false);
      }}
    >
      <span className="detail">App&rsquo;s target:</span>
      <select name="host_id" className="mr-select" data-testid="dw-deploy-host"
              aria-label="Host to deploy this app to" defaultValue={hosts[0]?.id ?? ""}>
        {hosts.map((h) => (
          <option key={h.id} value={h.id}>
            {h.name}{h.address ? ` · ${h.address}` : ""}
          </option>
        ))}
      </select>
      {/* The DISPLAY name and the name a container is created under are not always the same
          string, and the second is the one a deploy has to live with. */}
      <input name="app_name" defaultValue={appName} data-testid="dw-deploy-appname"
             aria-label="App name on the host" style={{ width: 150, fontSize: 12 }} />
      <input name="app_port" placeholder="port (auto)" data-testid="dw-deploy-port"
             aria-label="App port — blank lets BOSS pick a free one"
             style={{ width: 96, fontSize: 12 }} />
      <button className="cap-btn" type="submit" disabled={saving || !!busy}
              data-testid="dw-deploy-settarget">
        {saving ? "Saving…" : "Set target"}
      </button>
      {msg ? <span className="detail dw-bind-msg">{msg}</span> : null}
    </form>
  );
}

/** The hosts an operator can actually ship to, out of whatever the endpoint answered.
 *
 *  `hosts`, not `items`. GET /v1/deployment-hosts answers {"hosts": [...]} while the TARGETS
 *  endpoint answers {"items": [...]}, and this read `items` from the hosts response — got
 *  undefined, filtered an empty list, and told the operator "this project has no verified
 *  host. Add one under Infra and Models" while Infra showed hetzner-ci-01 VERIFIED and READY
 *  TO DEPLOY. Extracted so the shape is asserted by a test rather than by a screenshot.
 */
export function verifiedHostsFrom(body: unknown): DeployHost[] {
  const b = (body ?? {}) as { hosts?: unknown; items?: unknown };
  const raw = Array.isArray(b.hosts) ? b.hosts : Array.isArray(b.items) ? b.items : [];
  return (raw as DeployHost[]).filter(
    (h) => ["verified", "ready", "active"].includes(h?.status ?? ""));
}

// Same lesson as isDeployStep: `capability` is not always on the wire, and gating the ONLY
// preview controls on it alone left a step titled "Preview" with no way to run one.
export function isPreviewStep(step: RunStep): boolean {
  return step.step_id === "preview" || step.capability === "preview";
}

export function isDeployStep(step: RunStep, capability?: Capability): boolean {
  return step.step_id === "deploy" || step.capability === "deploy"
    || capability?.key === "deploy";
}


function StepDetail({ step, capability, targets, hosts, runInput, onRetry, onSetTarget,
                     onPrepareTarget, onRunPreview, onBound, buildRunId,
                     previewStarting, busy }: {
  step: RunStep; capability?: Capability; targets: DeployTarget[]; hosts: DeployHost[];
  runInput: Record<string, unknown>;
  onRetry: () => void; onSetTarget: (targetId: string) => void;
  onPrepareTarget: (hostId: string) => void;
  /** The code task this run built — what a preview is started FOR. */
  buildRunId: string | null;
  onRunPreview: (buildRunId: string) => void;
  /** Re-read the runs — used after a binding lands so the step shows it. */
  onBound: () => void;
  /** The build whose preview was asked for and has not arrived yet. */
  previewStarting?: string;
  busy: string | null;
}) {
  const phase = phaseOf(step);
  // The sentence this step is actually showing the operator — its failure, or the note the
  // capability recorded. It is what makes the opening question specific instead of "help".
  const askNote = String(
    step.error || (step.output || {}).note || (step.output || {}).error || "",
  ).slice(0, 400);
  // The live preview is resolved per-step by the API and lives ON the step, not in the output
  // the runtime recorded — merged here so the link builder sees one object.
  const merged = { ...(step.output || {}),
                   ...(step.live_preview ? { live_preview: step.live_preview } : {}) };
  const links = stepLinks(merged);
  const previewHref = links.find((l) => l.label === "open the preview")?.href ?? null;
  // Asked for on THIS build and not yet serving. The URL is the finish line; a POST returning
  // is not — which is why the old busy flag cleared a second after the click.
  const starting = !!previewStarting && previewStarting === buildRunId && !previewHref;
  const approval = step.approval as { mode?: string; approver_role?: string };
  const produced = Object.entries(step.output || {})
    .filter(([k]) => !["capability", "tool", "inputs", "note", "status"].includes(k))
    // A LOOPBACK URL IS NOT AN ADDRESS ANYBODY CAN USE. `preview_url` is where the app listens
    // ON THE HOST — 127.0.0.1:42543 — and printing it under a heading reading PREVIEW URL
    // invites somebody to copy it, paste it, and get nothing. The link above is the openable
    // one; this grid is for facts, and that is not one.
    .filter(([, v]) => !(typeof v === "string" && isLoopbackPreviewUrl(v)))
    .slice(0, 6);

  return (
    <section className="dw-detail" data-phase={phase} data-testid="dw-detail">
      <header className="dw-detail-head">
        <span className="dw-badge"
              data-tone={phase === "skipped" ? "muted"
                : phase === "failed_on" ? "bad" : tone(step.status)}
              data-testid={phase === "failed_on" ? "dw-step-failed-on"
                : phase === "skipped" ? "dw-step-na" : undefined}>
          {phase === "failed_on" ? "failed — went on"
            : phase === "skipped" ? "not run" : step.status}
        </span>
        <h4>{capability?.label ?? step.capability ?? step.step_id}</h4>
        {stepDuration(step) ? (
          <span className="dw-step-took" data-testid="dw-step-took">{stepDuration(step)}</span>
        ) : null}
        {approval?.mode === "manual" ? (
          <span className="dw-gate" title="Requires a person">
            gate{approval.approver_role ? ` · ${approval.approver_role}` : ""}
          </span>
        ) : null}
        {step.due_at ? (
          <span className="detail dw-due">due {step.due_at.slice(0, 16).replace("T", " ")}</span>
        ) : null}
        {step.timeout_applied ? (
          <span className="dw-timeout">{step.timeout_applied.replace("_", " ")}</span>
        ) : null}
      </header>

      {capability?.summary ? <p className="dw-detail-what">{capability.summary}</p> : null}
      {step.error ? <p className="dw-step-error" data-testid="dw-step-error">{step.error}</p> : null}
      {step.output?.note ? (
        <p className="dw-step-note" data-tone={phase === "failed_on" ? "bad" : undefined}>
          {String(step.output.note)}
        </p>
      ) : null}
      {observedGates(step) ? (
        <p className="dw-step-gates" data-testid="dw-step-gates">{observedGates(step)}</p>
      ) : null}

      {/* CHOOSE WHERE IT SHIPS BEFORE IT ASKS TO SHIP.
          This picker existed only inside the approval block, so it appeared for a few seconds
          at the gate and not before — and if the run was not sitting at the gate there was no
          way to set a target at all. The operator reported exactly that: no option on the
          deploy step to point it at a host. It belongs to the step for its whole life, from
          the moment you can see the step until it has shipped. */}
      {/* ALWAYS ON THE DEPLOY STEP. NO CONDITIONS.
          This was gated on `!succeeded && !needsYou`, which hid it on precisely the step the
          operator was looking at — a deploy that had already reported succeeded. They asked
          for this control repeatedly and it was never once on screen, because each version of
          the condition excluded the state their run happened to be in.

          Binding an app to a host is not a thing you do only while a step is pending: after a
          failed or wrongly-"succeeded" deploy you need to set the target and run it again,
          which is exactly the moment the old condition removed it. */}
      {isDeployStep(step, capability) ? (
        <div className="dw-step-target" data-testid="dw-step-target">
          <DeployWhere targets={targets} hosts={hosts} runInput={runInput}
                       buildRunId={buildRunId ?? ""} busy={busy}
                       onSetTarget={onSetTarget} onPrepareTarget={onPrepareTarget}
                       onBound={onBound} />
        </div>
      ) : null}

      {/* ASK ABOUT THIS STEP, WITHOUT LEAVING IT.
          A step that will not ship states its reason and then leaves the operator to act on
          it alone. The dock can read the run, the code, the host and the logs — so the
          question it opens with is this step's own words, not a blank box. */}
      <div className="dw-step-ask">
        <button type="button" className="cap-btn" data-testid="dw-ask-boss"
                onClick={() => askBossAbout(
                  buildRunId || "",
                  `On the ${capability?.label ?? step.step_id} step`
                  + ` (status ${step.status})`
                  + (askNote ? `, BOSS says: "${askNote}"` : "")
                  + ". Why is it in this state, and what is the fix? Check the app's code and"
                  + " the host before answering.")}>
          Ask BOSS about this step
        </button>
        <span className="detail">
          Opens the side dock with this step&rsquo;s state and message already filled in.
        </span>
      </div>

      {/* BACK IN THE QUEUE. A step that failed for a reason since dealt with — a target that
          did not exist, a service that was down — had no way back: the run sat stopped and the
          only recourse was to start the whole workflow again and repeat everything before it. */}
      {["failed", "deadlettered", "cancelled"].includes(step.status) ? (
        <div className="dw-step-decide">
          <button type="button" className="cap-btn primary" onClick={onRetry} disabled={!!busy}
                  data-testid="dw-step-retry">
            {busy === "Retry" ? "Queueing…" : `Try ${capability?.label ?? step.step_id} again`}
          </button>
          <span className="detail">Runs this step only; everything before it stays done.</span>
        </div>
      ) : null}

      {/* ANSWER THE APPROVAL WHERE IT IS SEEN. A gate answerable only in another area makes the
          operator leave the screen that told them it was waiting. */}
      {needsYou(step) ? (
        <div className="dw-step-decide" data-testid="dw-step-decide">
          {/* The target picker is rendered once, above, for every deploy step regardless of
              state — including this one. A second copy here would show two pickers at the
              gate. */}
          {/* DECIDE WHERE THE DECISION CAN BE READ.
              Two buttons here answered a release without showing what was being released — no
              recommendation, no cost, no risk tier, no note, and no record of who else is
              waiting on the same call. Approving a deployment off a step badge is a decision
              made blind. The Approvals page is where that context lives, so this goes there
              with the decision already selected. */}
          <a className="cap-btn primary dw-decide-btn" data-testid="dw-step-review"
             href={consoleHref(typeof window !== "undefined" ? window.location.search : "",
                               { area: "decisions", decision_filter: "needs_action",
                                 decision: step.decision_id })}>
            Review and decide →
          </a>
          <span className="detail">
            {(step.approval as { approver_role?: string })?.approver_role
              ? `${(step.approval as { approver_role?: string }).approver_role} signs this off.`
              : "Opens the approval with its recommendation, cost and risk."}
          </span>
        </div>
      ) : null}

      {/* RUN IT, OR OPEN IT. The preview step reported what it had recorded and offered no way
          to do anything: no button to start one, and — once the recorded preview expired — no
          link either. Factory has had "Open preview" and a way to start one all along; this is
          the same two controls, on the step whose whole subject is the preview. */}
      {isPreviewStep(step) ? (
        <div className="dw-step-decide" data-testid="dw-preview-controls">
          {/* Pending until the URL arrives: the request was accepted in a second, the app
              takes a minute. Cleared by the URL showing up, not by the POST returning. */}
          {previewHref ? (
            <a className="cap-btn primary" href={previewHref} target="_blank" rel="noreferrer"
               data-testid="dw-preview-open">Open preview</a>
          ) : null}
          {buildRunId ? (
            <button type="button" className="cap-btn"
                    disabled={!!busy || starting}
                    data-testid="dw-preview-run" onClick={() => onRunPreview(buildRunId)}>
              {starting ? "Starting…"
                : previewHref ? "Start a fresh one" : "Run preview"}
            </button>
          ) : null}
          <span className="detail" data-testid="dw-preview-hint">
            {starting
              // Said for the WHOLE wait, not for the second the POST takes. A preview boots a
              // container and waits for it to serve; a minute of silence after a click reads
              // as a click that did nothing.
              ? "Starting a preview — this takes up to a minute. It will appear here on its own."
              : previewHref
                ? (step.live_preview && step.live_preview.same_build === false
                    ? "This build has no preview running. The link opens a live preview of the "
                      + "same app from a later build — start a fresh one to preview THIS build."
                    : "A live preview of this build, on a URL you can open.")
                : "Nothing is serving this build right now. Starting one takes a few seconds."}
          </span>
        </div>
      ) : null}

      {/* WHERE THE WORK IT PRODUCED LIVES. Debugging a step means reading what it actually did. */}
      {links.length ? (
        <div className="dw-step-links" data-testid="dw-step-links">
          {links.map((l) => (
            l.external ? (
              <a key={l.label} href={l.href} target="_blank" rel="noreferrer"
                 className="dw-step-link">{l.label} ↗</a>
            ) : (
              <a key={l.label} href={l.href} className="dw-step-link">{l.label}</a>
            )
          ))}
        </div>
      ) : null}

      {produced.length ? (
        <dl className="dw-step-out" data-testid="dw-step-out">
          {produced.map(([k, v]) => (
            <div key={k}>
              <dt>{k.replace(/_/g, " ")}</dt>
              <dd>{summarise(v)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </section>
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
  // WHAT THE STEP KNOWS. A binding says which tool runs; it says nothing about the standard
  // the step has to hold to, and that lived in the head of whoever defined it.
  const [knowledge, setKnowledge] = useState("");
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
        knowledge: knowledge.trim(),
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
      {/* WHAT THIS STEP KNOWS. Carried into every execution, so the step behaves the same on
          its hundredth run as on the one where somebody was watching. */}
      <label className="field">
        <span>What should it know?</span>
        <textarea className="textarea" rows={4} value={knowledge}
                  data-testid="dw-step-knowledge"
                  placeholder="House style, the clauses that matter, the internal doc that explains this system — anything the step must hold to that its tool does not already know."
                  onChange={(e) => setKnowledge(e.target.value)} />
        <span className="detail">
          Optional. Handed to the skill or tool on every run as context.
        </span>
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
    return uniqueWorkflowName(source.name, installed.map((i) => i.name));
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
        {/* THE WAY TO MAKE A STEP, AT THE TOP, WHERE SOMEBODY LOOKING FOR IT WILL LOOK.
            It was moved next to the "Your steps" sub-heading once already, which is still
            below fourteen built-in capabilities: on a normal screen you scroll past the whole
            palette before the control appears, and the operator reported twice that there was
            no obvious way to add a step. The one below stays — this is the discoverable one. */}
        <div className="dw-palette-heading-row">
          <h4 className="dw-palette-heading">Steps</h4>
          {!defining ? (
            <button type="button" className="cap-btn primary dw-define" disabled={!!busy}
                    onClick={() => setDefining(true)} data-testid="dw-define-step-top">
              + New step
            </button>
          ) : null}
        </div>
        <p className="detail dw-palette-summary">
          Click a step to add it, or make one of your own that runs your skill or an MCP tool.
        </p>
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
        {/* THE CONTROL WITH ITS HEADING, not below whatever happens to be listed. With eleven
            of the customer's own steps the button sat off the bottom of the panel, so the way
            to make one was the one thing you had to scroll to find. */}
        <div className="dw-palette-heading-row">
          <h4 className="dw-palette-heading">Your steps</h4>
          {!defining ? (
            <button type="button" className="cap-btn dw-define" disabled={!!busy}
                    onClick={() => setDefining(true)} data-testid="dw-define-step">
              + Define a step
            </button>
          ) : null}
        </div>
        {defining ? (
          <DefineStepForm bindable={bindable} riskFloor={riskFloor} busy={busy}
                          projectId={projectId}
                          onCancel={() => setDefining(false)}
                          onDefine={async (fields) => {
                            const result = await onDefineStep(fields);
                            if (result.ok) setDefining(false);
                            return result;
                          }} />
        ) : null}
        {customSteps.length === 0 && !defining ? (
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
          {/* MAKING ONE COMES FIRST. Somebody who opens "Build your own" is usually here to
              build one; editing is what you do on a return visit. Reading order should match
              that, and the first tab is where the eye lands. */}
          <button type="button" role="tab" className="dw-mode-tab"
                  aria-selected={creating} data-testid="dw-tab-new"
                  disabled={!!busy}
                  onClick={() => { if (!creating) beginCreating(); }}>
            <WfIcon id="plus" /> Create a new workflow
          </button>
          <button type="button" role="tab" className="dw-mode-tab"
                  aria-selected={!creating} data-testid="dw-tab-edit"
                  disabled={!!busy}
                  onClick={() => { if (creating) cancelCreating(); }}>
            <WfIcon id="edit" /> Edit an existing workflow
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
