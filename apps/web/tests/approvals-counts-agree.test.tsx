/**
 * Approvals' counts agree with each other and with the rail (stranger test r2, 2026-11-17).
 *
 * The rail said "+4" and the bell "4" while Approvals said "Queue is clear 0" with no pointer
 * to the other projects, and the tabs read "Needs action 2" beside "All 0" (Needs action counted
 * the project's setup items, All did not).
 *
 * Breaks if the page stops naming and linking the other projects that have items waiting, counts
 * an archived project or this one, or if All counts fewer than Needs action.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ProjectCard, ProjectsSummary } from "../lib/project-experience";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", { ...props, type: props.type ?? "submit" }, props.children),
}));

const card = (id: string, name: string, needs: number, archived = false) =>
  ({ id, name, needs_you: needs, archived } as unknown as ProjectCard);
const SUMMARY: ProjectsSummary = {
  week_cap_usd: null,
  items: [card("boss-dogfood", "Default", 0), card("hello-world", "Hello World", 2),
          card("todo", "A tiny to-do", 2), card("old", "Old", 5, true)],
};

vi.mock("../lib/agents-live", () => ({
  useAttention: () => ({ count: 2, setup: 2, items: [
    { id: "s1", kind: "setup", title: "Add a deploy host" }, { id: "s2", kind: "setup", title: "Add a model" }] }),
}));
vi.mock("../lib/project-experience", async (orig) => {
  const real = await orig<typeof import("../lib/project-experience")>();
  return { ...real, usePxRead: () => ({ state: "ready", data: SUMMARY }) };
});

const { DecisionInbox, WaitingElsewhere, waitingElsewhere } = await import("../app/decision-inbox-ui");

describe("waiting in other projects", () => {
  it("names and links each other project with items waiting, never this one or an archived one", () => {
    const w = waitingElsewhere(SUMMARY, "boss-dogfood", "org_1");
    expect(w.total).toBe(4);
    expect(w.projects.map((p) => p.id)).toEqual(["hello-world", "todo"]);
    expect(w.projects[0].href).toContain("area=decisions");
    expect(w.projects[0].href).toContain("project_id=hello-world");
    expect(waitingElsewhere(SUMMARY, "hello-world").total).toBe(2);
    expect(waitingElsewhere(null, "x").total).toBe(0);

    const markup = renderToStaticMarkup(<WaitingElsewhere waiting={w} />);
    expect(markup).toContain("4 waiting in other projects:");
    expect(markup).toContain("Hello World (2)");
    expect(markup).toContain("A tiny to-do (2)");
    expect(renderToStaticMarkup(<WaitingElsewhere waiting={{ total: 0, projects: [] }} />)).toBe("");
  });

  it("the page shows the pointer, and All never counts fewer than Needs action", () => {
    const markup = renderToStaticMarkup(
      <DecisionInbox feed={{ projectId: "boss-dogfood", apiReachable: true, items: [] }}
                     tasks={[]} roles={[]} projectId="boss-dogfood" actorId="ceo" orgId="org_1" />,
    );
    expect(markup).toContain('data-testid="decision-inbox-elsewhere"');
    expect(markup).toContain("4 waiting in other projects");
    const count = (tab: string) => {
      const m = markup.match(new RegExp(`data-testid="decision-filter-${tab}"[^>]*>[^<]*<span class="dq-filter-count">(\\d+)</span>`));
      return m ? Number(m[1]) : NaN;
    };
    expect(count("needs-action")).toBe(2);
    expect(count("all")).toBe(2);
  });
});
