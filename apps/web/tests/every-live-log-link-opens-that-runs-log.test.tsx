// @vitest-environment happy-dom
/**
 * EVERY "LIVE LOG" LINK OPENS THAT RUN'S LOG. The operator, 2026-11-07: "Clicking on Livelogs does
 * not take to right logs place". Work rows and the drawer linked `<build link>&view=logs`; the build
 * page never read `view=logs` (it fell back to the Floor view) and its Logs drawer started closed,
 * so the link opened a page with no log on it.
 *
 * Now the engine gives each row `links.log` (`…&build=<run>&logs=build`), the build page reads
 * `logs=` and opens its Logs drawer on that tab, and that tab reads THIS run's activity only. Pinned:
 *   - the URL is read (and the old `view=logs` still means the same);
 *   - a row's and the drawer's Live log link is the run's log link;
 *   - following it mounts the build with its log open, fetched for that run, with content;
 *   - no link in the console builds `view=logs` again.
 */
import React from "react";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, mount, settle, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span /> }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => <span /> }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const LOG = "?project_id=p1&area=code_tasks&build=crun_inv2&logs=build";

describe("the build page reads where the log link points", () => {
  it("logs=build opens that build's log; the old view=logs means the same", async () => {
    const { readFactoryUrl } = await import("../app/code-tasks-ui");
    expect(readFactoryUrl(LOG)).toMatchObject({ build: "crun_inv2", logs: "build" });
    expect(readFactoryUrl("?area=code_tasks&view=logs&build=crun_inv2")).toMatchObject({ build: "crun_inv2", logs: "build" });
    expect(readFactoryUrl("?area=code_tasks&build=crun_inv2").logs).toBeNull();
    expect(readFactoryUrl("?area=code_tasks&build=crun_inv2&logs=nonsense").logs).toBeNull();
  });

  it("a build link becomes its log link, never view=logs", async () => {
    const { logOfBuild } = await import("../lib/project-experience");
    const out = logOfBuild("?project_id=p1&area=code_tasks&view=apps&build=crun_inv2");
    const q = new URLSearchParams(out.split("?")[1]);
    expect(q.get("build")).toBe("crun_inv2");
    expect(q.get("logs")).toBe("build");
    expect(q.get("view")).toBeNull();
  });
});

describe("following the link lands on the log, with content", () => {
  it("the cockpit opened with initialLogs shows the Logs drawer on the build tab, read for that run", async () => {
    const { Cockpit } = await import("../app/factory/cockpit");
    const urls: string[] = [];
    stubFetch(cockpit(), (url) => { urls.push(url); return undefined; });
    await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" initialLogs="build" onClose={() => {}} onOpenBuild={() => {}} />);
    await settle();
    expect($('[data-testid="fx-logs"]')).not.toBeNull();
    expect($('[data-testid="fx-logs-tab-build"]')?.getAttribute("aria-selected")).toBe("true");
    const activity = urls.find((u) => u.includes("/api/activity"));
    expect(activity).toBeTruthy();
    expect(new URLSearchParams(activity!.split("?")[1]).get("run_id")).toBe("crun_inv2");
    expect($('[data-testid="fx-log-build"]')?.textContent).toContain("Cloned the repo");
  });

  it("without the link the drawer starts closed (nothing else changed)", async () => {
    const { Cockpit } = await import("../app/factory/cockpit");
    stubFetch(cockpit());
    await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
    expect($('[data-testid="fx-logs"]')).toBeNull();
  });
});

describe("the whole page: the link a row carries, opened in the Factory", () => {
  it("lands on that build with its log open (the URL state keeps logs=; it once dropped it)", async () => {
    const { CodeTasksHome } = await import("../app/code-tasks-ui");
    const urls: string[] = [];
    stubFetch(cockpit(), (url) => {
      urls.push(url);
      return url.startsWith("/v1/factory/floor") ? { builds: [], counts: {} } : undefined;
    });
    window.history.replaceState(null, "", `/${LOG}`);
    await mount(<CodeTasksHome projectId="p1" codeTasks={[]} apiReachable actor="ceo" />);
    await settle();
    expect($('[data-testid="fx-cockpit"]')?.getAttribute("data-run")).toBe("crun_inv2");
    expect($('[data-testid="fx-logs"]')).not.toBeNull();
    expect($('[data-testid="fx-log-build"]')?.textContent).toContain("Cloned the repo");
    expect(urls.some((u) => u.includes("/api/activity") && u.includes("run_id=crun_inv2"))).toBe(true);
  });
});

describe("rows and the drawer link the run's log", () => {
  const item = async (links: Record<string, string>, state = "running") => {
    const { parseWorkItem } = await import("../lib/project-experience");
    return parseWorkItem({ id: "wfr_1", label: "Bump esbuild", kind: "fix", state, age: "2h", stages: [], agents: [], links, attempts: [] });
  };

  it("a running row's Live log is links.log", async () => {
    const { WorkRow } = await import("../app/projects/tab-work");
    const w = await item({ build: "?project_id=p1&area=code_tasks&view=apps&build=crun_inv2", log: LOG });
    const ctx = { projectId: "p1", refreshWork: () => {}, open: () => {} } as unknown as Parameters<typeof WorkRow>[0]["ctx"];
    await mount(<WorkRow w={w} ctx={ctx} />);
    const q = new URLSearchParams(($('[data-testid="px-work-log-wfr_1"]') as HTMLAnchorElement).getAttribute("href")!.split("?")[1]);
    expect(q.get("build")).toBe("crun_inv2");
    expect(q.get("logs")).toBe("build");
    expect(q.get("view")).toBeNull();
  });

  it("an older engine's row (no links.log) still gets a working log link from its build", async () => {
    const w = await item({ build: "?project_id=p1&area=code_tasks&view=apps&build=crun_inv2" });
    expect(new URLSearchParams(w.links.log!.split("?")[1]).get("logs")).toBe("build");
  });

  it("no console source builds `view=logs` any more", () => {
    const hits: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        if (statSync(p).isDirectory()) { if (name !== "node_modules") walk(p); continue; }
        if (!/\.(tsx?|ts)$/.test(name)) continue;
        if (/view=logs/.test(readFileSync(p, "utf8")) && !p.endsWith("code-tasks-ui.tsx")) hits.push(p);
      }
    };
    walk(join(__dirname, "..", "app"));
    walk(join(__dirname, "..", "lib"));
    expect(hits).toEqual([]);
  });
});
