import React from "react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return {
    ...actual,
    useActionState: () => [null, vi.fn(), false],
  };
});

vi.mock("../app/submit-button", () => ({
  SubmitButton: (
    props: React.ButtonHTMLAttributes<HTMLButtonElement> & { children?: React.ReactNode },
  ) =>
    React.createElement(
      "button",
      { type: "submit", className: props.className },
      props.children,
    ),
}));

const { MeetingConsensusCard } = await import("../app/phase3-ui");

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

describe("typed standup meeting UI", () => {
  it("convene form offers prd_panel and standup types", () => {
    const source = readFileSync(join(ROOT, "app/meeting-room-ui.tsx"), "utf8");
    expect(source).toContain('name="meeting_type"');
    expect(source).toContain('value="prd_panel"');
    expect(source).toContain('value="standup"');
    expect(source).toContain("no-spend fixture");
    const actions = readFileSync(join(ROOT, "app/actions.ts"), "utf8");
    expect(actions).toContain("/v1/meetings/standup");
  });

  it("hides the orphan tally behind requires meeting_id", () => {
    const html = renderToStaticMarkup(
      React.createElement(MeetingConsensusCard, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(html).toContain("requires meeting_id");
    expect(html).toContain('name="meeting_id"');
  });

  it("binds tally to a real meeting_id when provided", () => {
    const html = renderToStaticMarkup(
      React.createElement(MeetingConsensusCard, {
        projectId: "boss-dogfood",
        actor: "ceo",
        meetingId: "mtg_standup_1",
      }),
    );
    expect(html).toContain("mtg_standup_1");
    expect(html).not.toContain("requires meeting_id");
  });
});
