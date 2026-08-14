import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

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

const { AgentProcesses } = await import("../app/agent-process-ui");

const pageSource = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const actionsSource = readFileSync(
  resolve(__dirname, "../app/agent-process-actions.ts"),
  "utf8",
);

describe("Agent process island", () => {
  it("mounts on data-area=processes", () => {
    expect(pageSource).toContain('import { AgentProcesses } from "./agent-process-ui"');
    expect(pageSource).toContain('data-area="processes"');
    expect(pageSource).toContain("<AgentProcesses");
  });

  it("renders the PRD start form and handoff copy", () => {
    const markup = renderToStaticMarkup(
      React.createElement(AgentProcesses, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).toContain('data-testid="agent-processes"');
    expect(markup).toContain('data-testid="agent-process-start"');
    expect(markup).toContain("Start PRD process");
    expect(markup).toContain("Architect");
    expect(markup).toContain("QA");
    expect(markup).toContain("certified");
    expect(markup).not.toMatch(/silently (apply|change)/i);
    expect(actionsSource).toContain('"/v1/agent-processes/runs"');
  });
});
