// @vitest-environment happy-dom
/**
 * NEW WORK BUILDS ONTO THE APP ITS DOOR NAMED, AND KEEPS A LONG BRIEF WHOLE.
 *
 * AR-20: "+ Add a feature" on build crun_9a9a535c (project northgate-gateway) linked to
 * `?org=org_boss&area=project&tab=work&app=crun_…` — no project, so New work opened in whatever
 * project the URL last held, on another repo; and New work ignored `app=`, so it said
 * "local_northgate-gateway/northgate-gateway · onto main". Now ONE builder (lib/compose-links
 * addFeaturePatch / newWorkSearch) makes every link into New work and always carries the
 * project, and the app when the door is an app's; New work reads `app`, shows "building on
 * <app> · change #N" and its branch, and Start sends `app` (the engine continues that lineage).
 *
 * AR-21: the box stopped at 2,000 characters and a 2,080-character brief lost its last item
 * without a word. The limit is the start door's (MAX_DIRECTIVE), a counter shows near it, and
 * Start is held with a message above it — the words are never cut.
 */
import React, { act } from "react";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { addFeaturePatch, newWorkSearch } from "../lib/compose-links";
import { addFeatureHref } from "../app/factory/apps-tab";
import { AppsShelf } from "../app/command-center/rails";
import { parseCcApps } from "../lib/command-center-apps";
import { MAX_BRIEF_CHARS, anchorWords, briefLength } from "../lib/new-work";
import { ProjectPage } from "../app/projects/project-page";
import {
  FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_WORK, FX_WORK_PLAN,
} from "../lib/project-experience-fixtures";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const WEB = join(__dirname, "..");
const P = "northgate-gateway";
const RUN = "crun_9a9a535cd72e486e9c17";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(el, value);
    (el as HTMLElement).dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
};
const params = () => new URLSearchParams(window.location.search);

beforeEach(() => { window.history.replaceState(null, "", "/"); try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

// ── every door into New work ────────────────────────────────────────────────────────────────
describe("every link into New work carries the project, and the app when it is an app's door", () => {
  it("the build page's + Add a feature names its project and its app, from a URL that has no project", () => {
    // The build page's own URL (the Factory) carries no project_id: the link must bring it.
    window.history.replaceState(null, "", `/?org=org_boss&area=code_tasks&build=${RUN}`);
    const q = new URLSearchParams(addFeatureHref(P, RUN).replace(/^\?/, ""));
    expect([q.get("org"), q.get("area"), q.get("project_id"), q.get("tab"), q.get("compose"), q.get("app")])
      .toEqual(["org_boss", "project", P, "work", "1", RUN]);
  });

  it("the one builder: a patch with project and app, and the same door as a plain link", () => {
    expect(addFeaturePatch({ projectId: P, app: "northgate" })).toMatchObject({ area: "project", project_id: P, tab: "work", compose: "1", app: "northgate" });
    // A door that is not an app's keeps the page's project (no project_id key to overwrite it).
    expect("project_id" in addFeaturePatch()).toBe(false);
    expect(newWorkSearch({ projectId: "ops" })).toBe("?area=project&project_id=ops&tab=work&compose=1");
    expect(newWorkSearch({ projectId: P, app: RUN })).toBe(`?area=project&project_id=${P}&tab=work&compose=1&app=${RUN}`);
  });

  it("the Command Center's app card links into its own project", async () => {
    const [app] = parseCcApps({ apps: [{ key: "northgate", app: "Northgate Gateway", run_id: RUN, builders: [], state: "pr_open",
      word: "PR open", pr_number: 4, pr_url: null, pr_state: "open", cost_usd: 0, included: true, builds: 1, run_ids: [RUN], at: null,
      attempts: [{ run_id: RUN, state: "pr_open" }] }] });
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root: Root = createRoot(host);
    await act(async () => { root.render(<AppsShelf apps={[app]} orgId="org_boss" projectId={P} />); });
    const href = host.querySelector("[data-testid=cc-app-add]")!.getAttribute("href")!;
    const q = new URLSearchParams(href.split("?")[1]);
    expect([q.get("project_id"), q.get("app"), q.get("compose")]).toEqual([P, "northgate", "1"]);
    act(() => { root.unmount(); });
    host.remove();
  });

  it("no door builds a New work link by hand: each goes through the builder with its project", () => {
    // Inside the project page a door is `ctx.tab("work", { compose: "1" })` / projectHref(pid, …):
    // the project is already the page's. Add a project's "add work there" builds on
    // projectPageHref(pid). Everything else calls the builder.
    const ALLOWED = new Set(["lib/compose-links.ts", "app/start-project/add-project.tsx"]);
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        if (name === "node_modules" || name.startsWith(".")) continue;
        if (statSync(p).isDirectory()) walk(p);
        else if (/\.tsx?$/.test(name)) files.push(p);
      }
    };
    walk(join(WEB, "app"));
    walk(join(WEB, "lib"));
    walk(join(WEB, "components"));
    const handMade: string[] = [];
    const noProject: string[] = [];
    for (const f of files) {
      const rel = relative(WEB, f);
      const code = readFileSync(f, "utf8").split("\n").filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join("\n");
      if (!ALLOWED.has(rel) && !rel.startsWith("app/projects/") && /compose=1|compose: ?"1"/.test(code)) handMade.push(rel);
      for (const m of code.matchAll(/addFeaturePatch\(([^)]*)\)/g)) {
        if (rel === "lib/compose-links.ts") continue;
        if (!/projectId/.test(m[1])) noProject.push(`${rel}: addFeaturePatch(${m[1]})`);
      }
      for (const m of code.matchAll(/addFeatureHref\(([^)]*)\)/g)) {
        if (/export function/.test(code.slice(Math.max(0, m.index! - 20), m.index!))) continue;
        if (m[1].split(",").length < 2) noProject.push(`${rel}: addFeatureHref(${m[1]})`);
      }
    }
    expect(handMade).toEqual([]);
    expect(noProject).toEqual([]);
  });
});

// ── New work reads `app` and builds onto it ─────────────────────────────────────────────────
const LOCAL_CHANGE = `http://boss-local/${P}/northgate-gateway/changes/4`;
const ANCHOR = { run_id: RUN, app: "Northgate Gateway", branch: "boss/code/crun_root1", pr_number: 4, pr_url: LOCAL_CHANGE };

function engine(plan: Record<string, unknown> = { anchor: ANCHOR }) {
  return stubFetch((c: Call) => {
    if (c.method === "POST" && c.path === `/v1/projects/${P}/work/understand`) return { body: { status: "no_model" } };
    if (c.method === "GET" && c.path === `/v1/projects/${P}/work/plan`) {
      const q = new URLSearchParams(c.search);
      return { body: { ...FX_WORK_PLAN, kind: q.get("kind"), ...(q.get("app") ? plan : {}) } };
    }
    if (c.method === "POST" && c.path === `/v1/projects/${P}/work`) return { body: { run_id: "wfr_new", work: null } };
    if (c.method !== "GET") return { body: { ok: true } };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview: FX_OVERVIEW, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments: FX_DEPLOYMENTS,
                                                onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    return undefined;
  });
}
async function open(q: string) {
  window.history.replaceState(null, "", `/?org=org_boss&project_id=${P}&area=project${q}`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_boss" />, "owner");
  await settle(12);
}
const box = () => document.querySelector<HTMLTextAreaElement>("[data-testid=px-compose-text]");
const startBtn = () => tid("px-compose-start") as HTMLButtonElement | null;
const workPosts = (calls: Call[]) => calls.filter((c) => c.method === "POST" && c.path === `/v1/projects/${P}/work`);

describe("New work opened from an app's door builds onto that app", () => {
  it("asks the engine for the app, says which app and change it builds on, and Start sends it", async () => {
    const calls = engine();
    await open(`&tab=work&compose=1&app=${RUN}`);
    expect(tid("px-composer")).not.toBeNull();
    // The app is read once and dropped from the URL, as compose is.
    expect(params().get("app")).toBeNull();
    const asked = calls.filter((c) => c.method === "GET" && c.path === `/v1/projects/${P}/work/plan`);
    expect(asked.some((c) => new URLSearchParams(c.search).get("app") === RUN)).toBe(true);
    expect(tid("px-nw-anchor")?.textContent).toContain("building on Northgate Gateway · change #4");

    await type(box(), "Add a /healthz route that reports the upstream pool.");
    await press(startBtn());
    const sent = workPosts(calls);
    expect(sent).toHaveLength(1);
    expect((sent[0].body as { app?: string }).app).toBe(RUN);
  });

  it("an app that is not this project's holds Start and says why — never a quiet build on main", async () => {
    const calls = engine({ anchor: null, anchor_error: "that app is not in this project" });
    await open(`&tab=work&compose=1&app=crun_elsewhere`);
    expect(tid("px-nw-anchor")?.textContent).toContain("not in this project");
    await type(box(), "Add a /healthz route that reports the upstream pool.");
    expect(startBtn()?.disabled).toBe(true);
    expect(tid("px-nw-held")?.textContent).toContain("that app is not in this project");
    await press(startBtn());
    expect(workPosts(calls)).toHaveLength(0);
  });

  it("names a GitHub app's change as its PR, and an app with no change yet by its branch", () => {
    expect(anchorWords({ runId: RUN, app: "Shop", branch: "boss/code/r", prNumber: 7, prUrl: "https://github.com/acme/shop/pull/7" }))
      .toBe("building on Shop · PR #7");
    expect(anchorWords({ runId: RUN, app: "Shop", branch: "boss/code/r", prNumber: null, prUrl: null }))
      .toBe("building on Shop · boss/code/r");
  });
});

// ── the brief's limit ───────────────────────────────────────────────────────────────────────
describe("a long brief is never cut", () => {
  it("the box's limit is the engine's MAX_DIRECTIVE, one number", () => {
    const py = readFileSync(join(WEB, "../../engine/boss_engine/services/project_experience/add_project.py"), "utf8");
    const m = py.match(/^MAX_DIRECTIVE\s*=\s*([\d_]+)/m);
    expect(m).not.toBeNull();
    expect(MAX_BRIEF_CHARS).toBe(Number(m![1].replace(/_/g, "")));
    // And POST …/work refuses past that same number (never truncates).
    const router = readFileSync(join(WEB, "../api/app/routers/project_experience.py"), "utf8");
    expect(router).toMatch(/text: str = Field\(\.\.\., max_length=_MAX_BRIEF/);
  });

  it("a 2,080-character brief goes to Start whole, its last item included", async () => {
    const calls = engine();
    await open("&tab=work&compose=1");
    const items = Array.from({ length: 40 }, (_, i) => `- item ${String(i + 1).padStart(2, "0")}: the gateway keeps config ${i}`);
    let words = items.join("\n");
    while (words.length < 2080) words += " more";
    words = `${words.slice(0, 2060)}\n- LAST ITEM kept`;
    expect(words.length).toBeGreaterThan(2000);
    expect(box()?.hasAttribute("maxlength")).toBe(false);
    await type(box(), words);
    expect(box()?.value).toBe(words);
    await press(startBtn());
    const sent = workPosts(calls);
    expect(sent).toHaveLength(1);
    expect((sent[0].body as { text: string }).text).toBe(words);
    expect((sent[0].body as { text: string }).text.endsWith("- LAST ITEM kept")).toBe(true);
  });

  it("shows a counter near the limit, and past it holds Start with a message above it", async () => {
    const calls = engine();
    await open("&tab=work&compose=1");
    await type(box(), "x".repeat(MAX_BRIEF_CHARS - 500));
    expect(tid("px-nw-count")?.textContent).toBe(`${(MAX_BRIEF_CHARS - 500).toLocaleString("en-US")} / ${MAX_BRIEF_CHARS.toLocaleString("en-US")}`);
    expect(tid("px-nw-held")).toBeNull();
    expect(startBtn()?.disabled).toBe(false);

    await type(box(), "x".repeat(MAX_BRIEF_CHARS + 100));
    expect(box()?.value.length).toBe(MAX_BRIEF_CHARS + 100);
    expect(tid("px-nw-count")?.getAttribute("data-over")).toBe("true");
    expect(tid("px-nw-held")?.textContent).toContain("100 characters over");
    expect(startBtn()?.disabled).toBe(true);
    await press(startBtn());
    expect(workPosts(calls)).toHaveLength(0);
  });

  it("says nothing about length well below the limit", () => {
    expect(briefLength("short").count).toBeNull();
    expect(briefLength("x".repeat(MAX_BRIEF_CHARS)).over).toBe(false);
    expect(briefLength("x".repeat(MAX_BRIEF_CHARS + 1)).over).toBe(true);
  });
});
