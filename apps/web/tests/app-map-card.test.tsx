// @vitest-environment happy-dom
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppMapCard } from "../app/multirepo/app-map-card";
import { click, mountAs, stubFetch, tid, type as typeInto, unmount } from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

afterEach(unmount);

const PROPOSED_ENTRY = {
  app_key: "helpdesk-ai",
  app_name: "HelpDesk AI",
  root_path: "helpdesk-ai",
  workspace_root: ".",
  stack: { language: "node", framework: "express", package_manager: "npm" },
  commands: { install: "npm install", build: "npm run build", test: "npm test" },
  run: { command: "node src/server.js", health: "/health" },
  manifests: ["helpdesk-ai/package.json"],
  shared_paths: [],
  provenance: { source: "marker", confidence: "medium" },
  fingerprint: "abcdef1234567890",
  baseline: {},
  status: "proposed",
};

describe("AppMapCard (BOSSAPP-556)", () => {
  it("shows proposed App Map and confirms it on click", async () => {
    const calls = stubFetch((c) => {
      if (c.method === "GET" && c.path === "/v1/apps/helpdesk-ai/app-map") {
        return { body: { project_id: "p1", app_key: "helpdesk-ai", status: "proposed", app_map: PROPOSED_ENTRY } };
      }
      if (c.method === "PUT" && c.path === "/v1/apps/helpdesk-ai/app-map") {
        return {
          body: {
            project_id: "p1",
            app_key: "helpdesk-ai",
            status: "confirmed",
            app_map: { ...PROPOSED_ENTRY, status: "confirmed" },
          },
        };
      }
      return undefined;
    });

    await mountAs(<AppMapCard projectId="p1" appKey="helpdesk-ai" appName="HelpDesk AI" />, "owner");
    expect(tid("app-map-status")?.textContent).toBe("Needs confirmation");
    expect(tid("app-map-root-path")?.textContent).toBe("helpdesk-ai");

    await click(tid("app-map-confirm")!);
    expect(calls.some((c) => c.method === "PUT" && c.path === "/v1/apps/helpdesk-ai/app-map")).toBe(true);
    expect(tid("app-map-status")?.textContent).toBe("Confirmed");
  });

  it("handles ambiguous scan results by presenting candidates and confirming the chosen one", async () => {
    const calls = stubFetch((c) => {
      if (c.method === "GET" && c.path === "/v1/apps/storefront/app-map") {
        return { body: { project_id: "p1", app_key: "storefront", status: "unmapped", app_map: null } };
      }
      if (c.method === "POST" && c.path === "/v1/projects/p1/app-maps/scan") {
        const cands = [
          { ...PROPOSED_ENTRY, app_key: "web", root_path: "apps/web", provenance: { source: "workspace_graph" } },
          { ...PROPOSED_ENTRY, app_key: "api", root_path: "apps/api", provenance: { source: "workspace_graph" } },
        ];
        return {
          body: {
            project_id: "p1",
            status: "ambiguous",
            source: "workspace_graph:turborepo",
            workspace_root: ".",
            selected: null,
            apps: cands,
            candidates: cands,
            shared_paths: ["packages/ui"],
            warnings: [],
            refusal: "Multiple apps detected",
          },
        };
      }
      if (c.method === "PUT" && c.path === "/v1/apps/storefront/app-map") {
        return {
          body: {
            project_id: "p1",
            app_key: "storefront",
            status: "confirmed",
            app_map: { ...PROPOSED_ENTRY, app_key: "storefront", root_path: "apps/api", status: "confirmed" },
          },
        };
      }
      return undefined;
    });

    await mountAs(<AppMapCard projectId="p1" appKey="storefront" appName="Storefront" />, "owner");
    await click(tid("app-map-scan")!);
    expect(tid("app-map-candidates")).not.toBeNull();

    const apiRadio = tid("app-map-candidate-apps/api")!.querySelector("input") as HTMLInputElement;
    await click(apiRadio);
    await click(tid("app-map-confirm-candidate")!);

    const put = calls.find((c) => c.method === "PUT" && c.path === "/v1/apps/storefront/app-map");
    expect((put?.body as { root_path?: string })?.root_path).toBe("apps/api");
    expect(tid("app-map-status")?.textContent).toBe("Confirmed");
  });

  it("displays stale drift banner, allows inline editing, and runs baseline verify", async () => {
    stubFetch((c) => {
      if (c.method === "GET" && c.path === "/v1/apps/svc/app-map") {
        return {
          body: {
            project_id: "p1",
            app_key: "svc",
            status: "stale",
            app_map: { ...PROPOSED_ENTRY, app_key: "svc", root_path: "svc", status: "stale" },
            drift: { drifted: true, added_manifests: ["svc/pyproject.toml"] },
          },
        };
      }
      if (c.method === "PUT" && c.path === "/v1/apps/svc/app-map") {
        return {
          body: {
            project_id: "p1",
            app_key: "svc",
            status: "confirmed",
            app_map: {
              ...PROPOSED_ENTRY,
              app_key: "svc",
              root_path: "services/svc",
              shared_paths: ["libs/common"],
              status: "confirmed",
            },
          },
        };
      }
      if (c.method === "POST" && c.path === "/v1/apps/svc/app-map/baseline") {
        return {
          body: {
            project_id: "p1",
            app_key: "svc",
            baseline: { status: "passed", commit_sha: "abc1234567", duration_ms: 420 },
            app_map: {
              ...PROPOSED_ENTRY,
              app_key: "svc",
              root_path: "services/svc",
              status: "confirmed",
              baseline: { status: "passed", commit_sha: "abc1234567", duration_ms: 420 },
            },
          },
        };
      }
      return undefined;
    });

    await mountAs(<AppMapCard projectId="p1" appKey="svc" appName="Svc" />, "owner");
    expect(tid("app-map-drift")?.textContent).toContain("Manifests changed");

    await click(tid("app-map-edit")!);
    await typeInto(tid("app-map-input-root") as HTMLInputElement, "services/svc");
    await typeInto(tid("app-map-input-shared") as HTMLInputElement, "libs/common");
    await click(tid("app-map-save")!);

    expect(tid("app-map-root-path")?.textContent).toBe("services/svc");
    await click(tid("app-map-verify-baseline")!);
    expect(tid("app-map-baseline-status")?.textContent).toContain("Passed");
  });
});
