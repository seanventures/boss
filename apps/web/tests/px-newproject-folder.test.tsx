// @vitest-environment happy-dom
/**
 * Add a project › Folder on host (wireframe 03) and Infra › server (wireframe 14), on the API's
 * shapes (engine project_experience/host_folder.py, host_overview.py):
 *   * Folder on host lists the verified servers, reads one folder (read-only), shows what Helm read
 *     (Access, Stack, Build & test, Map code) and Start onboarding records the folder — plus its
 *     GitHub repository when the registry reaches it — then starts onboarding;
 *   * the server drawer's top: Reachable, CPU / Memory / Disk rings, ports, the projects on it with
 *     Project › Deployments, host plugins and recent changes; Read now asks the server again.
 *   * the one Add-a-project sheet opens from ?new_project= on the Projects page (PX-20) and is
 *     drawn once even when two hosts are mounted.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StartProjectSheet } from "../app/start-project/start-sheet";
import { StartProjectHost } from "../app/start-project/start-host";
import { ServerOverview } from "../app/infra/server-overview";
import { $$, click, mountAs, registryFixture, stubFetch, tid, type, unmount } from "./_helpers/multirepo-fixtures";

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

const FOLDER = { path: "/srv/apps/ledger-api", exists: true, git: true, remote: "https://github.com/acme/api.git", github: "acme/api",
                 branch: "main", files: 214, stack: ["Node", "Docker"], build: "npm install · npm test", suggested_name: "Ledger api",
                 host_id: "dhost_1", host: "demo-host" };

describe("Folder on host", () => {
  function api() {
    return stubFetch((c) => {
      if (c.path === "/v1/github/repos") return { body: registryFixture() };
      if (c.path === "/v1/projects/p1/captain") return { body: { role_key: "engineering_captain", name: "Helm", candidates: [], squad: [] } };
      if (c.path === "/v1/projects/p1/host-folders/hosts") return { body: { hosts: [{ id: "dhost_1", name: "demo-host", address: "203.0.113.10", shared: false }] } };
      if (c.path === "/v1/projects/p1/host-folders/probe") {
        return { body: (c.body as { path: string }).path === "/srv/none" ? { path: "/srv/none", exists: false, host: "demo-host" } : FOLDER };
      }
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "ledger", name: "Ledger api" } };
      if (c.method === "POST" && c.path === "/v1/projects/ledger/host-folders") return { body: { id: 1, path: FOLDER.path } };
      if (c.method === "PUT" && c.path === "/v1/projects/ledger/repos/ghr_api") return { body: { project_id: "ledger", items: [], addable: [] } };
      if (c.method === "POST" && c.path === "/v1/projects/ledger/onboarding/start") return { body: { state: "running" } };
      return undefined;
    });
  }

  it("reads a folder on a verified server and Start onboarding records it as the project's source", async () => {
    const calls = api();
    const go = vi.fn();
    await mountAs(<StartProjectSheet step="import" onStep={() => {}} onClose={() => {}} projectId="p1" homeId="p1" go={go} />);
    await click(tid("import-src-folder"));
    expect(tid("import-src-folder")!.getAttribute("aria-selected")).toBe("true");
    expect($$('[data-testid="import-folder-host"] option').map((o) => o.textContent)).toEqual(["demo-host · 203.0.113.10"]);
    await type(tid("import-folder-path"), "/srv/none");
    await click(tid("import-folder-read"));
    expect(tid("import-folder-error")!.textContent).toBe("There is no folder /srv/none on demo-host.");
    expect((tid("import-create") as HTMLButtonElement).disabled).toBe(true);

    await type(tid("import-folder-path"), "/srv/apps/ledger-api");
    await click(tid("import-folder-read"));
    expect(calls.filter((c) => c.path.endsWith("/probe")).pop()!.body).toEqual({ host_id: "dhost_1", path: "/srv/apps/ledger-api" });
    expect(tid("import-live")!.textContent).toBe("live");
    expect(tid("import-check-access")!.textContent).toBe("Accessread · git main");
    expect(tid("import-check-stack")!.textContent).toBe("StackNode · Docker");
    expect(tid("import-check-build")!.textContent).toBe("Build & testnpm install · npm test");
    expect(tid("import-mapping")!.textContent).toBe("Map code0 / 214 files · starts on Start");
    expect(tid("import-folder-remote")!.textContent).toContain("acme/api · joins the project too");
    expect((tid("import-name") as HTMLInputElement).value).toBe("Ledger api");

    await click(tid("import-create"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/projects/ledger/host-folders")!.body)
      .toEqual({ host_id: "dhost_1", path: "/srv/apps/ledger-api" });
    expect(calls.some((c) => c.method === "PUT" && c.path === "/v1/projects/ledger/repos/ghr_api")).toBe(true);
    expect(calls.some((c) => c.path === "/v1/projects/ledger/onboarding/start")).toBe(true);
    expect(go).toHaveBeenCalled();
  });
});

describe("the one Add-a-project sheet (PX-20)", () => {
  it("opens from ?new_project=import with no project menu on the page, and is drawn once by two hosts", async () => {
    stubFetch((c) => (c.path === "/v1/github/repos" ? { body: { items: [] } } : undefined));
    window.history.replaceState(null, "", "/?area=projects&new_project=import");
    await mountAs(<><StartProjectHost projectId="p1" homeId="p1" /><StartProjectHost projectId="p1" homeId="p1" /></>);
    // Add a project v2: the one screen, opened reading a repo.
    expect($$('[data-testid="add-project"]').length).toBe(1);
    expect(tid("add-project")!.getAttribute("data-kind")).toBe("repo");
    // No GitHub account yet: it still opens and says where to allow one (contract §15).
    expect(tid("ap-results")!.textContent).toContain("Allow more in Repo and models ›");
    await click(tid("ap-close"));
    expect(tid("add-project")).toBeNull();
    expect(window.location.search).toBe("?area=projects");
  });
});

describe("Infra › server (wireframe 14)", () => {
  const OVERVIEW = {
    id: "dhost_1", name: "demo-host", address: "203.0.113.20", sub: "Docker host · Ubuntu 22.04.4 LTS · shared by 2 projects",
    reachable: { state: "reachable", reason: null, at: "2026-11-06T10:00:00Z" },
    capacity: { cpu: { pct: 34, label: "8 vCPU" }, memory: { pct: 61, label: "16 GB" }, disk: { pct: 72, label: "200 GB" },
                ports: { used: 7, range: "8001–8104" }, at: new Date(Date.now() - 3600_000).toISOString() },
    projects: [
      { id: "helpdesk-ai-demo", name: "HelpDesk AI Demo", initials: "HD", target_id: "t1", app: "helpdesk", env: "staging", port: 8004,
        health: { word: "Healthy", tone: "green", status: "up" }, agent: { name: "Beacon", role_key: "sre", live: false }, href: "" },
      { id: "northpeak", name: "NorthPeak Retail", initials: "NP", target_id: "t2", app: "np", env: "preview", port: 8104,
        health: { word: "Degraded", tone: "amber", status: "degraded" }, agent: { name: "Beacon", role_key: "sre", live: true }, href: "" },
    ],
    shared_by: 2,
    plugins: [{ key: "docker", label: "Docker 27", icon: "cube" }, { key: "node-exporter", label: "node-exporter", icon: "chart" },
              { key: "caddy", label: "Caddy TLS", icon: "lock" }],
    changes: [{ id: "s1", label: "HelpDesk staging deploy", state: "done", at: new Date(Date.now() - 2 * 3600_000).toISOString(), by: "Beacon",
                href: "?project_id=helpdesk-ai-demo&area=project&tab=deployments" }],
  };

  it("draws the server's header, capacity rings, projects, plugins and changes — and Read now asks again", async () => {
    let reads = 0;
    const calls = stubFetch((c) => {
      if (c.path === "/v1/infra/hosts/dhost_1/overview") { reads += 1; return { body: OVERVIEW }; }
      if (c.method === "POST" && c.path === "/v1/infra/hosts/dhost_1/capacity") return { body: { cpus: 8 } };
      return undefined;
    });
    await mountAs(<ServerOverview projectId="p1" hostId="dhost_1" canExec />);
    expect(tid("inf-server-overview")!.querySelector("h3")!.textContent).toBe("203.0.113.20");
    expect(tid("inf-server-reach")!.textContent).toBe("Reachable");
    expect(tid("inf-server-sub")!.textContent).toBe("Docker host · Ubuntu 22.04.4 LTS · shared by 2 projects");
    expect(tid("inf-server-cpu")!.textContent).toContain("34%");
    expect(tid("inf-server-cpu")!.textContent).toContain("8 vCPU");
    expect(tid("inf-server-disk")!.querySelector("circle:nth-of-type(2)")!.getAttribute("stroke")).toBe("var(--amber)");
    // 8001–8104 holds 104 ports; 7 used leaves 97 free. That free count is the tile's headline.
    expect(tid("inf-server-ports-free")!.textContent).toBe("97");
    expect(tid("inf-server-ports")!.textContent).toContain("free · 7 used");
    expect(tid("inf-server-ports")!.textContent).toContain("8001–8104");
    expect($$('[data-testid^="inf-host-project-"]:not([data-testid*="link"])').map((r) => r.querySelector("b")!.textContent))
      .toEqual(["HelpDesk AI Demo", "NorthPeak Retail"]);
    const link = new URLSearchParams(tid("inf-host-project-link-helpdesk-ai-demo")!.getAttribute("href")!.split("?")[1]);
    expect([link.get("project_id"), link.get("area"), link.get("tab")]).toEqual(["helpdesk-ai-demo", "project", "deployments"]);
    expect(tid("inf-host-project-northpeak")!.textContent).toContain("Degraded");
    expect($$('[data-testid="inf-server-plugins"] .inf-so-pchip').map((p) => p.textContent!.trim())).toEqual(["Docker 27", "node-exporter", "Caddy TLS"]);
    expect(tid("inf-server-changes")!.textContent).toContain("HelpDesk staging deploy");
    expect(tid("inf-server-changes")!.textContent).toContain("Beacon");
    await click(tid("inf-server-read"));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/infra/hosts/dhost_1/capacity")).toBe(true);
    expect(reads).toBe(2);
  });
});
