import { NextResponse } from "next/server";

import { API_URL, authHeaders } from "../../../lib/client";
import { apiWrite } from "../../../lib/api-write";
import { refusalFrom } from "../../../lib/start-refusal";
import { rawRequestUrl } from "../../../lib/raw-request-url";

/**
 * Everything the delivery-workflow screen reads and does, in one place.
 *
 * A route handler rather than Server Actions: this screen refreshes while a workflow is
 * running, and a Server Action reply re-renders the whole console route — which on v1 costs
 * seconds. `view` and `action` select the upstream call so the screen needs one client module
 * rather than nine.
 */
export const dynamic = "force-dynamic";

const VIEWS: Record<string, (project: string) => string> = {
  catalogue: () => "/v1/delivery-workflows/catalogue",
  capabilities: (p) => `/v1/delivery-workflows/capabilities?project_id=${encodeURIComponent(p)}`,
  custom_steps: (p) =>
    `/v1/delivery-workflows/custom-steps?project_id=${encodeURIComponent(p)}`,
  installed: (p) => `/v1/delivery-workflows?project_id=${encodeURIComponent(p)}`,
  // The Workflow Studio library: the same list, read by lib/workflow-api.ts `fetchLibrary`,
  // which also uses the fields the list gained for it (copies, run count, last run).
  library: (p) => `/v1/delivery-workflows?project_id=${encodeURIComponent(p)}`,
  runs: (p) => `/v1/delivery-workflows/runs?project_id=${encodeURIComponent(p)}`,
  schedules: (p) =>
    `/v1/delivery-workflows/schedules?project_id=${encodeURIComponent(p)}`,
  // Where a build can go. The registry has always existed and nothing over HTTP could read it,
  // so the form that starts work could not offer a choice and every build went to whatever the
  // deployment's global default happened to be.
  repositories: (p) => `/v1/projects/${encodeURIComponent(p)}/repositories`,
  // What each workflow has cost before, from its real runs: New project's "Builds like this
  // cost $a–$b". No history, no line.
  costs: (p) => `/v1/delivery-workflows/costs?project_id=${encodeURIComponent(p)}`,
  // Every time the operator will be asked, per path — what New project's plan counts from.
  asks: (p) => `/v1/delivery-workflows/asks?project_id=${encodeURIComponent(p)}`,
};

/** `view=spec&key=…[&version=n]` — the steps a workflow currently has (or had at version n),
 *  so the editor opens with them rather than making a customer rebuild ten steps to change
 *  one. */
function specPath(project: string, key: string, version: string | null): string {
  return `/v1/delivery-workflows/${encodeURIComponent(key)}/spec`
    + `?project_id=${encodeURIComponent(project)}`
    + (version && /^\d+$/.test(version) ? `&version=${version}` : "");
}

/** `view=plan&key=…` — what the workflow WILL DO, as opposed to which steps it has.
 *
 *  `spec` returns the stored graph, which is what the builder edits. It says a step's id and
 *  its binding and nothing about what the step means: an operator reading it learns that
 *  "publish" runs `social_publish` and still does not know what that produces, who answers
 *  for it, or that it will stop and wait for them. This is the same version, joined to the
 *  capability catalogue, in the words a person decides on. */
function planPath(project: string, key: string): string {
  return `/v1/delivery-workflows/${encodeURIComponent(key)}/plan`
    + `?project_id=${encodeURIComponent(project)}`;
}

/** `view=readiness&key=…` — what this workflow needs from this project, each item checked,
 *  with the fix. The same answer Start gives when it refuses, asked before pressing it. */
function readinessPath(project: string, key: string, input: string | null): string {
  return `/v1/delivery-workflows/${encodeURIComponent(key)}/readiness`
    + `?project_id=${encodeURIComponent(project)}`
    + (input ? `&input=${encodeURIComponent(input)}` : "");
}

export async function GET(req: Request) {
  const params = rawRequestUrl(req).searchParams;
  const view = params.get("view") ?? "runs";
  const project = params.get("project_id")?.trim() ?? "";
  // A workflow's webhook triggers, and one trigger's recent deliveries (#512). Never a secret:
  // the API returns it only from create and rotate.
  if (view === "webhooks" || view === "webhook_deliveries") {
    const key = params.get("key")?.trim() ?? "";
    const webhookId = params.get("webhook_id")?.trim() ?? "";
    if (!project || (view === "webhook_deliveries" && !webhookId)) {
      return NextResponse.json({ error: `${view} needs project_id${view === "webhooks" ? "" : " and webhook_id"}` },
                               { status: 400 });
    }
    const path = view === "webhooks"
      ? `/v1/delivery-workflows/webhooks?project_id=${encodeURIComponent(project)}`
        + (key ? `&workflow_key=${encodeURIComponent(key)}` : "")
      : `/v1/delivery-workflows/webhooks/${encodeURIComponent(webhookId)}/deliveries`
        + `?project_id=${encodeURIComponent(project)}`;
    try {
      const r = await fetch(`${API_URL}${path}`, { headers: authHeaders({}), cache: "no-store" });
      const raw = await r.text();
      let parsed: unknown;
      try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
      return r.ok ? NextResponse.json(parsed)
        : NextResponse.json(refusal(r.status, parsed), { status: r.status });
    } catch (e) {
      return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
    }
  }
  if (view === "spec" || view === "plan" || view === "readiness") {
    const key = params.get("key")?.trim() ?? "";
    if (!project || !key) {
      return NextResponse.json({ error: `${view} needs project_id and key` }, { status: 400 });
    }
    try {
      const path = view === "plan" ? planPath(project, key)
        : view === "readiness" ? readinessPath(project, key, params.get("input"))
        : specPath(project, key, params.get("version"));
      const r = await fetch(`${API_URL}${path}`, {
        headers: authHeaders({}), cache: "no-store",
      });
      if (!r.ok) {
        // The API's own sentence ("no workflow 'x'"), so the editor can say what is wrong.
        const raw = await r.text();
        let parsed: unknown;
        try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
        return NextResponse.json(refusal(r.status, parsed), { status: r.status });
      }
      return NextResponse.json(await r.json());
    } catch (e) {
      return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
    }
  }
  const build = VIEWS[view];
  if (!build) {
    return NextResponse.json(
      { error: `unknown view '${view}'. Known: ${Object.keys(VIEWS).join(", ")}, spec, plan` },
      { status: 400 },
    );
  }
  if (!project && view !== "catalogue" && view !== "capabilities") {
    return NextResponse.json({ error: "missing project_id" }, { status: 400 });
  }
  try {
    const r = await fetch(`${API_URL}${build(project)}`, {
      headers: authHeaders({}),
      cache: "no-store",
    });
    if (!r.ok) {
      return NextResponse.json({ error: `API returned ${r.status}` }, { status: r.status });
    }
    return NextResponse.json(await r.json());
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

/**
 * The API's refusal, as the console reads it. A start that needs the person's consent (409
 * consent_required, IDENTITY.md §7.3) also keeps the API's whole body under `consent`, so the
 * console can show the consent sheet and re-send the same start (lib/identity/consent.ts).
 */
function refusal(status: number, parsed: unknown) {
  const consent = status === 409 && (parsed as { error?: unknown } | null)?.error === "consent_required";
  return { ...refusalFrom(status, parsed), ...(consent ? { consent: parsed } : {}) };
}

const ACTIONS: Record<string, string> = {
  install: "/v1/delivery-workflows/catalogue/install",
  apply: "/v1/delivery-workflows/apply",
  create: "/v1/delivery-workflows/create",
  fork: "/v1/delivery-workflows/fork",
  save: "/v1/delivery-workflows/save",
  start: "/v1/delivery-workflows/start",
  advance: "/v1/delivery-workflows/run-capabilities",
  define_step: "/v1/delivery-workflows/custom-steps",
  schedule: "/v1/delivery-workflows/schedules",
  // A new webhook trigger: the reply is the one time its signing secret is shown (#512).
  webhook_create: "/v1/delivery-workflows/webhooks",
  // The Workflow Studio editor (lib/workflow-api.ts). `check` and `publish` run the same
  // collector, so a check that passes is a publish that passes; `draft` and `wire` write
  // nothing. A refusal keeps its `problems` and `suggested_name` (lib/start-refusal.ts).
  check: "/v1/delivery-workflows/check",
  publish: "/v1/delivery-workflows/publish",
  draft: "/v1/delivery-workflows/draft",
  wire: "/v1/delivery-workflows/wire",
};

/** Actions addressed to ONE run. Kept apart because their path carries the run id. */
/** One POST to the API, with its own message forwarded verbatim.
 *
 *  Extracted when schedules added a second POST shape: two copies of the fetch/parse/forward
 *  dance drift, and the one that drifts is the one that swallows the API's sentence. */
async function proxyPost(path: string, payload: unknown) {
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    const raw = await r.text();
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
    if (!r.ok) {
      // A REFUSAL CAN CARRY A CHECKLIST — flattened by the one shared helper (see it for why).
      return NextResponse.json(refusal(r.status, parsed), { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

async function proxyDelete(path: string) {
  return proxyWrite("DELETE", path, null);
}

/** One PATCH or DELETE to the API, its refusal read by the one shared helper. */
async function proxyWrite(method: "PATCH" | "DELETE", path: string, payload: unknown) {
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method,
      headers: authHeaders(payload === null ? {} : { "Content-Type": "application/json" }),
      ...(payload === null ? {} : { body: JSON.stringify(payload) }),
      cache: "no-store",
    });
    const raw = await r.text();
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
    if (!r.ok) {
      return NextResponse.json(refusal(r.status, parsed), { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

/** Actions addressed to ONE schedule. Its id lives in the path, like a run's does. */
const SCHEDULE_ACTIONS: Record<string, (id: string) => string> = {
  schedule_enabled: (id) =>
    `/v1/delivery-workflows/schedules/${encodeURIComponent(id)}/enabled`,
};

/** Actions addressed to ONE webhook trigger (#512). Its id lives in the path. */
const WEBHOOK_ACTIONS: Record<string, (id: string) => string> = {
  webhook_rotate: (id) => `/v1/delivery-workflows/webhooks/${encodeURIComponent(id)}/rotate`,
  webhook_enabled: (id) => `/v1/delivery-workflows/webhooks/${encodeURIComponent(id)}/enabled`,
};

const RUN_ACTIONS: Record<string, (runId: string) => string> = {
  retry_step: (id) => `/v1/delivery-workflows/runs/${encodeURIComponent(id)}/retry-step`,
  // The other way past a failed step: go on without it. A failed step STOPS its run, for every
  // kind of step, so the operator decides — retry it, or skip it with a recorded reason.
  skip_step: (id) => `/v1/delivery-workflows/runs/${encodeURIComponent(id)}/skip-step`,
  // Re-test what stopped this run and put back whatever can now proceed. Addressed to the RUN,
  // not to a step: an operator arriving at a stalled workflow is asking "is this still true?"
  // of the whole thing, and does not know which step to point at.
  recheck: (id) => `/v1/delivery-workflows/runs/${encodeURIComponent(id)}/recheck`,
  set_input: (id) => `/v1/delivery-workflows/runs/${encodeURIComponent(id)}/input`,
  prepare_target: (id) =>
    `/v1/delivery-workflows/runs/${encodeURIComponent(id)}/deploy-target`,
  // Starting a preview is a CODE TASK operation — the same endpoint Factory calls — so the
  // run id here is the build's, not the workflow's.
  start_preview: (id) => `/v1/code-tasks/${encodeURIComponent(id)}/preview`,
  // STOP. The screen told an operator with three live runs to "cancel this run first" and
  // offered no way to do it — advice naming an action the product did not expose. Cancel
  // stops the WORK, not just the bookkeeping: it also cancels the code task a build step
  // started, which is the distinction that once cost a production database.
  stop: (id) => `/v1/workflow-runs/${encodeURIComponent(id)}/cancel`,
};

export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "body must be JSON" }, { status: 400 });
  }
  const action = String(body.action ?? "");
  const scheduleAction = SCHEDULE_ACTIONS[action];
  if (scheduleAction) {
    const id = String((body as { schedule_id?: string }).schedule_id ?? "");
    if (!id) {
      return NextResponse.json({ error: `'${action}' needs a schedule_id` }, { status: 400 });
    }
    const { action: _a, schedule_id: _s, ...rest } = body;
    return proxyPost(scheduleAction(id), rest);
  }
  const webhookAction = WEBHOOK_ACTIONS[action];
  if (webhookAction) {
    const id = String((body as { webhook_id?: string }).webhook_id ?? "");
    if (!id) {
      return NextResponse.json({ error: `'${action}' needs a webhook_id` }, { status: 400 });
    }
    const { action: _a, webhook_id: _w, ...rest } = body;
    return proxyPost(webhookAction(id), rest);
  }
  const runAction = RUN_ACTIONS[action];
  const runId = String((body as { run_id?: string }).run_id ?? "");
  const path = runAction ? (runId ? runAction(runId) : "") : ACTIONS[action];
  if (runAction !== undefined && !runId) {
    return NextResponse.json({ error: `'${action}' needs a run_id` }, { status: 400 });
  }
  if (!path) {
    return NextResponse.json(
      { error: `unknown action '${action}'. Known: ${Object.keys(ACTIONS).join(", ")}` },
      { status: 400 },
    );
  }
  const { action: _drop, ...payload } = body;
  try {
    const r = await apiWrite(`${API_URL}${path}`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    const raw = await r.text();
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
    if (!r.ok) {
      // The API's own message, forwarded verbatim. These are the sentences the builder shows
      // beside a step — "step 'd' is risk tier 2 and always requires a person" is the whole
      // point, and replacing it with "request failed" would waste it.
      return NextResponse.json(refusal(r.status, parsed), { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}

/** Removing a step a customer defined. Separate from POST because it is the one destructive
 *  thing this screen does, and routing it through the same `action` switch as "save" would
 *  make a typo in an action name capable of deleting something. */
/** Change a step that already exists (`step_key`), or rename a workflow (`definition_id`).
 *
 *  Its own verb rather than another POST action, because the key stays in the PATH: the key is
 *  what workflow specs bind to, so it is the one thing an edit must not be able to change. A
 *  body that carried it would invite exactly that. A workflow's name and description change in
 *  place the same way — no new version — and its id is likewise the path, not the body. */
export async function PATCH(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "body must be JSON" }, { status: 400 });
  }
  const definitionId = String(body.definition_id ?? "").trim();
  if (definitionId) {
    const { definition_id: _id, step_key: _key, ...rename } = body;
    return proxyWrite("PATCH",
      `/v1/delivery-workflows/${encodeURIComponent(definitionId)}`, rename);
  }
  const key = String(body.step_key ?? "").trim();
  if (!key) {
    return NextResponse.json({ error: "step_key is required" }, { status: 400 });
  }
  const { step_key: _drop, ...payload } = body;
  // Refusals are forwarded verbatim, like every other message from the API here: "choose the
  // skill this step runs" is the sentence the form shows, and "request failed" would waste it.
  return proxyWrite("PATCH",
    `/v1/delivery-workflows/custom-steps/${encodeURIComponent(key)}`, payload);
}

export async function DELETE(req: Request) {
  const params = rawRequestUrl(req).searchParams;
  const project = params.get("project_id")?.trim() ?? "";
  const key = params.get("step_key")?.trim() ?? "";
  const actor = params.get("actor")?.trim() || "operator";
  const scheduleId = params.get("schedule_id")?.trim() ?? "";
  // REMOVING A FINISHED RUN. A DELETE verb rather than another POST action, for the reason
  // given above the step delete: an action name is a string, and a typo in one must not be
  // able to destroy something.
  // DELETING (OR RETIRING) A WORKFLOW — the Library's Delete. The API retires instead when the
  // workflow has runs, refuses a standard (403) and one with live runs (409). The actor is the
  // signed-in member the API reads from the credentials, never a name from this request.
  const definitionId = params.get("definition_id")?.trim() ?? "";
  if (definitionId) {
    if (!project) {
      return NextResponse.json({ error: "project_id is required" }, { status: 400 });
    }
    return proxyDelete(`/v1/delivery-workflows/${encodeURIComponent(definitionId)}`
      + `?project_id=${encodeURIComponent(project)}`);
  }
  const runId = params.get("run_id")?.trim() ?? "";
  if (runId) {
    if (!project) {
      return NextResponse.json({ error: "project_id is required" }, { status: 400 });
    }
    return proxyDelete(
      `/v1/delivery-workflows/runs/${encodeURIComponent(runId)}`
        + `?project_id=${encodeURIComponent(project)}&actor=${encodeURIComponent(actor)}`);
  }
  if (scheduleId) {
    if (!project) {
      return NextResponse.json({ error: "project_id is required" }, { status: 400 });
    }
    return proxyDelete(
      `/v1/delivery-workflows/schedules/${encodeURIComponent(scheduleId)}`
        + `?project_id=${encodeURIComponent(project)}&actor=${encodeURIComponent(actor)}`);
  }
  if (!project || !key) {
    return NextResponse.json({ error: "project_id and step_key are required" }, { status: 400 });
  }
  try {
    const r = await apiWrite(
      `${API_URL}/v1/delivery-workflows/custom-steps/${encodeURIComponent(key)}`
        + `?project_id=${encodeURIComponent(project)}&actor=${encodeURIComponent(actor)}`,
      { method: "DELETE", headers: authHeaders({}), cache: "no-store" },
    );
    const raw = await r.text();
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { parsed = { detail: raw }; }
    if (!r.ok) {
      return NextResponse.json(refusal(r.status, parsed), { status: r.status });
    }
    return NextResponse.json(parsed);
  } catch (e) {
    return NextResponse.json({ error: String((e as Error)?.message ?? e) }, { status: 502 });
  }
}
