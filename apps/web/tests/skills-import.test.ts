import { readFileSync } from "node:fs";
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

    await expect(importSkillAction(fd)).resolves.toMatchObject({ ok: true });
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
    // REWRITTEN for the ActionResult contract (lib/action-result.ts): a server action now
    // returns its failure as a sentence instead of throwing one Next would redact.

    await expect(importSkillAction(fd)).resolves.toMatchObject({

      ok: false, error: expect.stringContaining("bundle must be valid JSON"),

    });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("exportSkillAction calls the export endpoint helper", async () => {
    vi.mocked(client.exportSkill).mockResolvedValueOnce(skillBundle);

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("skill_id", "local.community.skill");

    await expect(exportSkillAction(fd)).resolves.toMatchObject({ ok: true, data: {
      yaml: skillBundle,
      skillId: "local.community.skill",
    } });
    expect(client.exportSkill).toHaveBeenCalledWith(
      "boss-dogfood",
      "local.community.skill",
      "ceo",
    );
  });
});

describe("skill import lives in the Library", () => {
  // REWRITTEN for the Kit (skills-mcp spec §5.5): import is Library › ⋯ › Import, and the
  // Access and packs card is a link to it. The server action contract above is unchanged.
  it("the Library's Import posts the YAML through importSkillAction", () => {
    const SHEET = readFileSync("app/skills/import-sheet.tsx", "utf8");
    expect(SHEET).toContain('callAction(importSkillAction, undefined, "skill:manage")(form)');
    expect(SHEET).toContain('form.set("yaml", body)');
  });

  it("Access and packs links there instead of carrying a second form", () => {
    const PAGE = readFileSync("app/page.tsx", "utf8");
    expect(PAGE).not.toContain("SkillImportForm");
    expect(PAGE).toContain('href="?area=skills&tab=library" data-testid="org-skills-link"');
  });
});
