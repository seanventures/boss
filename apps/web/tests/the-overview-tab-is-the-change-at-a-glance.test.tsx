// @vitest-environment happy-dom
/**
 * THE OVERVIEW TAB (BOSSAPP-995). The operator: "introduce the overview tab, where explaining
 * about the PR description (introduce good formatting) and other active and resolved comments
 * for people to directly work on it, we should also provide link to jump to specific code piece."
 *
 *   - the description is rendered WHOLE as Markdown: headings, lists, code, tables — never the
 *     Conversation tab's three-line clamp;
 *   - comments are grouped Active (open · addressed · disputed) and Resolved (an expander,
 *     closed by arrival);
 *   - a thread's file:line and a panel note's file:line are links that jump to the exact row of
 *     the diff (lineAnchor gives every diff row an id), and the URL can say the same place.
 */
import React from "react";
import { describe, expect, it } from "vitest";

import { OverviewTab } from "../app/changes/[projectId]/[number]/overview-tab";
import { DiffTable, lineAnchor } from "../app/changes/[projectId]/[number]/review-panes";
import { parseChangeSet } from "../lib/repo-links";
import { parseUnifiedDiff } from "../lib/diff-view";
import type { Thread } from "../lib/local-review-data";
import { $, $$, click, mountAs, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const BODY = [
  "## Why", "A table and code, rendered, never raw markdown:", "",
  "| gate | result |", "| --- | --- |", "| tests | 148 passed |", "",
  "```js", "tick();", "```", "- one", "- two",
].join("\n");

const thread = (id: number, state: string, line: number) => ({
  id, path: "src/app.js", line, side: "new", body: `T${id}`, author: "ravi", author_kind: "human",
  state, severity: "major", origin: "console", outdated: false, replies: [],
});

const CS = parseChangeSet({
  number: 3, title: "Ticket SLA timers", state: "open", repository: "local_p1/helpdesk",
  branch: "boss/code/crun_1", base_branch: "main", url: "http://boss-local/local_p1/helpdesk/pull/3",
  body: BODY, source: "local",
  files: [{ path: "src/app.js", status: "modified", additions: 2, deletions: 1 }],
  commits: [{ sha: "a".repeat(40), message: "tick" }],
  comments: [thread(1, "open", 39), thread(2, "addressed", 41), thread(3, "resolved", 44)],
  review: [{ id: 9, kind: "review", event: "COMMENT", body: "One nit.", author: "vera",
             comments: [{ path: "src/app.js", line: 41, body: "Name the constant." }] }],
});

const mountTab = (onJump: (t: Thread) => void = () => undefined, onJumpLoc: (p: string, l?: number | null) => void = () => undefined) =>
  mountAs(<OverviewTab cs={CS} at={{ projectId: "p1", number: 3 }} files={CS.files} onChanged={() => undefined}
                       onJump={onJump} onJumpLoc={onJumpLoc} onFiles={() => undefined} />);

describe("the Overview tab", () => {
  it("renders the whole description as formatted markdown, unclamped", async () => {
    await mountTab();
    const d = tid("gr-ov-description")!;
    expect(d.querySelector(".md-table")).toBeTruthy();          // the table is a table
    expect(d.querySelector(".md-pre")).toBeTruthy();            // the code block is code
    expect(d.querySelector(".md-h")?.textContent).toBe("Why");  // the heading is a heading
    expect(d.textContent).not.toContain("| gate |");            // never raw markdown
    expect(d.querySelector("[data-clamp3]")).toBeNull();        // never the 3-line clamp
    unmount();
  });

  it("groups Active (open · addressed · disputed) apart from Resolved, which starts closed", async () => {
    await mountTab();
    const active = $$("[data-testid='gr-ov-active'] [data-testid='lr-thread']");
    expect(active.map((t) => t.getAttribute("data-thread"))).toEqual(["1", "2"]);
    const resolved = tid("gr-ov-resolved") as HTMLDetailsElement;
    expect(resolved.open).toBe(false);
    expect(resolved.querySelectorAll("[data-testid='lr-thread']").length).toBe(1);
    expect(resolved.textContent).toContain("Resolved");
    unmount();
  });

  it("a thread's file:line opens the diff there, a panel note's file:line jumps by place", async () => {
    const jumped: number[] = [];
    const locs: [string, number | null | undefined][] = [];
    await mountTab((t) => jumped.push(t.id), (p, l) => locs.push([p, l]));
    await click($("[data-testid='gr-ov-active'] [data-testid='gr-comment-anchor']"));
    expect(jumped).toEqual([1]);
    expect(tid("gr-ov-note")!.textContent).toContain("Name the constant.");
    await click(tid("gr-ov-note-loc"));
    expect(locs).toEqual([["src/app.js", 41]]);
    unmount();
  });

  it("says when nothing waits on anyone, and hides an empty Resolved group", async () => {
    const quiet = parseChangeSet({ number: 4, repository: "local_p1/helpdesk", state: "open",
                                   url: "http://boss-local/local_p1/helpdesk/pull/4", source: "local" });
    await mountAs(<OverviewTab cs={quiet} at={{ projectId: "p1", number: 4 }} files={[]} onChanged={() => undefined}
                               onJump={() => undefined} onJumpLoc={() => undefined} onFiles={() => undefined} />);
    expect(tid("gr-ov-active")!.textContent).toContain("every comment is resolved");
    expect(tid("gr-ov-resolved")).toBeNull();
    expect(tid("gr-ov-description")!.textContent).toContain("No description yet");
    unmount();
  });
});

describe("every diff row can be a link target", () => {
  const DIFF = "diff --git a/src/app.js b/src/app.js\n--- a/src/app.js\n+++ b/src/app.js\n"
    + "@@ -38,3 +38,4 @@\n context\n-old()\n+tick()\n+tock()\n";

  it("a unified row carries lineAnchor(path, line, side) as its id", async () => {
    const [file] = parseUnifiedDiff(DIFF);
    await mountAs(<DiffTable file={file} inline={new Map()} />);
    expect(document.getElementById(lineAnchor("src/app.js", 39, "new"))).toBeTruthy();  // +tick()
    expect(document.getElementById(lineAnchor("src/app.js", 39, "old"))).toBeTruthy();  // -old()
    expect(lineAnchor("src/app.js", 39)).toBe("gr-f-src_app_js-R39");
    unmount();
  });

  it("a split pair carries the new side's anchor", async () => {
    const [file] = parseUnifiedDiff(DIFF);
    await mountAs(<DiffTable file={file} inline={new Map()} layout="split" />);
    expect(document.getElementById(lineAnchor("src/app.js", 40, "new"))).toBeTruthy();
    unmount();
  });
});
