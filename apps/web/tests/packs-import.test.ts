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
    importPack: vi.fn(),
    exportPack: vi.fn(),
  },
}));

describe("pack import/export client contracts", () => {
  it("importPack posts YAML to /v1/packs/import", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const yaml = "id: community_ops_v1\nversion: 0.1.0\n";
    const api = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: async (input, init) => {
        calls.push({ url: String(input), init });
        return new Response(
          JSON.stringify({
            pack_id: "community_ops_v1",
            version: "0.1.0",
            description: "Imported",
            status: "active",
            requires: [],
            roles: [],
            workflows: [],
            policies: [],
            skills: [],
            installed: true,
            role_results: [],
            install_status: "imported",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      },
    });

    await api.importPack({
      project_id: "boss-dogfood",
      yaml_content: yaml,
      actor: "ceo",
    });

    expect(calls[0]?.url).toBe("http://127.0.0.1:8000/v1/packs/import");
    expect(calls[0]?.init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      project_id: "boss-dogfood",
      yaml_content: yaml,
      actor: "ceo",
    });
  });

  it("exportPack calls the export endpoint and returns YAML text", async () => {
    const yaml = "id: core_v1\nversion: 1.0.0\ndescription: Core\n";
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const api = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl: async (input, init) => {
        calls.push({ url: String(input), init });
        return new Response(yaml, {
          status: 200,
          headers: { "Content-Type": "application/x-yaml" },
        });
      },
    });

    const exported = await api.exportPack("boss-dogfood", "core_v1");
    expect(exported).toBe(yaml);
    expect(calls[0]?.url).toBe(
      "http://127.0.0.1:8000/v1/packs/core_v1/export?project_id=boss-dogfood",
    );
    expect(calls[0]?.init?.method).toBe("GET");
  });
});

describe("pack import/export actions", async () => {
  const { revalidatePath } = await import("next/cache");
  const { client } = await import("../lib/client");
  const { exportPackAction, importPackAction } = await import("../app/actions");

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("importPackAction posts textarea YAML through client.importPack", async () => {
    const yaml = "id: local_pack_v1\nversion: 0.1.0\n";
    vi.mocked(client.importPack).mockResolvedValueOnce({
      pack_id: "local_pack_v1",
      version: "0.1.0",
      description: "Local",
      status: "active",
      requires: [],
      roles: [],
      workflows: [],
      policies: [],
      skills: [],
      installed: true,
      role_results: [],
      install_status: "imported",
    });

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("yaml", yaml);

    await expect(importPackAction(fd)).resolves.toMatchObject({ ok: true });
    expect(client.importPack).toHaveBeenCalledWith({
      project_id: "boss-dogfood",
      yaml_content: yaml,
      actor: "ceo",
    });
    expect(revalidatePath).toHaveBeenCalledWith("/");
  });

  it("importPackAction reads yaml_file when the textarea is empty", async () => {
    const yaml = "id: from_file_v1\nversion: 0.2.0\n";
    vi.mocked(client.importPack).mockResolvedValueOnce({
      pack_id: "from_file_v1",
      version: "0.2.0",
      description: "File",
      status: "active",
      requires: [],
      roles: [],
      workflows: [],
      policies: [],
      skills: [],
      installed: true,
      role_results: [],
      install_status: "imported",
    });

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("yaml", "   ");
    fd.set("yaml_file", new File([yaml], "from_file_v1.yaml", { type: "application/x-yaml" }));

    await importPackAction(fd);
    expect(client.importPack).toHaveBeenCalledWith({
      project_id: "boss-dogfood",
      yaml_content: yaml,
      actor: "ceo",
    });
  });

  it("importPackAction surfaces invalid YAML as a visible error", async () => {
    vi.mocked(client.importPack).mockRejectedValueOnce(
      new BossApiError("BOSS API request failed: 422", 422, {
        detail: "invalid pack yaml",
      }),
    );

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("yaml", "invalid: [ yaml");
    // REWRITTEN for the ActionResult contract (lib/action-result.ts): a server action now
    // returns its failure as a sentence instead of throwing one Next would redact.

    await expect(importPackAction(fd)).resolves.toMatchObject({

      ok: false, error: expect.stringContaining("invalid pack yaml"),

    });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("exportPackAction calls the export endpoint helper", async () => {
    const yaml = "id: core_v1\nversion: 1.0.0\n";
    vi.mocked(client.exportPack).mockResolvedValueOnce(yaml);

    const fd = new FormData();
    fd.set("project_id", "boss-dogfood");
    fd.set("actor", "ceo");
    fd.set("pack_id", "core_v1");

    await expect(exportPackAction(fd)).resolves.toMatchObject({ ok: true, data: {
      yaml,
      packId: "core_v1",
    } });
    expect(client.exportPack).toHaveBeenCalledWith("boss-dogfood", "core_v1");
  });
});

describe("Company Pack YAML island", async () => {
  const { PackExportButton, PackImportForm } = await import("../app/packs-import-ui");

  it("renders import/export controls and does not render marketplace", () => {
    const html = renderToStaticMarkup(
      React.createElement(
        "div",
        null,
        React.createElement(PackImportForm, {
          projectId: "boss-dogfood",
          actor: "ceo",
        }),
        React.createElement(PackExportButton, {
          projectId: "boss-dogfood",
          packId: "core_v1",
          actor: "ceo",
        }),
      ),
    );

    expect(html).toContain('data-testid="pack-import"');
    expect(html).toContain('data-testid="pack-export"');
    expect(html).toMatch(/Local Community import/i);
    expect(html).toMatch(/pack_manage/i);
    expect(html).toContain("Export");
    expect(html).not.toMatch(/sync hosted catalog/i);
    expect(html).not.toMatch(/hosted marketplace catalog/i);
    expect(html).not.toContain('data-testid="hosted-marketplace-card"');
    expect(html).not.toContain("paid-surface-hidden-marketplace");
  });

});
