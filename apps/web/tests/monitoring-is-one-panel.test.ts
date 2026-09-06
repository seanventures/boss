import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { codeOnly } from "./_helpers/source";

/**
 * Health and the observability stack were two panels listing the same three apps.
 *
 *   Health of deployed apps      Incident-Timeline Builder · staging · up · 100% · 2.29.2.202:3011
 *   Observability stack          Incident-Timeline Builder · staging · …:3011/metrics · dashboard ↗
 *
 * Same names, same environments, one saying each app was up and the other saying where to look
 * at it. "Is it healthy" and "show me its graphs" are the same question asked twice, and
 * answering them in two blocks meant scrolling between them to follow one app.
 *
 * And the workflow's "Set up monitoring" step said "watching Meeting cost meter · staging" with
 * nowhere to go — the operator had to open Monitoring and then find that app in it, for a step
 * whose entire subject is that one app.
 */
const HEALTH = codeOnly("app/delivery-ui.tsx");
const STACK = codeOnly("app/delivery-manage.tsx");
const AREA = codeOnly("app/monitoring-ui.tsx");
const WORKFLOWS = codeOnly("app/delivery-workflows-ui.tsx");
const CSS = readFileSync(join(process.cwd(), "app/globals.css"), "utf8");

describe("one panel, not two", () => {
  it("the dashboard link sits on the health row it describes", () => {
    expect(HEALTH).toContain("dashboards?: DashboardIndex");
    expect(HEALTH).toContain("Open dashboard ↗");
    expect(HEALTH).toContain("scraped at");
  });

  it("the two lists are joined on app and environment", () => {
    expect(HEALTH).toContain("export const dashboardKey");
    expect(AREA).toContain("dashboardKey(a.app, a.environment)");
    expect(AREA).toContain("dashboardKey(t.app_name, t.environment)");
  });

  it("the stack becomes a footer rather than a second card", () => {
    expect(AREA).toContain("footer={");
    expect(AREA).toContain("compact");
    // A nested ct-block would draw a second bordered card and undo the merge.
    expect(STACK).toContain("const Frame = compact");
  });

  it("and stops repeating the app table in that mode", () => {
    expect(STACK).toContain("{compact ? (");
  });

  it("an app the stack scrapes but health does not know about is named, not dropped", () => {
    // Health is keyed on deployment TARGETS and the stack on what it was told to scrape; the
    // two can legitimately differ, and a merge that silently loses rows is worse than two
    // panels that at least showed everything.
    expect(AREA).toContain("Scraped but not health-checked");
    expect(AREA).toContain('data-testid="monitoring-orphans"');
  });

  it("the standalone stack panel still stands alone", () => {
    // Health can be empty while the stack is not — the merged panel renders nothing then, so
    // the stack still needs somewhere to live.
    expect(AREA).toContain("healthEmpty && !stackEmpty ?");
  });

  it("the copy no longer points at a table that moved", () => {
    expect(STACK).toContain('{compact ? "above" : "below"}');
  });
});

describe("the monitoring step links to the app it is watching", () => {
  it("one link per watched target", () => {
    const fn = WORKFLOWS.slice(WORKFLOWS.indexOf("if (Array.isArray(output.targets))"));
    expect(fn.slice(0, 800)).toContain('label: `monitor ${app}`');
    expect(fn.slice(0, 800)).toContain('area: "monitoring"');
  });

  it("naming the app and its environment, not just the area", () => {
    // `?area=monitoring` alone is the landing page — "go and find it" for a step that already
    // knows which app it means.
    const fn = WORKFLOWS.slice(WORKFLOWS.indexOf("if (Array.isArray(output.targets))"));
    expect(fn.slice(0, 800)).toContain("watch: env ? `${app}|${env}` : app");
  });

  it("a target with no app name produces no link", () => {
    const fn = WORKFLOWS.slice(WORKFLOWS.indexOf("if (Array.isArray(output.targets))"));
    expect(fn.slice(0, 800)).toContain("if (!app) continue;");
  });
});

describe("the link actually arrives somewhere", () => {
  it("each health row is addressable", () => {
    expect(HEALTH).toContain("id={`watch-${dashboardKey(t.app_name, t.environment)}`}");
  });

  it("the area reads the param and re-reads it on navigation", () => {
    // Areas switch client-side, so the param can change without a server render.
    expect(AREA).toContain('new URLSearchParams(window.location.search).get("watch")');
    expect(AREA).toContain('window.addEventListener("popstate", read)');
  });

  it("and the row is brought into view", () => {
    // Landing at the top of a list of every deployed app, with the one you asked for somewhere
    // below, has not taken you anywhere.
    expect(HEALTH).toContain("scrollIntoView");
  });

  it("the row is marked so the eye finds it", () => {
    expect(HEALTH).toContain('data-watched=');
    expect(CSS).toContain('.dh-row[data-watched="yes"]');
  });

  it("the mark fades rather than becoming permanent decoration", () => {
    expect(CSS).toContain("dh-watch-fade");
  });

  it("and a reader who turned motion off still sees the mark", () => {
    const reduced = CSS.split("@media (prefers-reduced-motion: reduce)").slice(1).join("\n");
    expect(reduced).toContain(".dh-row[data-watched=");
  });
});
