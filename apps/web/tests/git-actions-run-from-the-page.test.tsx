// @vitest-environment happy-dom
/**
 * GIT ACTIONS IN THE UI (BOSSAPP-996). The operator: "provide few important git commands in the
 * ui, to pull, resolve, showing history etc.. we dont have to go to CLI to execute these things."
 *
 * The change header's sync line: where the branch stands, Sync (pull the base in), Restack
 * (after a squash merge — PR #31's machinery, offered FIRST), the conflicting files named behind
 * an expander, and History. Every action posts to …/sync, which records a decision — never
 * ambient git.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { SyncPanel } from "../app/changes/[projectId]/[number]/sync-panel";
import { parseChangeSet } from "../lib/repo-links";
import { $, click, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => unmount());

const CS = (state = "open") => parseChangeSet({
  number: 3, state, repository: "local_p1/helpdesk", branch: "boss/code/crun_1", base_branch: "main",
  url: "http://boss-local/local_p1/helpdesk/pull/3", source: "local", head_sha: "a".repeat(40),
});

const preview = (over: Record<string, unknown> = {}) => ({
  number: 3, source: "local", branch: "boss/code/crun_1", base: "main", state: "open",
  ahead: 2, behind: 2, mergeable: true, conflicts: [], conflicts_known: true, restack: null, ...over,
});

const mountPanel = (p: Record<string, unknown>, onPost?: (c: Call) => void) => {
  const calls = stubFetch((c) => {
    if (c.path.endsWith("/sync-preview")) return { body: p };
    if (c.path.endsWith("/sync") && c.method === "POST") { onPost?.(c); return { body: { status: "approved" } }; }
    if (c.path.endsWith("/local-changes/3")) return { body: { number: 3, state: "open" } };
    return undefined;
  });
  return { calls, mounted: mountAs(<SyncPanel cs={CS()} at={{ projectId: "p1", number: 3 }} reread={async () => null} />) };
};

describe("the sync line", () => {
  it("says where the branch stands and Sync posts the governed action", async () => {
    const posts: Call[] = [];
    const { mounted } = mountPanel(preview(), (c) => posts.push(c));
    await mounted;
    expect(tid("gr-sync-stand")!.textContent).toContain("2 ahead");
    expect(tid("gr-sync-stand")!.textContent).toContain("2 behind");
    await click(tid("gr-sync-run"));
    expect(posts.length).toBe(1);
    expect(posts[0].body).toEqual({ mode: "update" });
    expect(posts[0].path).toBe("/v1/projects/p1/local-changes/3/sync");
    unmount();
  });

  it("names the conflicting files behind an expander, and the honest next step", async () => {
    const { mounted } = mountPanel(preview({ mergeable: false, conflicts: ["src/app.js", "lib/db.py"] }));
    await mounted;
    expect(tid("gr-conflicts")!.textContent).toContain("2 conflicting files");
    expect(tid("gr-conflicts-list")).toBeNull();            // the list waits behind the expander
    await click(tid("gr-conflicts"));
    const list = tid("gr-conflicts-list")!;
    expect(list.textContent).toContain("src/app.js");
    expect(list.textContent).toContain("lib/db.py");
    expect(list.textContent).toContain("Request changes"); // git alone cannot fix these
    unmount();
  });

  it("offers Restack first after a squash merge, and posts mode=restack", async () => {
    const posts: Call[] = [];
    const { mounted } = mountPanel(
      preview({ mergeable: false, conflicts: ["src/app.js"], restack: { number: 2, merged_head_sha: "b".repeat(40) } }),
      (c) => posts.push(c));
    await mounted;
    const restack = tid("gr-restack")!;
    expect(restack.textContent).toContain("Restack onto main");
    expect(restack.getAttribute("title")).toContain("#2");
    await click(restack);
    expect(posts[0].body).toEqual({ mode: "restack" });
    unmount();
  });

  it("a GitHub change says the files are not named, rather than inventing a list", async () => {
    const { mounted } = mountPanel(preview({ source: "github", mergeable: false, conflicts: [], conflicts_known: false }));
    await mounted;
    expect(tid("gr-conflicts")!.textContent).toContain("Conflicts with base");
    await click(tid("gr-conflicts"));
    expect(tid("gr-conflicts-list")!.textContent).toContain("does not name the files");
    unmount();
  });

  it("links History, and renders nothing for a change that is not open", async () => {
    const { mounted } = mountPanel(preview());
    await mounted;
    expect(tid("gr-history-link")!.getAttribute("href")).toBe("/?project_id=p1&area=project&tab=knowledge");
    unmount();
    stubFetch(() => undefined);
    await mountAs(<SyncPanel cs={CS("merged")} at={{ projectId: "p1", number: 3 }} reread={async () => null} />);
    expect(tid("gr-sync")).toBeNull();
    expect($("[data-testid='gr-sync-run']")).toBeNull();
  });
});
