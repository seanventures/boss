// @vitest-environment happy-dom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { emptyFloor } from "./_helpers/factory-fixtures";
import { $, mount, rerender, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { CodeTasksHome } = await import("../app/code-tasks-ui");
type Binding = Pick<React.ComponentProps<typeof CodeTasksHome>, "githubPlane" | "githubPlaneLabel" | "githubFullName">;
const home = (binding: Binding = {}) => (
  <CodeTasksHome projectId="p1" codeTasks={[]} apiReachable actor="ceo" {...binding} />
);
const header = () => $("[data-testid=fx-github-plane]")!;

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=code_tasks");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    expect(String(input)).toMatch(/^\/v1\/factory\/floor\?/);
    return Response.json(emptyFloor());
  }));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("Factory repository binding honesty", () => {
  it.each([null, "", "  ", "/", " / ", "acme/", "/sandbox", "acme", "acme/repo/extra"])(
    "does not present incomplete settings repository %j as a PR destination",
    async (githubFullName) => {
      await mount(home({ githubPlane: "legacy_env", githubFullName }));
      expect(header().textContent).toBe("GitHub · Repository unknown");
      expect(header().title).toContain("No complete repository is reported.");
      expect(header().title).toContain("settings");
      expect(header().title).toContain("Check GitHub access in Connect");
      expect(header().title).not.toMatch(/PRs open|settings token|connected|ready/i);
    },
  );

  it("reports missing binding metadata as unknown, without inventing a settings source", async () => {
    await mount(home());
    expect(header().textContent).toBe("GitHub · Repository unknown");
    expect(header().title).toContain("GitHub binding source is unknown.");
    expect(header().title).not.toContain("settings");
  });

  it.each([
    ["mission_control_binding", "Project GitHub binding for Acme-Team/Widget_UI.v2."],
    ["legacy_env", "Repository from settings: Acme-Team/Widget_UI.v2."],
  ])("retains exact repository spelling and the %s source without promising access", async (githubPlane, tip) => {
    await mount(home({ githubPlane, githubFullName: "Acme-Team/Widget_UI.v2" }));
    expect(header().textContent).toBe("GitHub · Acme-Team/Widget_UI.v2");
    expect(header().querySelector("[data-user-text]")?.textContent).toBe("Acme-Team/Widget_UI.v2");
    expect(header().title).toBe(`${tip} Check GitHub access in Connect before starting a build.`);
  });

  it("keeps the reported reason and repository when the binding source is unknown", async () => {
    await mount(home({ githubPlane: "future_binding", githubPlaneLabel: "Binding lookup unavailable.", githubFullName: "acme/sandbox" }));
    expect(header().textContent).toBe("GitHub · acme/sandbox");
    expect(header().title).toContain("GitHub binding source is unknown.");
    expect(header().title).toContain("Binding lookup unavailable.");
    expect(header().title).not.toMatch(/PRs open|settings token|ready/i);
  });

  it("removes a stale repository when refreshed binding metadata becomes incomplete", async () => {
    await mount(home({ githubPlane: "mission_control_binding", githubFullName: "acme/sandbox" }));
    await rerender(home({ githubPlane: "mission_control_binding", githubFullName: "/", githubPlaneLabel: "Choose a repository." }));
    expect(header().textContent).toBe("GitHub · Repository unknown");
    expect(header().title).toContain("Project GitHub binding.");
    expect(header().title).toContain("Choose a repository.");
    expect(header().title).not.toContain("acme/sandbox");
  });
});
