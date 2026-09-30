import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { emptyActivityLog, mapActivityLog } from "../lib/activity-log";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  PROJECT_ID: "boss-dogfood",
  apiUrlProblem: () => null,
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (
    props: React.ButtonHTMLAttributes<HTMLButtonElement> & { children?: React.ReactNode },
  ) =>
    React.createElement(
      "button",
      { type: "submit", className: props.className, disabled: props.disabled, name: props.name, value: props.value },
      props.children,
    ),
}));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const { ActivityLog } = await import("../app/activity-log-ui");
const { loadActivityLogAction } = await import("../app/activity-log-actions");

const pageSource = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const uiSource = readFileSync(resolve(__dirname, "../app/activity-log-ui.tsx"), "utf8");
const actionsSource = readFileSync(
  resolve(__dirname, "../app/activity-log-actions.ts"),
  "utf8",
);
const platformOpsSource = readFileSync(
  resolve(__dirname, "./platform-ops-ui.test.ts"),
  "utf8",
);

function existingAreaIdsSource(): string {
  const match = platformOpsSource.match(
    /const EXISTING_AREA_IDS = new Set\(\[([\s\S]*?)\]\)/,
  );
  return match?.[1] ?? "";
}

describe("Activity log island", () => {
  it("mounts on data-area=activity", () => {
    expect(pageSource).toContain('import { ActivityLog } from "./activity-log-ui"');
    expect(pageSource).toContain('data-area="activity"');
    expect(pageSource).toContain("<ActivityLog");
  });

  it("renders search, status/kind filters, and data-testid=activity-log", () => {
    const markup = renderToStaticMarkup(
      React.createElement(ActivityLog, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).toContain('data-testid="activity-log"');
    expect(markup).toContain('data-testid="activity-log-search"');
    expect(markup).toContain('data-testid="activity-log-filters"');
    expect(markup).toContain('data-testid="activity-log-status-filter"');
    expect(markup).toContain('data-testid="activity-log-kind-filter"');
    expect(markup).toContain('data-testid="activity-log-actor-filter"');
    expect(markup).toContain('name="q"');
    expect(markup).toContain('name="status"');
    expect(markup).toContain('name="kind"');
    expect(markup).toContain("Search");
    expect(markup).toContain("All statuses");
    expect(markup).toContain("All kinds");
    expect(markup).toContain("Loading activity…");
    expect(uiSource).toContain('data-testid="activity-log"');
    expect(actionsSource).toContain("/v1/activity");
  });

  it("keeps empty and error states honest and never silently applies", () => {
    const markup = renderToStaticMarkup(
      React.createElement(ActivityLog, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).toContain('data-testid="activity-log-loading"');
    expect(markup).not.toContain('data-testid="activity-log-row"');
    expect(markup).not.toMatch(/silently (apply|change)/i);
    expect(uiSource).toContain('data-testid="activity-log-empty"');
    expect(uiSource).toContain('data-testid="activity-log-error"');
    expect(uiSource).toContain("Nothing was invented");
    expect(uiSource).toMatch(/error-banner|role="alert"/);
    expect(uiSource).not.toMatch(/silently (apply|change)/i);
    expect(actionsSource).not.toMatch(/silently (apply|change)/i);
    expect(pageSource).not.toMatch(/silently (apply|change)/i);
  });
});

describe("Activity log mappers", () => {
  it("does not invent rows or a success status", () => {
    expect(mapActivityLog({ items: [] })).toMatchObject({
      apiReachable: true,
      items: [],
    });
    expect(mapActivityLog({ items: [{ title: "missing id" }] }).items).toEqual([]);
    expect(
      mapActivityLog({
        items: [{ id: "evt_1", kind: "event", status: "mystery" }],
      }).items[0],
    ).toMatchObject({
      id: "evt_1",
      kind: "event",
      status: "info",
    });
    const empty = emptyActivityLog("activity api down");
    expect(empty.apiReachable).toBe(false);
    expect(empty.items).toEqual([]);
    expect(empty.blockedReason).toBe("activity api down");
  });
});

describe("Activity log actions", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("gets /v1/activity with search and filter params", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        project_id: "boss-dogfood",
        items: [
          {
            id: "evt_1",
            source: "event",
            kind: "event",
            status: "failed",
            title: "budget reserve failed",
            actor: "ceo",
            entity_type: "budget",
            entity_id: "bud_1",
            created_at: "2026-08-14T12:00:00Z",
            data: {},
          },
        ],
      }),
    });
    const result = await loadActivityLogAction("boss-dogfood", {
      q: "budget",
      status: "failed",
      kind: "event",
      sort: "created_at",
      order: "desc",
    });
    expect(result.apiReachable).toBe(true);
    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.id).toBe("evt_1");
    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url.startsWith("https://boss-api.test/v1/activity?")).toBe(true);
    expect(url).toContain("project_id=boss-dogfood");
    expect(url).toContain("q=budget");
    expect(url).toContain("status=failed");
    expect(url).toContain("kind=event");
    expect(actionsSource).toContain("/v1/activity");
  });

  it("returns an empty blocked feed when the API errors", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => ({ detail: "unavailable" }),
    });
    const result = await loadActivityLogAction("boss-dogfood");
    expect(result.apiReachable).toBe(false);
    expect(result.items).toEqual([]);
    expect(result.blockedReason).toBe("API returned 503");
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/v1/activity");
  });
});

describe("platform-ops area registry", () => {
  it("includes activity in EXISTING_AREA_IDS", () => {
    expect(existingAreaIdsSource()).toMatch(/"activity"/);
  });
});
