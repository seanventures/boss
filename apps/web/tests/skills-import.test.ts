import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BossApiError, createBossApiClient } from "@boss/api-client";

vi.mock("next/cache", () => ({
  revalidatePath: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
  redirect: vi.fn(),
}));

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  API_URL: "http://127.0.0.1:8000",
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
  client: {
    importSkill: vi.fn(),
    exportSkill: vi.fn(),
  },
}));

const skillBundle = `{
  "format": "boss.capability-bundle/v1",
  "kind": "skill",
  "id": "local.community.skill"
}
`;

describe("skill import/export client contracts", () => {
  it("importSkill posts the capability bundle to /v1/skills/import", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const api = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: async (input, init) => {
        calls.push({ url: String(input), init });
        return new Response(
          JSON.stringify({
            id: "psk_local",
            project_id: "boss-dogfood",
            skill_id: "local.community.skill",
            source: "tenant",
            enabled: true,
            on_demand: false,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      },
    });

    await api.importSkill({
      project_id: "boss-dogfood",
      bundle: skillBundle,
      allow_untrusted: true,
      actor: "ceo",
    });

    expect(calls[0]?.url).toBe("http://127.0.0.1:8000/v1/skills/import");
    expect(calls[0]?.init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      project_id: "boss-dogfood",
      bundle: skillBundle,
      allow_untrusted: true,
      actor: "ceo",
    });
  });

  it("exportSkill calls the export endpoint and returns the raw bundle text", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const api = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: async (input, init) => {
        calls.push({ url: String(input), init });
        return new Response(skillBundle, {
          status: 200,
          headers: { "Content-Type": "application/vnd.boss.capability+json" },
        });
      },
    });

    const exported = await api.exportSkill("boss-dogfood", "local.community.skill", "ceo");
    expect(exported).toBe(skillBundle);
    expect(calls[0]?.url).toBe(
      "http://127.0.0.1:8000/v1/skills/local.community.skill/export?project_id=boss-dogfood&actor=ceo",
    );
    expect(calls[0]?.init?.method).toBe("GET");
  });
});

describe("skill import/export actions", async () => {
  const { revalidatePath } = await import("next/cache");
  const { client } = await import("../lib/client");
  const { exportSkillAction, importSkillAction } = await import("../app/actions");

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("importSkillAction posts textarea YAML through client.importSkill", async () => {
    vi.mocked(client.importSkill).mockResolvedValueOnce({
      id: "psk_local",
      skill_id: "local.community.skill",
      enabled: true,
    });

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("yaml", skillBundle);

    await expect(importSkillAction(fd)).resolves.toBeUndefined();
    expect(client.importSkill).toHaveBeenCalledWith({
      project_id: "boss-dogfood",
      bundle: skillBundle,
      allow_untrusted: true,
      actor: "ceo",
    });
    expect(revalidatePath).toHaveBeenCalledWith("/");
  });

  it("importSkillAction reads yaml_file when the textarea is empty", async () => {
    vi.mocked(client.importSkill).mockResolvedValueOnce({
      id: "psk_from_file",
      skill_id: "from.file.skill",
      enabled: true,
    });

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("yaml", "   ");
    fd.set(
      "yaml_file",
      new File([skillBundle], "from.file.skill.yaml", { type: "application/x-yaml" }),
    );

    await importSkillAction(fd);
    expect(client.importSkill).toHaveBeenCalledWith({
      project_id: "boss-dogfood",
      bundle: skillBundle,
      allow_untrusted: true,
      actor: "ceo",
    });
  });

  it("importSkillAction surfaces invalid YAML as a visible error", async () => {
    vi.mocked(client.importSkill).mockRejectedValueOnce(
      new BossApiError("BOSS API request failed: 422", 422, {
        detail: "bundle must be valid JSON",
      }),
    );

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("yaml", "invalid: [ yaml");

    await expect(importSkillAction(fd)).rejects.toThrow("bundle must be valid JSON");
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("exportSkillAction calls the export endpoint helper", async () => {
    vi.mocked(client.exportSkill).mockResolvedValueOnce(skillBundle);

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("skill_id", "local.community.skill");

    await expect(exportSkillAction(fd)).resolves.toEqual({
      yaml: skillBundle,
      skillId: "local.community.skill",
    });
    expect(client.exportSkill).toHaveBeenCalledWith(
      "boss-dogfood",
      "local.community.skill",
      "ceo",
    );
  });
});

describe("Community skill YAML island", async () => {
  const { SkillExportButton, SkillImportForm } = await import("../app/skills-import-ui");
  const { PaidSurfaceGate } = await import("../app/edition-ui");

  it("renders import/export controls and does not render marketplace", () => {
    const html = renderToStaticMarkup(
      React.createElement(
        "div",
        null,
        React.createElement(SkillImportForm, {
          projectId: "boss-dogfood",
          actor: "ceo",
        }),
        React.createElement(SkillExportButton, {
          projectId: "boss-dogfood",
          skillId: "local.community.skill",
          actor: "ceo",
        }),
      ),
    );

    expect(html).toContain('data-testid="skill-import"');
    expect(html).toContain('data-testid="skill-export"');
    expect(html).toMatch(/Local Community import/i);
    expect(html).toMatch(/pack_manage/i);
    expect(html).toContain("Export");
    expect(html).not.toMatch(/sync hosted catalog/i);
    expect(html).not.toMatch(/hosted marketplace catalog/i);
    expect(html).not.toContain('data-testid="hosted-marketplace-card"');
    expect(html).not.toContain("paid-surface-hidden-marketplace");
  });

  it("keeps hosted marketplace hidden on Community", () => {
    const html = renderToStaticMarkup(
      React.createElement(PaidSurfaceGate, {
        editionId: "community",
        surface: "marketplace",
        title: "Hosted marketplace catalog",
        children: React.createElement(
          "article",
          { "data-testid": "hosted-marketplace-card" },
          "Sync hosted catalog",
        ),
      }),
    );
    expect(html).toContain("paid-surface-hidden-marketplace");
    expect(html).not.toContain("hosted-marketplace-card");
    expect(html).not.toContain("Sync hosted catalog");
  });
});
