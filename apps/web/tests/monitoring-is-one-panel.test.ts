import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Health and the observability stack were two panels listing the same three apps.
 *
 *   Health of deployed apps      Incident-Timeline Builder · staging · up · 100% · 192.0.2.202:3011
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
//
// Both panels (delivery-ui.tsx's health table, delivery-manage.tsx's stack card) and the area
// that merged them (monitoring-ui.tsx) retired into Observability: Apps is ONE Pulse table — one
// row per app, its dashboard in the app's drawer — and the stack lives in Sources. The rendered
// behaviour is held by observability-apps-is-one-pulse.test.tsx; this file keeps the promises
// pinned in the source so they cannot be split apart again.
const PULSE = codeOnly("app/observability/pulse.tsx");
const DRAWER = codeOnly("app/observability/app-drawer.tsx");
const SOURCES = codeOnly("app/observability/sources.tsx");
const OBS_VIEW = codeOnly("lib/obs-view.ts");
const LEGACY = codeOnly("app/legacy-areas.ts");
const WORKFLOWS = codeOnly("app/delivery-workflows-ui.tsx");

describe("one panel, not two", () => {
  it("the dashboard link sits with the app it describes", () => {
    // Health and graphs are one record per app now: the Pulse row opens a drawer whose facts
    // carry the dashboard, joined by the server (lib/obs-view.ts facts.dashboard_url).
    expect(OBS_VIEW).toContain("dashboardUrl: optStr(f.dashboard_url)");
    expect(DRAWER).toContain('data-testid="obs-app-dashboard">Dashboard ↗</a>');
  });

  it("the stack is not a second card on the Apps screen", () => {
    // It has its own tab (Sources); the Apps tab draws no stack block.
    expect(PULSE).not.toContain("obs-stack");
    expect(SOURCES).toContain('data-testid="obs-stack-state"');
  });

  it("an app the stack scrapes but health does not know about is named, not dropped", () => {
    // Health is keyed on deployment TARGETS and the stack on what it was told to scrape; the
    // two can legitimately differ, and a merge that silently loses rows is worse than two
    // panels that at least showed everything. (Rendered: observability-apps-is-one-pulse
    // "an app scraped but not health-checked is named, not dropped".)
    expect(OBS_VIEW).toContain("notWatched: arr(o.not_watched)");
    expect(PULSE).toContain('data-testid="obs-not-watched"');
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
  // The old area highlighted a row and scrolled to it; Observability opens that app's drawer —
  // the record itself — which is what "take me to the app this step watches" means.
  // (Rendered: observability-apps-is-one-pulse "the workflow's watch= link opens that app's
  // drawer" and "an unknown watch= becomes Find with the app's name".)
  it("the old monitoring link lands on Observability with watch= kept", () => {
    expect(LEGACY).toContain('if (area === "monitoring")');
  });

  it("the area reads the param", () => {
    expect(PULSE).toContain('new URLSearchParams(window.location.search).get("watch")');
  });

  it("matched on app and environment, and opens that app", () => {
    expect(PULSE).toContain("r.app === app && (!env || r.env === env)");
    expect(PULSE).toContain("go(hit ? { app: hit.targetId, watch: null } : { q: app || null, watch: null }");
  });
});
