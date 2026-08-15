import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (
    props: React.ButtonHTMLAttributes<HTMLButtonElement> & { children?: React.ReactNode },
  ) =>
    React.createElement(
      "button",
      { type: "submit", className: props.className, disabled: props.disabled },
      props.children,
    ),
}));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const { ProcessManager } = await import("../app/process-manager-ui");
const { tickProcessManagerAction } = await import("../app/process-manager-actions");

const pageSource = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const actionsSource = readFileSync(
  resolve(__dirname, "../app/process-manager-actions.ts"),
  "utf8",
);

describe("Process manager island", () => {
  it("mounts next to AgentProcesses inside data-area=processes", () => {
    expect(pageSource).toContain('import { AgentProcesses } from "./agent-process-ui"');
    expect(pageSource).toContain('import { ProcessManager } from "./process-manager-ui"');
    expect(pageSource).toContain('data-area="processes"');
    expect(pageSource).toContain("<AgentProcesses");
    expect(pageSource).toContain("<ProcessManager");
  });

  it("renders last metrics, last tick, proposals, and Tick now", () => {
    const markup = renderToStaticMarkup(
      React.createElement(ProcessManager, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).toContain('data-testid="process-manager"');
    expect(markup).toContain("Tick now");
    expect(markup).toContain("never auto-builds");
    expect(markup).toContain("Last tick");
    expect(markup).toContain("Metrics");
    expect(markup).toContain("Proposals");
    expect(markup).not.toMatch(/5\s*\/\s*5/);
    expect(actionsSource).toContain('"/v1/process-manager/tick"');
  });

  it("shows an honest empty state instead of invented scores", () => {
    const markup = renderToStaticMarkup(
      React.createElement(ProcessManager, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).toMatch(/No metrics recorded yet|Loading process manager/i);
    expect(markup).not.toContain("5/5");
  });
});

describe("Process manager actions", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("posts Tick now to /v1/process-manager/tick", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        project_id: "boss-dogfood",
        last_tick: { at: "2026-08-14T12:01:00Z", status: "ok" },
        proposals: [],
      }),
    });
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    const result = await tickProcessManagerAction(form);
    expect(result.ok).toBe(true);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "https://boss-api.test/v1/process-manager/tick",
    );
    expect(
      JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body)),
    ).toMatchObject({
      project_id: "boss-dogfood",
      actor: "ceo",
    });
  });
});
