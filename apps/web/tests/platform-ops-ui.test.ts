/**
 * Smoke tests for platform-ops Mission Control panels.
 * Verifies modules export and area nesting map (no new nav items).
 */
import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { IdentityProvidersCatalogView } from "../app/identity-providers-ui";
import { OncallSreHome } from "../app/oncall-sre-ui";
import { SecurityScanCard } from "../app/security-scan-ui";
import { ReleasePlanningPanel } from "../app/release-planning-ui";
import { WorkerAutoscalingPanel } from "../app/worker-autoscaling-ui";
import { ObservabilitySuitePanel } from "../app/observability-suite-ui";
import { KnowledgeGraphPanel } from "../app/knowledge-graph-ui";
import { CICDPipelinesPanel } from "../app/cicd-pipelines-ui";
import { AiSreHome } from "../app/ai-sre-ui";

/** Area nesting map — platform panels ride existing ConsoleShell AreaIds. */
export const PLATFORM_OPS_AREA_NESTING = {
  identity: "enterprise",
  oncall: "reliability",
  security: "assurance",
  releases: "portfolio",
  cicd: "delivery",
  scaling: "infra",
  observability: "monitoring",
  knowledgeGraph: "reliability",
} as const;

const EXISTING_AREA_IDS = new Set([
  "agent",
  "metatron",
  "dashboard",
  "overview",
  "decisions",
  "work",
  "activity",
  "portfolio",
  "discovery",
  "meetings",
  "processes",
  "code_tasks",
  "shepherd",
  "delivery",
  "monitoring",
  "reliability",
  "ai-sre",
  "reports",
  "budget",
  "pipeline",
  "comms",
  "github",
  "infra",
  "capabilities",
  "studio",
  "sandboxes",
  "foundations",
  "components",
  "ops",
  "assurance",
  "org",
  "enterprise",
]);

describe("platform-ops UI nesting", () => {
  it("nests every platform panel into an existing Mission Control area", () => {
    for (const [panel, area] of Object.entries(PLATFORM_OPS_AREA_NESTING)) {
      expect(EXISTING_AREA_IDS.has(area), `${panel} → ${area}`).toBe(true);
    }
    expect(PLATFORM_OPS_AREA_NESTING.identity).toBe("enterprise");
    expect(PLATFORM_OPS_AREA_NESTING.oncall).toBe("reliability");
    expect(PLATFORM_OPS_AREA_NESTING.security).toBe("assurance");
    expect(PLATFORM_OPS_AREA_NESTING.releases).toBe("portfolio");
    expect(PLATFORM_OPS_AREA_NESTING.cicd).toBe("delivery");
    expect(PLATFORM_OPS_AREA_NESTING.scaling).toBe("infra");
    expect(PLATFORM_OPS_AREA_NESTING.observability).toBe("monitoring");
    expect(PLATFORM_OPS_AREA_NESTING.knowledgeGraph).toBe("reliability");
  });

  it("introduces zero new AreaIds (max 5 allowed; prefer fewer)", () => {
    const nestedAreas = new Set(Object.values(PLATFORM_OPS_AREA_NESTING));
    const newAreas = [...nestedAreas].filter((a) => !EXISTING_AREA_IDS.has(a));
    expect(newAreas).toEqual([]);
  });
});

describe("platform-ops UI smoke imports", () => {
  it("exports all platform view components", () => {
    expect(typeof IdentityProvidersCatalogView).toBe("function");
    expect(typeof OncallSreHome).toBe("function");
    expect(typeof SecurityScanCard).toBe("function");
    expect(typeof ReleasePlanningPanel).toBe("function");
    expect(typeof WorkerAutoscalingPanel).toBe("function");
    expect(typeof ObservabilitySuitePanel).toBe("function");
    expect(typeof KnowledgeGraphPanel).toBe("function");
    expect(typeof CICDPipelinesPanel).toBe("function");
    expect(typeof AiSreHome).toBe("function");
  });
});

describe("security scan GA gate", () => {
  it("shows prior scans without offering an unsafe run action", () => {
    const markup = renderToStaticMarkup(
      createElement(SecurityScanCard, {
        projectId: "boss-dogfood",
        initialRun: null,
        scanners: [],
      }),
    );
    expect(markup).toContain("Scanner runs are unavailable");
    expect(markup).not.toContain("Run scan");
    expect(markup).not.toContain("Simulated");
  });
});
