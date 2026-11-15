// @vitest-environment happy-dom
/**
 * After repositories are allowed (BOSSAPP-1018): one primary per repo to import it as a project
 * or add it to the current project, and a line naming where it lives afterwards.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RepoHandoff } from "../app/setup/repo-handoff";
import { click, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

const REPOS = [{ full_name: "kaappu/helpdesk", id: "ghr_help" }, { full_name: "kaappu/website", id: "ghr_web" }];

describe("the handoff after repositories are allowed", () => {
  it("names where a repo lives afterwards and offers one primary action per repo", async () => {
    window.history.replaceState(null, "", "/?area=setup&org=org_1");
    stubFetch(() => undefined);
    await mountAs(<RepoHandoff projectId="p" repos={REPOS} />);
    const where = tid("acct-handoff-where")!;
    expect(where.textContent).toContain("Start a project");
    expect(where.textContent).toContain("Add from GitHub");
    expect(where.textContent).toContain("the project's Repositories");
    const start = where.querySelector("a")!;
    const links = [...where.querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links.map((l) => l[0])).toEqual(["Start a project", "Add from GitHub", "the project's Repositories"]);
    expect(String(links[0][1])).toContain("new_project=1");
    expect(String(links[1][1])).toContain("new_project=import");
    expect(String(links[2][1])).toContain("section=repos");
    expect(String(links[2][1])).toContain("project_id=p");
    expect(start).not.toBeNull();
    expect(tid("acct-import-kaappu/helpdesk")!.textContent).toBe("Import as a project");
    expect(tid("acct-add-kaappu/helpdesk")!.textContent).toBe("Add to this project");
    expect(tid("acct-import-kaappu/website")!.textContent).toBe("Import as a project");
    expect(tid("acct-import-kaappu/helpdesk")!.className).toContain("primary");
  });

  it("adds the repo to this project", async () => {
    const calls = stubFetch((c) => {
      if (c.method === "PUT" && c.path === "/v1/projects/p/repos/ghr_help") {
        return { body: { project_id: "p", items: [], addable: [] } };
      }
      return undefined;
    });
    await mountAs(<RepoHandoff projectId="p" repos={[{ full_name: "kaappu/helpdesk", id: "ghr_help" }]} />);
    await click(tid("acct-add-kaappu/helpdesk"));
    expect(calls.find((c) => c.method === "PUT")!.path).toBe("/v1/projects/p/repos/ghr_help");
    expect(tid("acct-handoff-note")!.textContent).toContain("project's Repositories");
  });

  it("imports the repo as its own project", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const calls = stubFetch((c) => {
      if (c.method === "POST" && c.path === "/v1/projects") return { status: 201, body: { id: "helpdesk", name: "Helpdesk" } };
      if (c.method === "PUT" && c.path === "/v1/projects/helpdesk/repos/ghr_help") {
        return { body: { project_id: "helpdesk", items: [], addable: [] } };
      }
      return undefined;
    });
    await mountAs(<RepoHandoff projectId="p" repos={[{ full_name: "kaappu/helpdesk", id: "ghr_help" }]} />);
    await click(tid("acct-import-kaappu/helpdesk"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/v1/projects")!.body).toEqual({ name: "Helpdesk" });
    expect(calls.some((c) => c.method === "PUT" && c.path === "/v1/projects/helpdesk/repos/ghr_help")).toBe(true);
    expect(assign).toHaveBeenCalled();
    expect(String(assign.mock.calls[0][0])).toContain("project_id=helpdesk");
    expect(String(assign.mock.calls[0][0])).toContain("section=repos");
  });

  it("looks the registry id up when the allow result did not carry one", async () => {
    const calls = stubFetch((c) => {
      if (c.method === "GET" && c.path === "/v1/github/repos") {
        return { body: { items: [{ id: "ghr_help", full_name: "kaappu/helpdesk", allowed: true }] } };
      }
      if (c.method === "PUT") return { body: { project_id: "p", items: [], addable: [] } };
      return undefined;
    });
    await mountAs(<RepoHandoff projectId="p" repos={[{ full_name: "kaappu/helpdesk" }]} />);
    await click(tid("acct-add-kaappu/helpdesk"));
    expect(calls.some((c) => c.method === "GET" && c.path === "/v1/github/repos")).toBe(true);
    expect(calls.find((c) => c.method === "PUT")!.path).toBe("/v1/projects/p/repos/ghr_help");
  });
});
