import { describe, expect, it } from "vitest";

import { resolveLegacyArea, resolveLegacyRoute, searchParamsFrom } from "../app/legacy-areas";

/**
 * Every retired surface still opens what it meant (Workflow Studio spec 2.9).
 *
 * Agent Studio and Team structure left the nav, the Workflow area's five tabs became two, and
 * three v2 screens became console areas. A link to any of them sits in bookmarks, chat answers
 * and notifications; each row below is one such link and where it must land.
 */
const resolve = (query: string) => {
  const out = resolveLegacyArea(new URLSearchParams(query));
  return out ? Object.fromEntries(out.entries()) : null;
};

describe("resolveLegacyArea", () => {
  it("sends a Studio run link to the run, in Workflows", () => {
    expect(resolve("org=o1&area=studio&wfr=wfr_9")).toEqual(
      { org: "o1", area: "delivery_workflows", wfr: "wfr_9" });
  });

  it("sends Studio's flows to the Library, and a named one to its editor", () => {
    expect(resolve("area=studio&studio_mode=workflows")).toEqual(
      { area: "delivery_workflows", tab: "library" });
    expect(resolve("area=studio&mode=workflows")).toEqual(
      { area: "delivery_workflows", tab: "library" });
    expect(resolve("area=studio&studio_workflow=video_short")).toEqual(
      { area: "delivery_workflows", wf: "video_short" });
    expect(resolve("area=studio&workflow=video_short&mode=workflows")).toEqual(
      { area: "delivery_workflows", wf: "video_short" });
  });

  it("sends a Studio agent link to that agent's page", () => {
    expect(resolve("org=o1&area=studio&studio_role=qa_lead")).toEqual(
      { org: "o1", area: "workforce", role: "qa_lead" });
  });

  it("sends anything else in Studio to Agents", () => {
    expect(resolve("area=studio")).toEqual({ area: "workforce" });
    expect(resolve("area=studio&studio_mode=panels&project_id=p")).toEqual(
      { area: "workforce", project_id: "p" });
  });

  it("sends Team structure to the Agents team view", () => {
    expect(resolve("org=o1&area=org_chart")).toEqual(
      { org: "o1", area: "workforce", view: "team" });
  });

  it("maps every old Workflow tab", () => {
    expect(resolve("area=delivery_workflows&tab=running")).toEqual(
      { area: "delivery_workflows", tab: "runs" });
    expect(resolve("area=delivery_workflows&tab=yours")).toEqual(
      { area: "delivery_workflows", tab: "library" });
    expect(resolve("area=delivery_workflows&tab=catalogue")).toEqual(
      { area: "delivery_workflows", tab: "library" });
    expect(resolve("area=delivery_workflows&tab=design")).toEqual(
      { area: "delivery_workflows", tab: "library", wf: "new" });
    expect(resolve("area=delivery_workflows&tab=steps")).toEqual(
      { area: "delivery_workflows", tab: "library", lib: "steps" });
  });

  it("keeps a workflow an old design link already named", () => {
    expect(resolve("area=delivery_workflows&tab=design&wf=mine")).toEqual(
      { area: "delivery_workflows", tab: "library", wf: "mine" });
  });

  it("leaves current links alone", () => {
    expect(resolve("area=delivery_workflows&tab=runs")).toBeNull();
    expect(resolve("area=delivery_workflows&tab=library")).toBeNull();
    expect(resolve("area=delivery_workflows&wf=new")).toBeNull();
    expect(resolve("area=workforce&view=team")).toBeNull();
    expect(resolve("area=overview")).toBeNull();
    expect(resolve("")).toBeNull();
  });

  it("is idempotent: a resolved link resolves to nothing further", () => {
    for (const q of ["area=studio&wfr=x", "area=studio&studio_mode=workflows",
                     "area=studio&studio_role=r", "area=studio", "area=org_chart",
                     "area=delivery_workflows&tab=design", "area=delivery_workflows&tab=steps"]) {
      const once = resolveLegacyArea(new URLSearchParams(q));
      expect(once).not.toBeNull();
      expect(resolveLegacyArea(once!)).toBeNull();
    }
  });
});

describe("resolveLegacyRoute", () => {
  const route = (path: string, query = "") =>
    resolveLegacyRoute(path, new URLSearchParams(query));

  it("sends the v2 workflow screen to the Library", () => {
    expect(route("/v2/boss")).toBe("/?area=delivery_workflows&tab=library");
  });

  it("sends v2 Agent work and Team structure to Agents", () => {
    expect(route("/v2/factory")).toBe("/?area=workforce");
    expect(route("/v2/org/")).toBe("/?area=workforce&view=team");
  });

  it("carries the org across, and nothing v2-specific", () => {
    expect(route("/v2/boss", "org=o1&view=x")).toBe(
      "/?org=o1&area=delivery_workflows&tab=library");
  });

  it("leaves other v2 routes alone", () => {
    expect(route("/v2/hive")).toBeNull();
    expect(route("/v2")).toBeNull();
  });

  it("sends the missions index to the project list, keeping the org", () => {
    // /v2 is the list; /v2/missions/[id] is one build. The bare index must not 404.
    expect(route("/v2/missions", "org=org_boss")).toBe("/v2?org=org_boss");
    expect(route("/v2/missions/", "org=org_boss&project_id=p1&view=x")).toBe("/v2?org=org_boss&project_id=p1");
    expect(route("/v2/missions")).toBe("/v2");
    expect(route("/v2/missions/build_9", "org=org_boss")).toBeNull();
  });
});

describe("searchParamsFrom", () => {
  it("flattens a Next search-param record to first values", () => {
    expect(searchParamsFrom({ area: "studio", x: ["a", "b"], y: undefined }).toString())
      .toBe("area=studio&x=a");
  });
});
