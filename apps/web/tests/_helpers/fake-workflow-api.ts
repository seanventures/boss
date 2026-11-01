/**
 * A stand-in for the /api/delivery-workflows proxy, at the fetch boundary.
 *
 * The editor tests mount the real editor: the real store and API client (lib/workflow-*), the
 * real save flow, inspector and JSON view. Only `fetch` is replaced, by this: it answers the
 * proxy's reads (capabilities, custom steps, library, one workflow's spec) from fixtures in the
 * API's own wire shapes, records every write, and answers check, publish, draft and delete the
 * way the API does. So a test can say "Save made exactly one publish call, with these fields".
 */
import type { Palette, Spec } from "../../lib/workflow-editor-types";

type Obj = Record<string, unknown>;

export type FakeWorkflow = {
  definition_id: string; workflow_key: string; name: string; is_catalogue: boolean; version: number;
  spec: Spec; versions?: { version: number; spec: Spec }[];
  copy?: { definition_id: string; workflow_key: string; name: string } | null;
  /** The run the Pipeline tab's "after a run" trace table reads (KG P6b). */
  last_run?: { id: string; status: string; at: string } | null;
};

/** `view=context_trace`, by run id — what each step of the run read and wrote in the graph
 *  (`fetchRunContext`, the Pipeline tab's last run). */
export type FakeRunContext = Record<string, { status: string; started_at?: string;
  steps: Record<string, { status: string; reads?: Obj[]; writes?: Obj[]; calls?: Obj[] }> }>;

export type FakeApi = {
  workflows: FakeWorkflow[];
  palette: Palette;
  writes: Obj[];
  /** Problems `check` reports (and `publish` refuses with). */
  problems: { step_id: string | null; field: string | null; message: string }[];
  /** The draft endpoint's answer; null answers 409 "no model". */
  draft: { name: string; spec: Spec } | null;
  fetch: (input: unknown, init?: RequestInit) => Promise<Response>;
};

function paletteWire(p: Palette) {
  return {
    capabilities: {
      capabilities: p.capabilities,
      blocks: p.blocks,
      agents: p.agents.map((a) => ({ role_key: a.role_key, name: a.name, holds: a.skills, mcp: a.mcp })),
      human_role: p.humanRole,
      manual_approval_risk_floor: p.riskFloor,
    },
    custom_steps: {
      steps: p.customSteps,
      bindable: {
        skills: p.bindable.skills.map((s) => ({ skill_id: s.id, label: s.name })),
        mcp_servers: p.bindable.mcp_servers,
      },
    },
  };
}

export function fakeWorkflowApi(palette: Palette, workflows: FakeWorkflow[],
                                runContext: FakeRunContext = {}): FakeApi {
  const api: FakeApi = {
    workflows, palette, writes: [], problems: [], draft: null,
    fetch: async (input, init) => {
      const url = new URL(String(input), "http://console.test");
      const method = (init?.method ?? "GET").toUpperCase();
      const wire = paletteWire(api.palette);
      if (method === "GET") {
        const view = url.searchParams.get("view");
        if (view === "capabilities") return Response.json(wire.capabilities);
        if (view === "custom_steps") return Response.json(wire.custom_steps);
        if (view === "library") {
          return Response.json({ items: api.workflows.map((w) => ({
            definition_id: w.definition_id, workflow_key: w.workflow_key, name: w.name,
            is_catalogue: w.is_catalogue, version: w.version, run_count: 0,
            last_run: w.last_run ?? null,
            steps: w.spec.steps.map((s) => ({ id: s.id, title: s.title ?? s.id,
                                              capability: (s.binding as Obj | undefined)?.capability })),
          })) });
        }
        if (view === "context_trace") {
          const runId = url.searchParams.get("run_id") ?? "";
          const run = runContext[runId];
          if (!run) return Response.json({ error: "no such run" }, { status: 404 });
          return Response.json({ run_id: runId, completed_at: null, started_at: null, ...run });
        }
        if (view === "spec") {
          const w = api.workflows.find((x) => x.workflow_key === url.searchParams.get("key"));
          if (!w) return Response.json({ error: "No such workflow" }, { status: 404 });
          const wanted = Number(url.searchParams.get("version")) || w.version;
          const versions = w.versions ?? [{ version: w.version, spec: w.spec }];
          const at = versions.find((v) => v.version === wanted) ?? { version: w.version, spec: w.spec };
          return Response.json({
            definition_id: w.definition_id, workflow_key: w.workflow_key, name: w.name, description: "",
            is_catalogue: w.is_catalogue, version: at.version, spec: at.spec, copy: w.copy ?? null,
            versions: versions.map((v) => ({ version: v.version, published_at: "2026-10-01T10:00:00Z",
                                             is_active: v.version === w.version })),
          });
        }
        return Response.json({});
      }
      const body = init?.body ? JSON.parse(String(init.body)) as Obj : {};
      const write = { method, ...body, ...(method === "DELETE" ? Object.fromEntries(url.searchParams) : {}) };
      api.writes.push(write);
      if (method === "DELETE") return Response.json({ outcome: "deleted" });
      const spec = (body.spec ?? { steps: [] }) as Spec;
      const summary = { steps: spec.steps.length, agents: [], approvals: [], typical_minutes: null, needs: [] };
      if (body.action === "check") {
        return Response.json({ ok: !api.problems.length, problems: api.problems, summary });
      }
      if (body.action === "publish") {
        if (api.problems.length) return Response.json({ problems: api.problems }, { status: 422 });
        const existing = api.workflows.find((w) => w.definition_id === body.definition_id)
          ?? api.workflows.find((w) => w.definition_id === body.update_copy_id);
        if (existing && !existing.is_catalogue) {
          existing.version += 1;
          existing.spec = spec;
          return Response.json({ definition: { id: existing.definition_id, workflow_key: existing.workflow_key,
                                               name: existing.name }, version: existing.version, created: false });
        }
        const made: FakeWorkflow = {
          definition_id: `wfd_${api.workflows.length + 1}`, name: String(body.name),
          workflow_key: String(body.name).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, ""),
          is_catalogue: false, version: 1, spec,
        };
        api.workflows.push(made);
        return Response.json({ definition: { id: made.definition_id, workflow_key: made.workflow_key, name: made.name },
                               version: 1, created: true });
      }
      if (body.action === "draft") {
        return api.draft ? Response.json(api.draft) : Response.json({ detail: "no model" }, { status: 409 });
      }
      return Response.json({ ok: true });
    },
  };
  return api;
}
