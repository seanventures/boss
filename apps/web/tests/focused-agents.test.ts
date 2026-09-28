import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { mapFocusedAgentList } from "../lib/focused-agents";

vi.mock("next/cache", () => ({
  revalidatePath: vi.fn(),
}));

vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
}));

vi.mock("../app/submit-button", () => ({
  SubmitButton: (
    props: React.ButtonHTMLAttributes<HTMLButtonElement> & {
      children?: React.ReactNode;
    },
  ) =>
    React.createElement(
      "button",
      { type: "submit", className: props.className, disabled: props.disabled },
      props.children,
    ),
}));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const { FocusedAgents } = await import("../app/focused-agents-ui");
const { createFocusedAgentAction, wakeFocusedAgentAction } = await import(
  "../app/focused-agents-actions"
);

const pageSource = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const actionsSource = readFileSync(
  resolve(__dirname, "../app/focused-agents-actions.ts"),
  "utf8",
);

describe("Focused agents island", () => {
  it("mounts the builder on Agent Studio", () => {
    expect(pageSource).toContain('import { FocusedAgents } from "./focused-agents-ui"');
    expect(pageSource).toContain("<FocusedAgents");
    expect(pageSource).toContain('data-area="studio"');
  });

  it("renders builder fields and always-on copy", () => {
    const markup = renderToStaticMarkup(
      React.createElement(FocusedAgents, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).toContain('data-testid="focused-agents"');
    expect(markup).toContain('data-testid="focused-agents-builder"');
    expect(markup).toContain("Create and start");
    expect(markup).toContain("always on");
    expect(markup).toContain("preview_keeper");
    expect(markup).toContain("launch factory job");
    expect(markup).toContain("Create and start");
    expect(markup).not.toMatch(/silently (apply|change)/i);
  });

  it("does not invent agents when the list is empty", () => {
    const state = mapFocusedAgentList({ project_id: "boss-dogfood", agents: [] });
    expect(state.apiReachable).toBe(true);
    expect(state.agents).toEqual([]);
  });
});

describe("Focused agent actions", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("posts a new agent to /v1/focused-agents", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({ id: "fagent_1", name: "Preview keeper" }),
    });
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("kind", "preview_keeper");
    form.set("name", "Preview keeper");
    form.set("focus", "Keep views up");
    form.set("mode", "always_on");
    form.set("interval_seconds", "90");
    form.append("allowed_actions", "restart_preview");
    const result = await createFocusedAgentAction(form);
    expect(result.ok).toBe(true);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "https://boss-api.test/v1/focused-agents",
    );
    expect(
      JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body)),
    ).toMatchObject({
      project_id: "boss-dogfood",
      kind: "preview_keeper",
      start: true,
      allowed_actions: ["restart_preview"],
    });
    expect(actionsSource).toContain('"/v1/focused-agents"');
  });

  it("wakes an agent on demand", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ status: "verified", applied: [] }),
    });
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("agent_id", "fagent_1");
    const result = await wakeFocusedAgentAction(form);
    expect(result.ok).toBe(true);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "https://boss-api.test/v1/focused-agents/fagent_1/wake",
    );
  });
});
