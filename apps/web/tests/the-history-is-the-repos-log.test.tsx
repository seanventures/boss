// @vitest-environment happy-dom
/**
 * GIT HISTORY IN THE CONSOLE (BOSSAPP-993). The operator: "introduce the git logs in the ui …
 * provide few important git commands in the ui … showing history etc.. we dont have to go to CLI".
 *
 * The History card (Repo & knowledge, named by the project header's History chip) shows the
 * default branch's log the same way for a LOCAL repository and a GitHub one: subject (a link to
 * the commit's files and diff), author, when, the change or PR that produced it, the short sha.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { RepoHistory } from "../app/projects/repo-history";
import { $$, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => unmount());

const item = (sha: string, over: Record<string, unknown> = {}) => ({
  sha: sha.repeat(40).slice(0, 40), parents: ["b".repeat(40)], merge: false,
  author: "BOSS", date: "2026-11-10T12:00:00Z", message: `commit ${sha}`, change: null, ...over,
});

const history = (source: "local" | "github", repository: string) => ({
  repository, source, branch: "main",
  items: [
    item("a", { merge: true, change: { source: source === "github" ? "github" : "local", number: 3,
                                       href: source === "github" ? "/changes/p1/3?repo=acme/web" : "/changes/p1/3" } }),
    item("c"),
  ],
});

describe("the History card is the repo's log, whichever host holds the code", () => {
  for (const [source, repository] of [["local", "local_p1/helpdesk"], ["github", "acme/web"]] as const) {
    it(`a ${source} repository: subject, author, when, its change, the short sha`, async () => {
      stubFetch((c) => (c.path === "/v1/projects/p1/history" ? { body: history(source, repository) } : undefined));
      await mountAs(<RepoHistory projectId="p1" />);
      const rows = $$("[data-testid='px-history-commit']");
      expect(rows.length).toBe(2);
      const first = rows[0];
      const link = first.querySelector("[data-testid='px-history-link']") as HTMLAnchorElement;
      expect(link.textContent).toBe("commit a");
      // The commit page holds the files and diff; a GitHub repo's link names the repo.
      expect(link.getAttribute("href")).toBe(source === "github"
        ? `/projects/p1/commits/${"a".repeat(40)}?repo=acme%2Fweb` : `/projects/p1/commits/${"a".repeat(40)}`);
      expect(first.querySelector("[data-testid='px-history-author']")?.textContent).toBe("BOSS");
      expect(first.querySelector("[data-testid='px-history-when']")?.textContent?.length).toBeGreaterThan(0);
      expect(first.querySelector("[data-testid='px-history-change']")?.textContent)
        .toBe(source === "github" ? "PR #3" : "Change #3");
      expect(first.querySelector("code")?.textContent).toBe("a".repeat(7));
      expect(rows[1].querySelector("[data-testid='px-history-change']")).toBeNull();
      unmount();
    });
  }

  it("says when there is nothing yet", async () => {
    stubFetch((c) => (c.path === "/v1/projects/p1/history"
      ? { body: { repository: "local_p1/helpdesk", source: "local", branch: "main", items: [] } } : undefined));
    await mountAs(<RepoHistory projectId="p1" />);
    expect(tid("px-history")!.textContent).toContain("No commits yet");
  });
});
