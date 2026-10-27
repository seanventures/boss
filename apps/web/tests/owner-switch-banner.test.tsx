// @vitest-environment happy-dom
/**
 * Build L: the banner that tells an operator their console just switched to a deep-linked
 * object's own project, and stamps project_id onto the URL (via history.replaceState, no new
 * history entry) so every later poll and in-console link keeps it.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { OwnerSwitchBanner } from "../app/owner-switch-banner";
import { $, cleanup, click, render } from "./_helpers/kit-render";

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

describe("OwnerSwitchBanner", () => {
  it("rewrites the URL to carry the resolved project_id, without adding a history entry", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks&build=crun_1");
    await render(<OwnerSwitchBanner projectId="proj_owner" projectName="Owner Project" objectType="build" />);
    expect(window.location.search).toContain("project_id=proj_owner");
    expect(window.location.search).toContain("build=crun_1");
  });

  it("leaves the URL alone once it already carries the resolved project_id", async () => {
    window.history.replaceState(null, "", "/?area=code_tasks&build=crun_1&project_id=proj_owner");
    const before = window.location.search;
    await render(<OwnerSwitchBanner projectId="proj_owner" projectName="Owner Project" objectType="build" />);
    expect(window.location.search).toBe(before);
  });

  it("names the project and the kind of object in its copy", async () => {
    window.history.replaceState(null, "", "/?meeting=mtg_1");
    await render(<OwnerSwitchBanner projectId="proj_owner" projectName="Owner Project" objectType="meeting" />);
    const banner = $('[data-testid="owner-switch-banner"]');
    expect(banner?.textContent).toContain("Owner Project");
    expect(banner?.textContent).toContain("meeting");
  });

  it("falls back to naming the object kind when no project name is known", async () => {
    window.history.replaceState(null, "", "/?self_fix=sfx_1");
    await render(<OwnerSwitchBanner projectId="proj_owner" projectName={null} objectType="self_fix" />);
    expect($('[data-testid="owner-switch-banner"]')?.textContent).toContain("self-fix’s project");
  });

  it("dismiss hides the banner", async () => {
    window.history.replaceState(null, "", "/?build=crun_1");
    await render(<OwnerSwitchBanner projectId="proj_owner" projectName="Owner Project" objectType="build" />);
    expect($('[data-testid="owner-switch-banner"]')).not.toBeNull();
    await click('[data-testid="owner-switch-banner"] button');
    expect($('[data-testid="owner-switch-banner"]')).toBeNull();
  });
});
