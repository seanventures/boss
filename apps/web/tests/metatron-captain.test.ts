import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CAPTAIN_UNAVAILABLE_404,
  captainOperatorHref,
  describeMetatronCaptainUnavailable,
  isLoopbackOperatorUrl,
  mapMetatronCaptainSession,
  unavailableMetatronCaptain,
} from "../lib/metatron-captain";

vi.mock("next/cache", () => ({
  revalidatePath: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
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

const { MetatronCaptain, MetatronCaptainView } = await import(
  "../app/metatron-captain-ui"
);
const {
  sendMetatronCaptainMessageAction,
  proposeMetatronCaptainDriveAction,
} = await import("../app/metatron-captain-actions");

const islandSource = readFileSync(
  resolve(__dirname, "../app/metatron-captain-ui.tsx"),
  "utf8",
);
const pageSource = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const actionsSource = readFileSync(
  resolve(__dirname, "../app/metatron-captain-actions.ts"),
  "utf8",
);

const noop = () => undefined;

function html(node: React.ReactElement) {
  return renderToStaticMarkup(node);
}

describe("Metatron Captain island", () => {
  it("mounts on Mission Control overview beside the existing Status Query", () => {
    expect(pageSource).toContain(
      'import { MetatronCaptain } from "./metatron-captain-ui"',
    );
    expect(pageSource).toContain("<MetatronCaptain");
    expect(pageSource).toContain("Status Query");
    expect(pageSource).toContain("Ask Metatron");
    expect(pageSource).toContain('name="q"');
    expect(pageSource.split("<MetatronCaptain").length - 1).toBe(1);

    const overview = pageSource.slice(
      pageSource.lastIndexOf("<section", pageSource.indexOf("<MetatronCaptain")),
      pageSource.indexOf("<MetatronCaptain") + 160,
    );
    expect(overview).toContain('data-area="overview"');
    expect(overview).toContain("Status Query");
  });

  it("renders chat, goal, watch, and proposal-only drive fields", () => {
    const markup = html(
      React.createElement(MetatronCaptain, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );

    expect(markup).toContain('data-testid="metatron-captain"');
    expect(markup).toContain("Community self-hosted captain");
    expect(markup).toContain('data-testid="metatron-captain-chat"');
    expect(markup).toContain('name="message"');
    expect(markup).toContain('name="project_id"');
    expect(markup).toContain('name="actor"');
    expect(markup).toContain('value="boss-dogfood"');
    expect(markup).toContain('value="ceo"');
    expect(markup).toContain('data-testid="metatron-captain-goal"');
    expect(markup).toContain('name="title"');
    expect(markup).toContain('name="kind"');
    expect(markup).toContain('name="description"');
    expect(markup).toContain("working_software");
    expect(markup).toContain('data-testid="metatron-captain-watch"');
    expect(markup).toContain("Refresh watch");
    expect(markup).toContain('data-testid="metatron-captain-drive"');
    expect(markup).toContain("Propose next moves");
    expect(markup).toContain("proposal-only");
    expect(markup).toContain("Loading captain session");
    expect(markup).not.toMatch(/applied automatically|silently (apply|change)/i);
    expect(islandSource).toContain('data-testid="metatron-captain"');
  });

  it("shows honest unavailable copy and does not invent a mock success", () => {
    const markup = html(
      React.createElement(MetatronCaptainView, {
        projectId: "boss-dogfood",
        actor: "ceo",
        state: unavailableMetatronCaptain(CAPTAIN_UNAVAILABLE_404),
        loading: false,
        error: CAPTAIN_UNAVAILABLE_404,
        pending: false,
        onSend: noop,
        onAddGoal: noop,
        onWatch: noop,
        onDrive: noop,
      }),
    );

    expect(markup).toContain('data-testid="metatron-captain-unavailable"');
    expect(markup).toContain('data-empty-state="api-unavailable"');
    expect(markup).toContain(CAPTAIN_UNAVAILABLE_404);
    expect(markup).toContain("No session, goals, or watch were invented");
    expect(markup).not.toContain('data-testid="metatron-captain-empty-chat"');
    expect(markup).not.toMatch(/session ready|captain is online|drive applied/i);
    expect(describeMetatronCaptainUnavailable(404)).toBe(CAPTAIN_UNAVAILABLE_404);

    const state = unavailableMetatronCaptain(
      describeMetatronCaptainUnavailable(404),
    );
    expect(state.apiReachable).toBe(false);
    expect(state.messages).toEqual([]);
    expect(state.goals).toEqual([]);
    expect(state.watch).toBeNull();
  });

  it("shows empty chat/goals/watch only when the captain API is reachable", () => {
    const markup = html(
      React.createElement(MetatronCaptainView, {
        projectId: "boss-dogfood",
        actor: "ceo",
        state: mapMetatronCaptainSession({
          thread_id: "ath_captain",
          messages: [],
          memories: [],
          goals: [],
          watch: null,
        }),
        loading: false,
        error: null,
        pending: false,
        onSend: noop,
        onAddGoal: noop,
        onWatch: noop,
        onDrive: noop,
      }),
    );

    expect(markup).toContain('data-testid="metatron-captain-empty-chat"');
    expect(markup).toContain('data-testid="metatron-captain-empty-goals"');
    expect(markup).toContain('data-testid="metatron-captain-empty-watch"');
    expect(markup).not.toContain('data-testid="metatron-captain-unavailable"');
  });

  it("never claims a loopback operator URL", () => {
    expect(isLoopbackOperatorUrl("http://127.0.0.1:8000")).toBe(true);
    expect(isLoopbackOperatorUrl("http://localhost:3000/captain")).toBe(true);
    expect(
      captainOperatorHref("http://127.0.0.1:8000/v1/metatron/session"),
    ).toBeNull();
    expect(captainOperatorHref("https://boss.example/captain")).toBe(
      "https://boss.example/captain",
    );

    const markup = html(
      React.createElement(MetatronCaptain, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).not.toMatch(/href="https?:\/\/(127\.0\.0\.1|localhost)/);
    expect(markup).not.toContain("View captain at http://127");
    expect(islandSource).not.toContain("127.0.0.1");
    expect(islandSource).not.toContain("localhost");
    expect(actionsSource).not.toContain("127.0.0.1");
    expect(actionsSource).not.toContain("localhost");
    expect(actionsSource).toContain('"/v1/metatron/session"');
    expect(actionsSource).toContain('"/v1/metatron/goals"');
    expect(actionsSource).toContain('"/v1/metatron/watch"');
    expect(actionsSource).toContain('"/v1/metatron/drive"');
  });
});

describe("Metatron Captain actions", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("posts chat turns to /v1/metatron/session and treats 404 as unavailable", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Not Found" }),
    });

    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("message", "watch");

    const result = await sendMetatronCaptainMessageAction(form);
    expect(result.ok).toBe(false);
    expect(result.unavailable).toBe(true);
    expect(result.error).toBe(CAPTAIN_UNAVAILABLE_404);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "https://boss-api.test/v1/metatron/session",
    );
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).method).toBe("POST");
    expect(
      JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body)),
    ).toEqual({
      project_id: "boss-dogfood",
      actor: "ceo",
      message: "watch",
    });
    expect(String(fetchMock.mock.calls[0]?.[0])).not.toContain("127.0.0.1");
  });

  it("posts drive as proposal-only (auto: false)", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        status: "proposed",
        auto: false,
        applied: [],
        moves: [],
        note: "Captain proposes only.",
      }),
    });

    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");

    const result = await proposeMetatronCaptainDriveAction(form);
    expect(result.ok).toBe(true);
    expect(
      JSON.parse(String((fetchMock.mock.calls.at(-1)?.[1] as RequestInit).body)),
    ).toEqual({
      project_id: "boss-dogfood",
      actor: "ceo",
      auto: false,
    });
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toBe(
      "https://boss-api.test/v1/metatron/drive",
    );
  });
});
