// @vitest-environment happy-dom
/**
 * Setup › Repos, collapsed ("done") row: with several bound repositories, every one of them
 * must be listed with its own menu (Test / Replace token / Make default / Remove), not just the
 * default repo collapsed to a single line. A single-repo project keeps rendering exactly as it
 * does today — no account header, no extra chrome (FR1.1/FR1.2/FR1.6).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";
import type { SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

describe("Setup › Repos collapsed row", () => {
  it("P1: lists every bound repo with its own Test/Replace/Make default/Remove menu", async () => {
    const view = board();
    const [defaultRepo] = view.repository.repos;
    const second = {
      ...defaultRepo, binding_id: "ghb2", full_name: "acme/second", default: false,
    };
    const twoRepos: SetupView = {
      ...view, repository: { ...view.repository, repos: [defaultRepo, second] },
    };
    fakeApi(twoRepos);
    mounted = await mountBoard(twoRepos);

    expect(document.querySelector(`[data-testid="su-repo-${defaultRepo.full_name}"]`)).not.toBeNull();
    expect(document.querySelector('[data-testid="su-repo-acme/second"]')).not.toBeNull();

    const secondMenu = document.querySelector('[data-testid="su-repo-acme/second"] [data-testid="su-repo-menu"]');
    expect(secondMenu).not.toBeNull();
    expect(document.querySelector('[data-testid="su-add-repo"]')).not.toBeNull();
  });
});
