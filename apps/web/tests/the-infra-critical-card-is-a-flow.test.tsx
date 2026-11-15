// @vitest-environment happy-dom
/**
 * S2 · "Infra Agent · critical work in progress" (BOSSAPP-1156): the incident is a row of nodes
 * drawn from the API's StripNode[] (detected → paused N → cleaning → … → resumed), the progress
 * to target, the safest-first steps with a mark each; the tier 3 question opens from its step's
 * Review; prose stays behind Details. The Command Center shows it above the Now cards, says
 * "Infra Agent is cleaning" in its status line, and stops its own motion.
 */
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { InfraCriticalCardView } from "../app/infra/infra-critical-card";
import { flowSentence } from "../app/infra/infra-flow";
import { motionOf } from "../app/command-center-ui";
import { openIncident } from "../lib/infra-storage";
import { demoIncident } from "../lib/infra-storage-demo";
import { codeOnly } from "./_helpers/source";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const incident = openIncident(demoIncident("1"))!;

describe("the critical card", () => {
  it("draws the incident as the API's flow nodes, one shape per node, the active one marked", async () => {
    await mount(<InfraCriticalCardView incident={incident} projectId="p1" actor="alice" />);
    const nodes = $$('[data-testid="icc-flow-node"]');
    expect(nodes.map((n) => n.querySelector(".ifl-cap")!.textContent)).toEqual(["Detected", "Paused", "Cleaning", "Retention", "Freed", "Resumed"]);
    expect(nodes.map((n) => n.dataset.state)).toEqual(["done", "done", "active", "waiting", "waiting", "waiting"]);
    expect(nodes[1].querySelector(".ifl-detail")!.textContent).toBe("14 runs");
    expect(nodes[1].querySelector(".ifl-at")!.textContent).toMatch(/^\d{2}:\d{2}$/);
    // One sentence for a screen reader, the whole flow in order.
    const flow = $('[data-testid="icc-flow"]')!;
    expect(flow.getAttribute("role")).toBe("img");
    expect(flow.getAttribute("aria-label")).toBe(flowSentence(incident.flow));
    expect(flow.getAttribute("aria-label")).toMatch(/Detected 9 % free at \d{2}:\d{2} → .*Cleaning 3 of 6( at \d{2}:\d{2})? \(now\)/);
  });

  it("says the cause, the GB freed and the ETA, and the bar measures the way to the target", async () => {
    await mount(<InfraCriticalCardView incident={{ ...incident, freePctNow: 17, freedGb: 38, etaSeconds: 360 }} projectId="p1" actor="alice" />);
    expect($('[data-testid="icc-cause"]')!.textContent).toContain("backups grew 18 GB this week");
    expect($('[data-testid="icc-freed"]')!.textContent).toContain("38 GB");
    expect($('[data-testid="icc-freed"]')!.textContent).toContain("~6 min left");
    expect($('[data-testid="icc-sev"]')!.textContent).toContain("Critical");
    // The bar is an SVG rect in a 0–100 box: its width is the percentage, never an inline style.
    const fill = $('[data-testid="icc-progress"] .icc-bar-fill')!;
    expect(fill.getAttribute("width")).toBe("50");
    expect($('[data-testid="icc-progress"]')!.getAttribute("aria-label")).toBe("17 % free, target 25 %");
  });

  it("lists the steps safest first with a mark each, GB per step, and the tier 3 one asks first", async () => {
    await mount(<InfraCriticalCardView incident={incident} projectId="p1" actor="alice" />);
    const steps = $$('[data-testid="icc-step"]');
    expect(steps).toHaveLength(6);
    expect(steps.map((s) => s.dataset.state)).toEqual(["done", "done", "active", "waiting", "waiting", "ask"]);
    expect(steps[0].textContent).toContain("12 GB");
    expect(steps.at(-1)!.dataset.tier).toBe("3");
    expect($('[data-testid="icc-paused"]')!.textContent).toBe("14 runs · new work not claimed");
    expect(document.body.textContent).toContain("dry-run shown first");
    expect(document.body.textContent).toContain("labelled items only");
  });

  it("opens the deletion card from the step's Review, and keeps the prose behind Details", async () => {
    // The question is one read of the decision (GET /v1/decisions/{id}); answered here from the sample.
    window.sessionStorage.setItem("boss.infra_demo", "asking");
    await mount(<InfraCriticalCardView incident={incident} projectId="p1" actor="alice" />);
    expect($('[data-testid="icc-review-card"]')).toBeNull();
    const details = $('[data-testid="icc-details"]') as HTMLDetailsElement;
    expect(details.open).toBe(false);
    await click($('[data-testid="icc-review"]'));
    for (let i = 0; i < 5 && !$('[data-testid="storage-delete-decision"]'); i += 1) await settle();
    expect($('[data-testid="icc-review-card"] [data-testid="storage-delete-decision"]')).not.toBeNull();
    expect($('[data-testid="sdd-sentence"]')!.textContent).toContain("348 pre-upgrade backups");
    await click($('[data-testid="icc-review"]'));
    expect($('[data-testid="icc-review-card"]')).toBeNull();
    window.sessionStorage.removeItem("boss.infra_demo");
  });

  it("is the card of a resolved incident too, green, with every run resumed", async () => {
    await mount(<InfraCriticalCardView incident={{ ...incident, status: "resolved", flow: [...incident.flow, { ...incident.flow[0], key: "reported", label: "Learned", type: "learning" }] }} projectId="p1" actor="alice" />);
    expect($('[data-testid="infra-critical-card"]')!.dataset.status).toBe("resolved");
    expect($('[data-testid="icc-sev"]')!.textContent).toContain("Resolved");
    expect($$('[data-testid="icc-flow-node"]').at(-1)!.querySelector(".ifl-cap")!.textContent).toBe("Learned");
  });
});

describe("the Command Center", () => {
  it("shows the card above the Now cards, says so in the status line, and stops its motion", () => {
    const src = codeOnly(join(__dirname, "../app/command-center-ui.tsx"));
    const card = src.indexOf("<InfraCriticalCardView");
    expect(card).toBeGreaterThan(0);
    expect(card).toBeLessThan(src.indexOf("{nowCards}"));
    expect(src).toMatch(/withInfraStatus\(statusOf\(/);
    const ok = { paused: false, hidden: false, reachable: true, hostCritical: false, reduced: false };
    expect(motionOf(ok)).toBe("full");
    expect(motionOf({ ...ok, infraIncident: true })).toBe("none");
  });
});
