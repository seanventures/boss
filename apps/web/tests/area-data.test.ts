/**
 * BOSS-13: per-area data loaders exist, gate correctly, and page imports them.
 */
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  API_URL: "http://localhost:9",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  authHeaders: () => ({}),
  client: {
    getBudgetStatus: vi.fn().mockRejectedValue(new Error("offline")),
    listBudgetAllocations: vi.fn().mockRejectedValue(new Error("offline")),
    listProviderDefinitions: vi.fn().mockRejectedValue(new Error("offline")),
    listProviderAccounts: vi.fn().mockRejectedValue(new Error("offline")),
    listExternalResources: vi.fn().mockRejectedValue(new Error("offline")),
    listProviderCosts: vi.fn().mockRejectedValue(new Error("offline")),
    getAssuranceOverview: vi.fn().mockRejectedValue(new Error("offline")),
    getModelGatewayUsage: vi.fn().mockRejectedValue(new Error("offline")),
    getMcpServers: vi.fn().mockRejectedValue(new Error("offline")),
    getEdition: vi.fn().mockRejectedValue(new Error("offline")),
    listInfraResources: vi.fn().mockRejectedValue(new Error("offline")),
  },
}));

describe("area-data loaders (BOSS-13)", () => {
  it("exports budget, infra, and assurance loaders", async () => {
    const mod = await import("../lib/area-data");
    expect(typeof mod.loadBudgetAreaData).toBe("function");
    expect(typeof mod.loadInfraAreaData).toBe("function");
    expect(typeof mod.loadAssuranceAreaData).toBe("function");
    expect(typeof mod.shouldLoadBudgetArea).toBe("function");
    expect(typeof mod.shouldLoadInfraArea).toBe("function");
    expect(typeof mod.shouldLoadAssuranceArea).toBe("function");
  });

  it("gates inactive areas without network work", async () => {
    const {
      loadBudgetAreaData,
      loadInfraAreaData,
      loadAssuranceAreaData,
      shouldLoadBudgetArea,
      shouldLoadInfraArea,
      shouldLoadAssuranceArea,
    } = await import("../lib/area-data");

    expect(shouldLoadBudgetArea(undefined)).toBe(true);
    expect(shouldLoadBudgetArea("overview")).toBe(true);
    expect(shouldLoadBudgetArea("budget")).toBe(true);
    expect(shouldLoadBudgetArea("meetings")).toBe(false);

    expect(shouldLoadInfraArea("infra")).toBe(true);
    expect(shouldLoadInfraArea("meetings")).toBe(false);

    expect(shouldLoadAssuranceArea("assurance")).toBe(true);
    expect(shouldLoadAssuranceArea("meetings")).toBe(false);

    const budget = await loadBudgetAreaData({
      projectId: "boss-dogfood",
      active: false,
    });
    expect(budget.loaded).toBe(false);
    expect(budget.budgetStatus.apiReachable).toBe(false);
    expect(budget.finopsSummary.spendByProvider).toEqual([]);

    const infra = await loadInfraAreaData({
      projectId: "boss-dogfood",
      active: false,
    });
    expect(infra.loaded).toBe(false);
    expect(infra.providerOps.providerAccounts).toEqual([]);

    const assurance = await loadAssuranceAreaData({
      projectId: "boss-dogfood",
      active: false,
    });
    expect(assurance.loaded).toBe(false);
    expect(assurance.securityScanners).toEqual([]);
  });

  it("page.tsx imports area-data loaders (vertical slice wired)", () => {
    const pagePath = resolve(__dirname, "../app/page.tsx");
    const source = readFileSync(pagePath, "utf8");
    expect(source).toContain('from "../lib/area-data"');
    expect(source).toContain("loadBudgetAreaData");
    expect(source).toContain("loadInfraAreaData");
    expect(source).toContain("loadAssuranceAreaData");
    expect(source).toContain("shouldLoadBudgetArea");
    expect(source).toContain("shouldLoadInfraArea");
    expect(source).toContain("shouldLoadAssuranceArea");
  });
});
