/**
 * Every old Infra, System Configuration and Observability URL lands on its new tab
 * (infra-observability spec §4.1). The rail's "Infra" was area `delivery`, its "Observability"
 * was `monitoring`, `?area=infra` was System Configuration, and `?area=observability` opened a
 * blank Command Center. One resolver (app/legacy-areas.ts) answers for the server render, the
 * shell's link handler and the v2 routes, so this table is the whole contract.
 */
import { describe, expect, it } from "vitest";

import { resolveLegacyArea, resolveLegacyRoute } from "../app/legacy-areas";
import { isAreaId } from "../lib/area-ids";
import { readInfraUrl, logsHref } from "../lib/infra-view";
import { readObsUrl } from "../lib/obs-view";

const land = (qs: string): Record<string, string> | null => {
  const next = resolveLegacyArea(new URLSearchParams(qs));
  return next ? Object.fromEntries(next.entries()) : null;
};

describe("legacy Infra / System Configuration / Observability links", () => {
  it.each([
    ["area=delivery", { area: "infra", view: "fleet" }],
    ["area=delivery&project_id=p1&org=o1", { area: "infra", view: "fleet", project_id: "p1", org: "o1" }],
    ["area=monitoring", { area: "observability", view: "apps" }],
    ["area=monitoring&watch=invoice%7Cprod", { area: "observability", view: "apps", watch: "invoice|prod" }],
    ["area=infra&infra_view=connections", { area: "infra", view: "cloud", cloud: "accounts" }],
    ["area=infra&infra_view=resources", { area: "infra", view: "cloud", cloud: "resources" }],
    ["area=infra&infra_view=scaling", { area: "infra", view: "cloud", cloud: "capacity" }],
    ["area=infra&infra_view=finops", { area: "infra", view: "cloud", cloud: "spend" }],
    ["area=infra&infra_view=mcp", { area: "skills", tab: "library", lib: "mcp" }],
    ["area=infra&provider_account_id=acct_1", { area: "infra", view: "cloud", cloud: "accounts", account: "acct_1" }],
    ["area=observability&tab=models", { area: "observability", view: "models", win: "30d" }],
    ["area=observability&tab=models&win=7d", { area: "observability", view: "models", win: "7d" }],
  ])("%s", (qs, want) => {
    expect(land(qs)).toEqual(want);
  });

  it("leaves the new URLs alone", () => {
    for (const qs of ["area=infra", "area=infra&view=deploys&deploy=stack%3Asd_1", "area=observability",
                      "area=observability&view=models&win=30d"]) {
      expect(land(qs), qs).toBeNull();
    }
  });

  it("drops the parameters only the old screens read", () => {
    const next = land("area=infra&infra_view=resources&provider_account_id=a&actor=ceo")!;
    expect(next.infra_view).toBeUndefined();
    expect(next.provider_account_id).toBeUndefined();
    expect(next.actor).toBe("ceo");
  });

  it("sends /v2/infra to Infra, carrying only who you are", () => {
    expect(resolveLegacyRoute("/v2/infra", new URLSearchParams("org=o1&project_id=p1&tab=hosts")))
      .toBe("/?org=o1&project_id=p1&area=infra");
  });

  it("knows both areas, and the redirect targets read back as the right tab", () => {
    expect(isAreaId("infra")).toBe(true);
    expect(isAreaId("observability")).toBe(true);
    expect(readInfraUrl("?area=infra&view=cloud&cloud=spend").view).toBe("cloud");
    expect(readInfraUrl("?area=infra&view=cloud&cloud=spend").cloud).toBe("spend");
    expect(readInfraUrl("?area=infra").view).toBe("fleet");
    // Govern's link (activity-govern spec) reads as the Models tab even before the rewrite.
    expect(readObsUrl("?area=observability&tab=models").view).toBe("models");
    expect(readObsUrl("?area=observability&tab=models").win).toBe("30d");
    expect(readObsUrl("?area=observability&view=models").win).toBe("24h");
    expect(readObsUrl("?area=observability").view).toBe("apps");
  });

  it("links into the Activity log with the filter shape the Activity spec honours", () => {
    // §6.7: the Activity log accepts `source=` and `subject=` (it rewrites source → src).
    expect(logsHref("deployment", "t_live")).toBe("?area=activity&source=deployment&subject=t_live");
    expect(logsHref("llm_call", "claude-sonnet", "2026-10-04T00:00:00Z"))
      .toBe("?area=activity&source=llm_call&subject=claude-sonnet&since=2026-10-04T00%3A00%3A00Z");
  });
});
