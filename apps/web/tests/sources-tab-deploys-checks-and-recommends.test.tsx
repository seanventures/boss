// @vitest-environment happy-dom
/**
 * Observability › Sources (spec §4.13): the stack's state is the server's word; `Redeploy` posts
 * the stack-deploy route once and then RE-POLLS the view (the old panel never refreshed); no
 * machine reads `Needs a machine` + `Add host ↗` and offers no deploy; `Status unknown` offers
 * `Check now`, which asks for one fresh reading; `Check config` is the dry-run health check, its
 * rows in words; `Add tools…` recommends by workload over the full catalogue.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { noHostSourcesWire, unknownSourcesWire } from "./_helpers/obs-fixtures";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const sourceGets = (calls: string[]) => calls.filter((u) => u.startsWith("/v1/observability/sources?")).length;

describe("the stack", () => {
  it("says its state, its containers and Grafana", async () => {
    await openObs("?view=sources");
    expect(text($("[data-testid='obs-stack-state']"))).toBe("Running");
    expect($$("[data-testid='obs-stack-containers'] .obs-chip").map((c) => text(c)))
      .toEqual(["prometheus: running", "grafana: running", "otel-collector: running"]);
    expect($("[data-testid='obs-stack-grafana']")?.getAttribute("href")).toBe("http://grafana.local");
    expect(text($("[data-testid='obs-stack-deploy']"))).toBe("Redeploy");
  });

  it("Redeploy posts once, then the view is read again", async () => {
    const rec = await openObs("?view=sources");
    const before = sourceGets(rec.calls);
    const b = $("[data-testid='obs-stack-deploy']")!;
    b.click(); b.click();
    await settle();
    expect(rec.writes).toHaveLength(1);
    expect(rec.writes[0].url).toBe("/v1/observability/stack/deploy");
    expect(rec.writes[0].body).toEqual({ project_id: "p1", host_target_id: "t_live", actor: "ceo" });
    expect(sourceGets(rec.calls)).toBeGreaterThan(before);
  });

  it("no machine: Needs a machine, Add host, and nothing to deploy", async () => {
    await openObs("?view=sources", { sources: noHostSourcesWire() });
    expect(text($("[data-testid='obs-stack-state']"))).toBe("Not deployed");
    expect(text($("[data-testid='obs-stack-needs-machine']"))).toBe("Needs a machineAdd host ↗");
    expect($("[data-testid='obs-stack-add-host']")?.getAttribute("href")).toBe("?area=infra&view=fleet");
    expect($("[data-testid='obs-stack-deploy']")).toBeNull();
    expect(text($("[data-testid='obs-gaps']"))).toContain("Nothing watched yet");
  });

  it("status unknown: Check now asks once for a fresh reading", async () => {
    const rec = await openObs("?view=sources", { sources: unknownSourcesWire() });
    const state = $("[data-testid='obs-stack-state']");
    expect(text(state)).toBe("Status unknown");
    expect(state?.getAttribute("title")).toBe("ssh: connection timed out");
    await click($("[data-testid='obs-stack-check']"));
    expect(rec.calls.filter((u) => u.includes("/v1/observability/sources?") && u.includes("fresh=1"))).toHaveLength(1);
  });
});

describe("scraped apps, sources, gaps and tools", () => {
  it("scraped apps flag the one nothing watches", async () => {
    await openObs("?view=sources");
    const rows = $$("[data-testid='obs-scraped-row']");
    expect(rows).toHaveLength(2);
    expect(text(rows[1])).toContain("Not watched");
    expect(text(rows[1])).toContain("No dashboard");
    expect(text(rows[0])).not.toContain("Not watched");
  });

  it("Check config runs the dry run and says each source's result in words", async () => {
    const rec = await openObs("?view=sources");
    expect($$("[data-testid='obs-source-row']")).toHaveLength(2);
    await click($("[data-testid='obs-check-config']"));
    expect(rec.writes[0].url).toBe("/v1/observability-suite/health-check");
    expect(rec.writes[0].body).toMatchObject({ dry_run: true, project_id: "p1" });
    const rows = $$("[data-testid='obs-check-row']");
    expect(rows.map((r) => text(r.querySelector(".inf-state-word")))).toEqual(["Config OK", "Misconfigured"]);
    expect(text($("[data-testid='obs-check-result']"))).toContain("1 of 2 OK");
  });

  it("Add tools recommends for a workload over the whole catalogue", async () => {
    const rec = await openObs("?view=sources");
    await click($("[data-testid='obs-add-tools']"));
    expect($$("[data-testid='obs-tools-catalog'] [data-testid='obs-tool-card']")).toHaveLength(3);
    expect(text($("[data-testid='obs-tools-catalog']"))).toContain("In BOSS stack");
    await click($("[data-testid='obs-tools-recommend']"));
    expect(rec.writes.at(-1)?.body).toEqual({ workload_profile: "web" });
    expect(text($("[data-testid='obs-tools-alongside']"))).toBe("Add alongsideloki");
    expect($$("[data-testid='obs-tools-result'] [data-testid='obs-tool-card']")).toHaveLength(2);
  });
});
