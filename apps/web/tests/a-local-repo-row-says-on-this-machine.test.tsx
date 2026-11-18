// @vitest-environment happy-dom
/**
 * A local repository's row in Setup says "On this machine" and none of GitHub's facts
 * (stranger test r2, 2026-11-17: it read "Private · Pull requests ✓" for code on the box).
 * Breaks if a local row shows Private, the Read / Write / Pull requests checks or Replace token,
 * or a GitHub row loses them.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SetupView } from "../lib/setup-types";
import { fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

async function mount(view: SetupView): Promise<Document> {
  mounted = await mountBoard(view);
  return document;
}

function withRepo(fullName: string): SetupView {
  const view = board();
  return { ...view, repository: { ...view.repository, repos: view.repository.repos.map((r) => ({ ...r, full_name: fullName })) } };
}

describe("a local repo row", () => {
  it("says On this machine, without GitHub's facts", async () => {
    const name = "local_a-tiny-to-do-list-web-app/a-tiny-to-do-list-web-app";
    fakeApi(withRepo(name));
    const c = await mount(withRepo(name));
    const row = c.querySelector(`[data-testid='su-repo-${name}']`)!;
    expect(row).not.toBeNull();
    expect(row.querySelector("[data-testid='su-repo-on-machine']")?.textContent).toBe("On this machine");
    expect(row.textContent).not.toContain("Private");
    expect(row.querySelector("[data-testid='su-check-pull_requests']")).toBeNull();
  });

  it("a GitHub repo keeps Private and its checks", async () => {
    fakeApi(withRepo("owner/app"));
    const c = await mount(withRepo("owner/app"));
    const row = c.querySelector("[data-testid='su-repo-owner/app']")!;
    expect(row.textContent).toContain("Private");
    expect(row.querySelector("[data-testid='su-check-pull_requests']")).not.toBeNull();
    expect(row.querySelector("[data-testid='su-repo-on-machine']")).toBeNull();
  });
});
