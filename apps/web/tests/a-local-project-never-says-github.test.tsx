// @vitest-environment happy-dom
/**
 * A LOCAL PROJECT NEVER SAYS GITHUB (BOSSAPP-946, finding 8).
 *
 * The local-first HelpDesk's pages said "PR #1", "Merge PR #1", "Pull request: Held until the gates
 * pass" and "Idea to PR" for a project with no GitHub anywhere. Every place draws a change through
 * lib/repo-links: "Change #1", "Merge change #1", "Change set", "Idea to change set".
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { DecisionActs, actWord } from "../app/projects/drawers-more";
import { StartedCards } from "../app/projects/started-cards";
import type { PxCtx } from "../app/projects/project-page";
import { outputLabel } from "../app/factory/outputs";
import { missingWord } from "../lib/build-detail";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { parseOverview, type NeedsYou } from "../lib/project-experience";
import { changeRef, changeWords, projectIsLocal } from "../lib/repo-links";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

const LOCAL = "http://boss-local/local_hd/hd/pull/1";
const GITHUB_WORD = /\bPRs?\b|pull request|GitHub/i;

afterEach(() => unmount());

describe("the words for a change", () => {
  it("say change for a local change set and PR on GitHub", () => {
    expect(changeRef(LOCAL, 1)).toBe("Change #1");
    expect(changeRef(LOCAL, 1, true)).toBe("change #1");
    expect(changeRef("https://github.com/a/b/pull/3", 3)).toBe("PR #3");
    expect(projectIsLocal([{ full_name: "local_hd/hd" }])).toBe(true);
    expect(projectIsLocal([{ full_name: "acme/web" }])).toBe(false);
    for (const w of Object.values(changeWords(true))) expect(w).not.toMatch(GITHUB_WORD);
  });

  it("the build's Outputs row is the Change set, held until the gates pass", () => {
    const c = { brief: { repo: "local_hd/hd" }, outputs: { pr: null } } as never;
    expect(outputLabel("pr", c)).toBe("Change set");
    const held = missingWord("pr", { live: true, state: { state: "running" }, summary: null, local: true,
                                     outputs: { pr: null, tests: null, changes: null, preview: null, cost: { word: "" } } } as never);
    expect(held?.tip).toContain("change set");
    expect(held?.tip).not.toMatch(GITHUB_WORD);
  });
});

describe("the project page of a local project", () => {
  const ctx = { projectId: "hd", work: null, open: vi.fn(), tab: vi.fn(), refreshWork: vi.fn(),
                overview: { ...parseOverview(FX_OVERVIEW), project: { ...parseOverview(FX_OVERVIEW).project, repos: [{ full_name: "local_hd/hd", branch: "main", access: { read: true, write: true, pr: true }, external_url: "" }] } },
              } as unknown as PxCtx;

  it("the drawer merges change #1", async () => {
    const n: NeedsYou = parseOverview({ ...FX_OVERVIEW, needs_you: [{
      id: "dec_m1", kind: "merge", label: "Merge change #1", detail: "Approve code change: Help desk (change #1)",
      external_url: LOCAL, mini: [], action: { label: "Review change", href: "?drawer=needs:dec_m1", decision_id: "dec_m1" } }] }).needs_you[0];
    expect(actWord(n)).toBe("Merge change #1");
    stubFetch(() => undefined);
    const host = await mountAs(<DecisionActs ctx={ctx} n={n} />);
    expect(tid("px-drawer-needs-approve")?.textContent).toContain("Merge change #1");
    expect(host.textContent ?? "").not.toMatch(GITHUB_WORD);
  });

  it("Idea to change set, never Idea to PR", async () => {
    stubFetch((c) => (c.method === "GET" ? { body: {
      seed: { kind: "idea", words: "a desk", brief: {}, picks: { repos: ["local_hd/hd"] }, created_at: null },
      run: { id: "wfr_1", status: "running" }, nodes: [{ key: "pr", label: "PR", type: "todo", state: "todo" }],
      now: null, workflow: null, prd: { state: "drafting", decision_id: null } } } : undefined));
    await mountAs(<StartedCards ctx={ctx} />);
    expect(tid("px-started")?.querySelector("h2")?.textContent).toBe("Idea to change set");
  });
});
