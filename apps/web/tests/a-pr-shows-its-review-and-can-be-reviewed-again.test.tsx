// @vitest-environment happy-dom
/**
 * The Pull request output says whether the agents' review is ON the PR, says in words when it
 * could not be posted (a token without "Pull requests: write"), and offers "Review this PR again"
 * — the adversarial review on the PR's real diff, posted as a new round. PR #223 had
 * "Conversation 0" on GitHub and nothing on the build page said so.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseCockpit } from "../lib/cockpit";
import { $, click, mount, unmount } from "./_helpers/live-render";
import { posts, stubFetch } from "./_helpers/cockpit-render";
import { cockpitPrOpen } from "./_helpers/factory-fixtures";

vi.mock("../app/factory/legacy-build-parts", async (orig) => ({ ...(await orig<object>()), DeploymentLine: () => <span data-testid="deploy-line" /> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Outputs } = await import("../app/factory/outputs");
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

type W = Record<string, unknown>;
function withReview(review: W): W {
  const base = cockpitPrOpen();
  const outputs = base.outputs as W;
  return { ...base, outputs: { ...outputs, pr: { ...(outputs.pr as W), review } } };
}
const view = (w: W) => <Outputs projectId="p1" actor="ceo" cockpit={parseCockpit(w)} open="pr" onOpen={() => {}} onLogs={() => {}} onRefresh={() => {}} />;

describe("The pull request's review", () => {
  it("links the posted review and offers to review the PR again", async () => {
    const w = withReview({ word: "2 review rounds on the PR", url: "https://github.com/acme/app/pull/842#pullrequestreview-1",
                           permission: false, rounds: 2 });
    stubFetch(w);
    await mount(view(w));
    expect($('[data-testid="fx-pr-review-word"]')?.textContent).toBe("2 review rounds on the PR");
    expect($('[data-testid="fx-pr-review-open"]')?.getAttribute("href")).toContain("pullrequestreview-1");
    const again = $('[data-testid="fx-pr-review-again"]') as HTMLButtonElement;
    expect(again.textContent).toBe("Review this PR again");
    await click(again);
    expect(posts.some((p) => p.url.endsWith("/v1/code-tasks/crun_pr/review-pr-again") && p.body.actor === "ceo")).toBe(true);
  });

  it("says in words when the connection cannot write reviews", async () => {
    const w = withReview({ word: "Review not posted", permission: true, rounds: 1,
                           reason: "BOSS's GitHub connection cannot write pull request reviews on acme/app (GitHub answered 403). Give its token \"Pull requests: Read and write\" access" });
    stubFetch(w);
    await mount(view(w));
    expect($('[data-testid="fx-pr-review-reason"]')?.textContent).toContain("Pull requests: Read and write");
  });

  it("shows a review in flight and does not offer a second one", async () => {
    const w = withReview({ word: "Reviewing again…", again: "running", permission: false, rounds: 1 });
    stubFetch(w);
    await mount(view(w));
    const again = $('[data-testid="fx-pr-review-again"]') as HTMLButtonElement;
    expect(again.disabled).toBe(true);
    expect(again.textContent).toBe("Reviewing…");
  });
});
