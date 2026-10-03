/**
 * Every door that starts work forwards a refusal as a SENTENCE, with the checklist beside it,
 * and every screen that starts work draws that checklist with "Start anyway".
 *
 * THE INCIDENT (review of the readiness change, 2026-10-02). Start began answering 409 with a
 * structured detail — `{message, readiness}` — when the project lacks what a workflow needs.
 * `/api/builds` forwarded `detail` as `error`, the Ask-BOSS hero rendered `⚠ {error}`, and
 * React refused to draw an object: the headline Build button crashed on any project with no
 * deployment host, where it used to build to a PR and a preview. Only one of four start
 * proxies had been taught to flatten it, by hand; Re-run, Build on this research, the run
 * report's Fix and Agent Studio's Run showed a sentence at best and had no way past.
 *
 * These ENUMERATE the doors rather than name them, so a door added later is held to the same
 * rule without anybody remembering this file.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { codeOnly } from "./_helpers/source";
import { refusalFrom } from "../lib/start-refusal";

vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  API_KEY: "",
  authHeaders: (extra: Record<string, string>) => ({ Authorization: "Bearer t", ...extra }),
}));
vi.mock("../lib/api-write", () => ({
  apiWrite: (url: string, init: RequestInit) => fetch(url, init),
  pressToken: async () => "press",
}));

afterEach(() => vi.unstubAllGlobals());

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (name === "node_modules" || name.startsWith(".")) continue;
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\./.test(name)) out.push(path);
  }
  return out;
}

const ALL = [...sources("app"), ...sources("lib")];

/** Server-side code that sends a start to the API. */
const START_TO_API = /\/v1\/delivery-workflows\/start|["'`]\/v1\/workflow-runs["'`]/;
/** Browser code that asks for a start through our own routes or actions. */
const START_FROM_SCREEN =
  /action: "start"|["'`]\/api\/builds["'`]|act\([^)]*"start"|\bpost\("start"/;

const READINESS = {
  ready: false,
  checked: [{ key: "deployment_target" }],
  missing: [{ key: "deployment_target", label: "Somewhere to deploy", fix: "Add a host.",
              area: "delivery", ok: false, detail: "no host",
              needed_by: [{ step_id: "deploy", label: "Deploy" }],
              first_step_label: "Deploy", after_a_person: true }],
};

describe("the shared refusal helper", () => {
  it("turns the checklist refusal into a sentence plus the checklist", () => {
    const flat = refusalFrom(409, { detail: { message: "cannot run here yet", readiness: READINESS } });
    expect(flat).toEqual({ error: "cannot run here yet", readiness: READINESS });
    expect(typeof flat.error).toBe("string");
  });

  it("keeps a plain sentence, joins a validation list, and never returns an object", () => {
    expect(refusalFrom(400, { detail: "no such workflow" })).toEqual({ error: "no such workflow" });
    expect(refusalFrom(422, { detail: [{ msg: "a" }, { msg: "b" }] })).toEqual({ error: "a; b" });
    expect(refusalFrom(500, {})).toEqual({ error: "API returned 500" });
    expect(typeof refusalFrom(409, { detail: { odd: 1 } }).error).toBe("string");
  });
});

describe("every door that sends a start to the API", () => {
  const doors = ALL.filter((path) => START_TO_API.test(codeOnly(path)));

  it("is found — the enumeration is not empty", () => {
    expect(doors).toEqual(expect.arrayContaining([
      "app/api/builds/route.ts", "app/api/delivery-workflows/route.ts",
    ]));
    // Agent Studio's Run door (POST /v1/workflow-runs) is gone: every Start is the composer.
    expect(doors).not.toContain("app/agent-studio-actions.ts");
  });

  it.each(doors)("%s flattens a refusal through lib/start-refusal", (path) => {
    const code = codeOnly(path);
    expect(code).toMatch(/import \{ refusalFrom \} from "[./]+lib\/start-refusal"/);
    expect(code).toContain("refusalFrom(");
    // The two shapes that forwarded the object: `error: data?.detail` and a raw detail cast.
    expect(code).not.toMatch(/error:\s*data\?\.detail/);
    expect(code).not.toMatch(/\{ detail\?: string \}\)\?\.detail/);
  });
});

describe("every screen that starts work", () => {
  const screens = ALL.filter((path) => path.endsWith(".tsx")
    && START_FROM_SCREEN.test(codeOnly(path)));

  it("is found — the enumeration is not empty", () => {
    expect(screens).toEqual(expect.arrayContaining([
      "app/ask-boss-hero.tsx", "app/delivery-workflows-ui.tsx", "app/run-report-ui.tsx",
      "app/workflows/new-project-composer.tsx",
    ]));
  });

  it.each(screens)("%s draws the checklist and offers Start anyway", (path) => {
    const code = codeOnly(path);
    // Either the shared refusal drawing, or the start form's own panel with its accept list.
    const drawsRefusal = code.includes("<StartRefusal") && code.includes("asReadiness(");
    const drawsForm = code.includes("<ReadinessPanel") && code.includes("start_anyway");
    expect(drawsRefusal || drawsForm).toBe(true);
    // And the retry sends the ticked keys back — not a second identical start.
    expect(code).toMatch(/retry: \(keys\) =>|start_anyway: startAnyway/);
  });
});

describe("the Build route", () => {
  it("forwards a readiness refusal as a string with the checklist, and sends Build anyway", async () => {
    const sent: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      sent.push(JSON.parse(String(init.body)));
      return sent.length === 1
        ? Response.json({ detail: { message: "This workflow cannot run here yet.",
                                    readiness: READINESS } }, { status: 409 })
        : Response.json({ run: { id: "wfr_1" } });
    }));
    const { POST } = await import("../app/api/builds/route");
    const post = (body: Record<string, unknown>) => POST(new Request("http://console/api/builds", {
      method: "POST", body: JSON.stringify(body) }));

    const refused = await post({ project_id: "p", directive: "Shift swap board" });
    expect(refused.status).toBe(409);
    const body = await refused.json();
    expect(body.error).toBe("This workflow cannot run here yet.");
    expect(body.readiness.missing[0].key).toBe("deployment_target");

    const started = await post({ project_id: "p", directive: "Shift swap board",
                                 start_anyway: ["deployment_target"] });
    expect(started.status).toBe(200);
    expect((sent[1] as { start_anyway?: string[] }).start_anyway).toEqual(["deployment_target"]);
    expect((sent[0] as { start_anyway?: string[] }).start_anyway).toBeUndefined();
  });
});

describe("re-running a run", () => {
  const UI = codeOnly("app/delivery-workflows-ui.tsx");

  it("names the run it repeats, so what was accepted for it carries over", () => {
    expect(UI).toContain("rerun_of: run.id");
    expect(UI).toContain("replaces_run: run.id");
  });

  it("every refused start on the runs panel draws the checklist, whichever button made it", () => {
    const act = UI.slice(UI.indexOf("const act = useCallback("),
                         UI.indexOf("actRef.current = act;"));
    expect(act).toContain("asReadiness(result.readiness)");
    expect(act).toContain("start_anyway: keys");
  });
});

describe("schedules", () => {
  // Set from a Library card's Schedule… popover (app/workflows/library.tsx); saved by the area.
  const LIBRARY = codeOnly("app/workflows/library.tsx");
  const UI = codeOnly("app/delivery-workflows-ui.tsx");

  it("show what their firing will refuse while being saved, and store Start anyway", () => {
    const view = LIBRARY.slice(LIBRARY.indexOf("function SchedulePopover("));
    expect(view).toContain("useWorkflowReadiness(projectId, draft.workflowKey, draft.values)");
    expect(view).toContain("<ReadinessPanel readiness={readiness}");
    expect(UI).toContain("start_anyway: draft.startAnyway");
  });

  it("say what was missing when a firing did not start, each with its fix", () => {
    expect(LIBRARY).toContain("s.last_missing.map((m) =>");
    expect(LIBRARY).toContain("${m.fix ? ` (${m.fix})` : \"\"}");
    expect(readFileSync("app/workflows/library.tsx", "utf8"))
      .toContain('data-testid={`wf-sched-refused-${s.id}`}');
  });
});
