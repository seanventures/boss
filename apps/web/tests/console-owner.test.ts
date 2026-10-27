/**
 * Build L: resolve-before-fetch. Pins the pure core of app/console-owner.ts — which URL param
 * names which object kind, how a query string is rewritten with project_id, and the single
 * resolve call — per object type (build, meeting, workflow_run via wfr/run, decision, self_fix,
 * shepherd_escalation, agent_thread), without a browser or a live API.
 */
import { describe, expect, it } from "vitest";

import {
  DEEP_LINK_PARAMS,
  projectIdFromSearch,
  readDeepLinkTarget,
  resolveDeepLinkOwner,
  resolveOwnerPath,
  searchWithProject,
} from "../app/console-owner";

describe("readDeepLinkTarget names the object a URL opens, per object type", () => {
  for (const { param, type } of DEEP_LINK_PARAMS) {
    it(`?${param}=xyz -> ${type}`, () => {
      expect(readDeepLinkTarget(`?${param}=xyz`)).toEqual({ param, type, id: "xyz" });
    });
  }

  it("no deep-link param names no object", () => {
    expect(readDeepLinkTarget("?area=code_tasks")).toBeNull();
  });

  it("a blank id names no object", () => {
    expect(readDeepLinkTarget("?build=")).toBeNull();
  });

  it("build takes precedence over a stale run= on the same URL", () => {
    expect(readDeepLinkTarget("?run=wfr_1&build=crun_1")).toEqual({
      param: "build", type: "build", id: "crun_1",
    });
  });
});

describe("searchWithProject / projectIdFromSearch", () => {
  it("stamps project_id and keeps the rest of the query string", () => {
    const next = searchWithProject("?area=code_tasks&build=crun_1", "proj_a");
    expect(next).toContain("project_id=proj_a");
    expect(next).toContain("build=crun_1");
    expect(projectIdFromSearch(next)).toBe("proj_a");
  });

  it("projectIdFromSearch answers null when absent", () => {
    expect(projectIdFromSearch("?area=code_tasks")).toBeNull();
  });
});

describe("resolveOwnerPath", () => {
  it("names the target's type and id, and the caller's current project", () => {
    const path = resolveOwnerPath({ param: "meeting", type: "meeting", id: "mtg_1" }, "proj_a");
    expect(path).toBe("/v1/console/resolve-owner?type=meeting&id=mtg_1&project_id=proj_a");
  });

  it("omits project_id when the caller has none yet", () => {
    const path = resolveOwnerPath({ param: "decision", type: "decision", id: "dec_1" }, null);
    expect(path).toBe("/v1/console/resolve-owner?type=decision&id=dec_1");
  });
});

describe("resolveDeepLinkOwner, per object type", () => {
  const okFetch = (projectId: string, canView = true) =>
    async () => ({ ok: true, status: 200, json: async () => ({ project_id: projectId, can_view: canView }) });

  for (const { param, type } of DEEP_LINK_PARAMS) {
    it(`${type}: switches to the object's own project`, async () => {
      const result = await resolveDeepLinkOwner({
        search: `?${param}=id_1`,
        projectId: "proj_selected",
        fetchImpl: okFetch("proj_owner"),
      });
      expect(result).toEqual({
        target: { param, type, id: "id_1" },
        projectId: "proj_owner",
        switched: true,
        canView: true,
      });
    });
  }

  it("no target: keeps the current project without resolving", async () => {
    const result = await resolveDeepLinkOwner({
      search: "?area=code_tasks",
      projectId: "proj_selected",
      fetchImpl: async () => { throw new Error("must not be called"); },
    });
    expect(result).toEqual({ target: null, projectId: "proj_selected", switched: false, canView: false });
  });

  it("already on the owner's project: resolved but not switched", async () => {
    const result = await resolveDeepLinkOwner({
      search: "?build=crun_1",
      projectId: "proj_owner",
      fetchImpl: okFetch("proj_owner"),
    });
    expect(result.switched).toBe(false);
    expect(result.canView).toBe(true);
  });

  it("denied (not-found or access-denied, indistinguishably): fails open, keeps the starting project", async () => {
    const result = await resolveDeepLinkOwner({
      search: "?build=crun_1",
      projectId: "proj_selected",
      fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ can_view: false }) }),
    });
    expect(result).toEqual({
      target: { param: "build", type: "build", id: "crun_1" },
      projectId: "proj_selected",
      switched: false,
      canView: false,
    });
  });

  it("a non-ok response fails open", async () => {
    const result = await resolveDeepLinkOwner({
      search: "?build=crun_1",
      projectId: "proj_selected",
      fetchImpl: async () => ({ ok: false, status: 500, json: async () => ({}) }),
    });
    expect(result.projectId).toBe("proj_selected");
    expect(result.switched).toBe(false);
  });

  it("a thrown fetch failure fails open, never blocking the console", async () => {
    const result = await resolveDeepLinkOwner({
      search: "?meeting=mtg_1",
      projectId: "proj_selected",
      fetchImpl: async () => { throw new Error("network down"); },
    });
    expect(result).toEqual({
      target: { param: "meeting", type: "meeting", id: "mtg_1" },
      projectId: "proj_selected",
      switched: false,
      canView: false,
    });
  });

  it("base is prefixed onto the resolver path (server-side caller)", async () => {
    const seen: string[] = [];
    await resolveDeepLinkOwner({
      search: "?build=crun_1",
      projectId: null,
      base: "https://api.internal",
      fetchImpl: async (path) => {
        seen.push(path);
        return { ok: true, status: 200, json: async () => ({ project_id: "proj_owner", can_view: true }) };
      },
    });
    expect(seen[0]?.startsWith("https://api.internal/v1/console/resolve-owner")).toBe(true);
  });
});
