// @vitest-environment happy-dom
/**
 * THE CHANGE PAGE AS A GIT-STYLE REVIEW (BOSSAPP-954; wireframe 6-git-review.html): the tree,
 * compare, the agent pane, one count source, the URLs, the comment's code, the dockable panes, and
 * the full-width layout. Pure helpers first, then the panes rendered against a stubbed API.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  agentStatus, ancestorsOf, buildTree, changeBase, changedFiles, changeUrl, commentCounts, crumbs, defaultExpanded, filterFiles,
  fuzzyFiles, middleEllipsis, nameParts, neighbours, parseAgent, rangeLabel, rangeOfTicked, readChangeUrl, sinceMyReview, tick, totals,
  treeFiles, treeRowFit, viewedCount, withoutWhatChanged, type Commit, type TreeNode,
} from "../lib/change-review";
import { anchorWords, parseThreads, snippetFocus } from "../lib/local-review-data";
import { parseChangeSet } from "../lib/repo-links";
import { columns, DEFAULT_LAYOUT, effective, TREE_W, keyAction, layoutKey, loadLayout, parseLayout, reduce, type Layout } from "../lib/review-layout";
import { ReviewLayoutProvider } from "../app/changes/[projectId]/[number]/review-dock";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { FileTree, GoToFile } from "../app/changes/[projectId]/[number]/file-tree";
import { AgentPane, CommitsTab } from "../app/changes/[projectId]/[number]/review-panes";
import { CommentCode, useLineReview } from "../app/changes/[projectId]/[number]/local-review";
import { declarations } from "./_helpers/css-declarations";
import { $$, click, mountAs, stubFetch, tid, type, unmount } from "./_helpers/multirepo-fixtures";

afterEach(() => unmount());

const C = (sha: string, message = sha): Commit => ({ sha: sha.repeat(40).slice(0, 40), message, author: "forge", date: null, fixes: [],
                                                     additions: 1, deletions: 0, files: 1 });
const names = (nodes: TreeNode[]): string[] => nodes.map((n) => (n.kind === "dir" ? `${n.name}/` : n.name));
const tf = (path: string, changed = true) => ({ path, name: path.split("/").pop()!, status: changed ? "modified" : null,
                                                additions: 1, deletions: 0, comments: 0, open: 0, changed });

describe("the file tree", () => {
  it("puts folders first, compacts single-child folder chains, and shows names only", () => {
    const tree = buildTree([tf("local_x/helpdesk/src/app.js"), tf("local_x/helpdesk/src/sla.js"), tf("local_x/helpdesk/README.md"),
                            tf("local_x/helpdesk/test/sla.test.js")]);
    expect(names(tree)).toEqual(["local_x/helpdesk/"]);                 // one row for the chain
    const top = tree[0] as Extract<TreeNode, { kind: "dir" }>;
    expect(top.path).toBe("local_x/helpdesk");
    expect(names(top.children)).toEqual(["src/", "test/", "README.md"]);  // folders before files
    const src = top.children[0] as Extract<TreeNode, { kind: "dir" }>;
    expect(names(src.children)).toEqual(["app.js", "sla.js"]);
  });

  it("opens the folders of changed files and of the open file, and reveals any path", () => {
    const tree = buildTree([tf("a/b/changed.ts"), tf("c/quiet.ts", false), tf("d/e/open.ts", false)]);
    const open = defaultExpanded(tree, "d/e/open.ts");
    expect([...open].sort()).toEqual(["a/b", "d/e"]);
    expect(ancestorsOf(tree, "c/quiet.ts")).toEqual(["c"]);
  });

  it("filters, fuzzy-finds by the whole path, and cuts long names in the middle", () => {
    expect(filterFiles([{ path: "src/app.js" }, { path: "test/a.js" }], "src app").map((f) => f.path)).toEqual(["src/app.js"]);
    const hits = fuzzyFiles(["src/inbox/list.js", "src/sla.js", "docs/slab.md"], "sla");
    expect(hits[0]).toEqual({ path: "src/sla.js", name: "sla.js", folder: "src" });
    expect(fuzzyFiles(["src/app.js"], "zz")).toEqual([]);
    expect(middleEllipsis("helpdesk-laptop-component.tsx", 15)).toBe("helpdes…ent.tsx");
    expect(middleEllipsis("app.js", 15)).toBe("app.js");
    expect(crumbs("src/inbox/list.js")).toEqual([{ name: "src", path: "src", file: false },
      { name: "inbox", path: "src/inbox", file: false }, { name: "list.js", path: "src/inbox/list.js", file: true }]);
  });

  it("marks changed files with +/− and an open-comment dot", () => {
    const rows = treeFiles([{ path: "src/app.js", status: "modified", additions: 14, deletions: 2 }],
                           [{ path: "src/app.js", status: "M" }, { path: "README.md", status: null }],
                           [{ path: "src/app.js", state: "open" }, { path: "src/app.js", state: "resolved" }], "all");
    expect(rows.find((r) => r.path === "src/app.js")).toMatchObject({ changed: true, additions: 14, open: 1, comments: 2 });
    expect(rows.find((r) => r.path === "README.md")).toMatchObject({ changed: false, open: 0 });
  });

  // helpdesk-laptop/6 on the laptop: the names that read "package-" and "esbuildO…e.te" at the
  // default width, at their real depths (the chain local_helpdesk-laptop/helpdesk-laptop is one
  // row; server/tests another) and with their real counts.
  const REAL = [
    { path: "local_helpdesk-laptop/helpdesk-laptop/package-lock.json", additions: 240, deletions: 156, depth: 1 },
    { path: "local_helpdesk-laptop/helpdesk-laptop/package.json", additions: 3, deletions: 0, depth: 1 },
    { path: "local_helpdesk-laptop/helpdesk-laptop/server/tests/esbuildOverride.test.js", additions: 87, deletions: 0, depth: 2 },
  ];
  const nameOf = (p: string) => p.split("/").pop()!;

  it("a name keeps its extension and its end; the head alone gives way, in the middle", () => {
    expect(nameParts("package-lock.json")).toEqual({ head: "package-", tail: "lock.json" });
    expect(nameParts("esbuildOverride.test.js")).toEqual({ head: "esbuildOverride", tail: ".test.js" });
    expect(nameParts("package.json")).toEqual({ head: "pac", tail: "kage.json" });
    expect(nameParts("app.js")).toEqual({ head: "", tail: "app.js" });         // short: never split
    for (const r of REAL) {
      const { head, tail } = nameParts(nameOf(r.path));
      expect(head + tail).toBe(nameOf(r.path));
      expect(tail).toMatch(/\.[a-z]+$/);                                         // the extension is in the part that never shrinks
    }
  });

  it("the +/− counts give way first: full, then compact, then hidden — at the default and the minimum width", () => {
    const fit = (w: number) => REAL.map((r) => treeRowFit(nameOf(r.path), { width: w, depth: r.depth, stats: true, status: true,
                                                                             additions: r.additions, deletions: r.deletions }).counts);
    expect(DEFAULT_LAYOUT.treeW).toBe(264);
    expect(fit(TREE_W.initial)).toEqual(["compact", "full", "hidden"]);
    expect(fit(TREE_W.min)).toEqual(["hidden", "hidden", "hidden"]);
    expect(fit(TREE_W.max)).toEqual(["full", "full", "full"]);
    expect(treeRowFit("package-lock.json", { width: 264, depth: 1, stats: true, additions: 240, deletions: 156 }))
      .toMatchObject({ full: "+240 −156", compact: "±396" });
    expect(treeRowFit("app.js", { width: 264, depth: 0, stats: false, additions: 3 }).counts).toBe("hidden");   // the Code tree
  });

  for (const [label, width, stats] of [["Files changed, default width", TREE_W.initial, true], ["Files changed, minimum width", TREE_W.min, true],
                                       ["Code, default width", TREE_W.initial, false], ["Code, minimum width", TREE_W.min, false]] as const) {
    it(`renders the real names whole, split head|tail, with the counts on the title — ${label}`, async () => {
      const files = REAL.map((r) => ({ path: r.path, name: nameOf(r.path), status: r.path.endsWith("lock.json") ? "M" : "A",
                                       additions: r.additions, deletions: r.deletions, comments: 0, open: 0, changed: true }));
      await mountAs(<FileTree files={files} current={null} onPick={() => undefined} stats={stats} width={width} />);
      const rows = $$("[data-testid='gr-tree-file']");
      expect(rows.map((r) => r.querySelector("[data-testid='gr-tree-name']")?.textContent))
        .toEqual(["esbuildOverride.test.js", "package-lock.json", "package.json"]);            // never a cut string
      expect(rows.map((r) => r.querySelector("[data-testid='gr-name-tail']")?.textContent)).toEqual([".test.js", "lock.json", "kage.json"]);
      const lock = rows[1];
      expect(lock.getAttribute("title")).toBe(stats ? `${REAL[0].path} · +240 −156` : REAL[0].path);
      const pm = lock.querySelector("[data-testid='gr-tree-pm']");
      if (!stats || width === TREE_W.min) expect(pm).toBeNull();
      else expect(pm?.getAttribute("data-mode")).toBe("compact");
      expect(lock.textContent).not.toContain("…");                                   // the ellipsis is CSS's, at the head's end
      unmount();
    });
  }

  it("the row's CSS: the head shrinks with an ellipsis, the tail and the counts never shrink", () => {
    const css = readFileSync(join(__dirname, "../app/changes/[projectId]/[number]/change-set.css"), "utf8");
    const d = declarations(css);
    const v = (sel: string, prop: string) => d.find((x) => x.selector === sel && x.prop === prop)?.value;
    expect(v(".gr-name-h", "text-overflow")).toBe("ellipsis");
    expect(v(".gr-name-h", "min-width")).toBe("0");
    expect(v(".gr-name-t", "flex")).toBe("none");
    expect(v(".gr-tree-row > :not(.gr-name):not(.cs-spacer)", "flex")).toBe("none");
    expect(d.filter((x) => /gr-ah-t b|cs-who/.test(x.selector) && x.prop === "text-transform")).toEqual([]);   // ids are never title-cased
  });

  it("a reveal focuses its file, unless the person moved focus elsewhere meanwhile", async () => {
    const files = [{ path: "src/app.js", name: "app.js", status: "A", additions: 1, deletions: 0, comments: 0, open: 0, changed: true }];
    const reveal = { path: "src/app.js", n: 1 };
    const host = await mountAs(<><button type="button" data-testid="elsewhere">x</button>
      <FileTree files={files} current={null} onPick={() => undefined} testid="lr-tree-item" reveal={reveal} /></>);
    (host.querySelector("[data-testid='elsewhere']") as HTMLElement).focus();     // a key on another control
    await act(async () => { await new Promise((ok) => setTimeout(ok, 60)); });
    expect(document.activeElement?.getAttribute("data-testid")).toBe("elsewhere");
    unmount();
  });

  it("renders a tree that expands and collapses, and go to file picks with the keyboard", async () => {
    const picked: string[] = [];
    await mountAs(<div><FileTree files={[tf("src/app.js"), tf("src/inbox/list.js"), tf("docs/a.md", false)]} current={null}
                                    onPick={(p) => picked.push(p)} stats />
      <GoToFile paths={["src/app.js", "src/inbox/list.js"]} onPick={(p) => picked.push(`go:${p}`)} /></div>);
    const dirs = () => $$("[data-testid='gr-tree-dir']").map((d) => d.getAttribute("data-path"));
    expect(dirs()).toEqual(["docs", "src", "src/inbox"]);                // docs closed: nothing changed in it
    expect($$("[data-testid='gr-tree-file']").map((f) => f.textContent)).not.toContain("a.md");
    await click($$("[data-testid='gr-tree-dir']")[1]);                    // collapse src
    expect(dirs()).toEqual(["docs", "src"]);
    await click($$("[data-testid='gr-tree-dir']")[0]);                    // open docs
    expect($$("[data-testid='gr-tree-name']").map((n) => n.textContent)).toContain("a.md");
    const input = tid("lr-goto") as HTMLInputElement;
    await type(input, "list");
    expect(tid("gr-goto-results")?.textContent).toContain("list.js");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    expect(picked).toContain("go:src/inbox/list.js");
  });
});

describe("compare: base…head", () => {
  const commits = [C("c"), C("b"), C("a")];                              // newest first
  it("two ticks compare older…newer, a third replaces the oldest tick", () => {
    expect(rangeOfTicked(commits, [commits[0].sha, commits[2].sha])).toEqual({ base: commits[2].sha, head: commits[0].sha });
    expect(rangeOfTicked(commits, [commits[0].sha])).toBeNull();
    expect(tick(tick(tick([], "x"), "y"), "z")).toEqual(["y", "z"]);
    expect(tick(["x"], "x")).toEqual([]);
  });
  it("Since my review starts at the head the person last reviewed", () => {
    expect(sinceMyReview({ person: "p", paths: {}, reviewed_sha: "old" }, "new")).toEqual({ base: "old", head: "new" });
    expect(sinceMyReview({ person: "p", paths: {}, reviewed_sha: "new" }, "new")).toBeNull();
    expect(rangeLabel({ base: null, head: null }, "main", commits)).toBe(`main … ${commits[0].sha.slice(0, 7)} · latest`);
    expect(viewedCount([{ path: "a" }, { path: "b" }], { person: "p", paths: { a: "h", b: "old" }, reviewed_sha: null }, "h")).toBe(1);
  });
});

describe("one source for every count", () => {
  it("the diff decides the files and their +/−; a file without lines still counts", () => {
    const diff = "diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1 +1,2 @@\n-x\n+y\n+z\n"
      + "diff --git a/b.js b/b.js\nnew file mode 100644\n--- /dev/null\n+++ b/b.js\n@@ -0,0 +1 @@\n+b\n";
    const files = changedFiles([{ path: "a.js", status: "modified", additions: 9, deletions: 9 },
                                { path: "logo.png", status: "added", additions: 0, deletions: 0 }],
                               [{ path: "a.js", status: "modified", additions: 2, deletions: 1 }, { path: "b.js", status: "added", additions: 1, deletions: 0 }]);
    expect(files.map((f) => f.path)).toEqual(["a.js", "logo.png", "b.js"]);
    expect(totals(files)).toEqual({ files: 3, additions: 3, deletions: 1 });
    expect(diff.length).toBeGreaterThan(0);
  });
  it("drops the description's own What changed section", () => {
    expect(withoutWhatChanged("Adds a timer.\n\n## What changed\n- 2 files\n\n## Why\nSLA.")).toBe("Adds a timer.\n\n## Why\nSLA.");
  });
});

describe("URLs: tabs and commits are links, and Back reads them", () => {
  const at = { projectId: "p1", number: 3 };
  it("a commit has its own path; other tabs keep ?tab=", () => {
    expect(changeUrl(at, "commits", "abcdef1")).toBe("/changes/p1/3/commits/abcdef1");
    expect(changeUrl({ ...at, repo: "acme/store" }, "commits", "abcdef1")).toBe("/changes/p1/3/commits/abcdef1?repo=acme%2Fstore");
    expect(changeUrl(at, "files")).toBe("/changes/p1/3");
    expect(changeUrl(at, "checks")).toBe("/changes/p1/3?tab=checks");
    expect(readChangeUrl("/changes/p1/3/commits/abcdef1", "")).toEqual({ tab: "commits", commit: "abcdef1" });
    expect(readChangeUrl("/changes/p1/3", "?tab=code")).toEqual({ tab: "code", commit: null });
    expect(changeBase({ ...at, repo: "acme/store" })).toBe("/v1/projects/p1/github-changes/acme/store/3");
  });
  it("Previous is the older commit, Next the newer", () => {
    const commits = [C("c"), C("b"), C("a")];
    expect(neighbours(commits, commits[1].sha)).toEqual({ prev: commits[2].sha, next: commits[0].sha, index: 1 });
    expect(neighbours(commits, commits[0].sha).next).toBeNull();
  });
});

describe("a comment shows its code", () => {
  const [t] = parseThreads([{ id: 7, path: "src/app.js", line: 39, body: "Tick once a minute.", outdated: true,
    commit_sha: "a".repeat(40), snippet: { start: 37, line: 39, lines: ["a", "b", "c", "d", "e"], commit: "a".repeat(40) }, replies: [] }]);
  it("anchors: the line in its snippet, or the whole change", () => {
    expect(snippetFocus(t.snippet!)).toBe(2);
    expect(snippetFocus({ start: null, line: null, lines: ["x", "y"], commit: null })).toBe(1);   // a GitHub hunk ends there
    expect(anchorWords(t)).toBe("src/app.js:39");
    expect(anchorWords({ path: null, line: null })).toBe("on the whole change");
  });
  it("renders file:line, the snippet with its line marked, outdated with the commit it was made on", async () => {
    const opened: number[] = [];
    await mountAs(<CommentCode thread={t} onOpen={((x: { id: number }) => opened.push(x.id)) as never}
                               commitHref={(sha) => `/changes/p/1/commits/${sha}`} />);
    expect(tid("gr-comment-anchor")?.textContent).toBe("src/app.js:39");
    expect($$("[data-testid='gr-snippet'] tr[data-focus]").map((r) => r.textContent)).toEqual(["39c"]);
    expect(tid("gr-outdated")).toBeTruthy();
    expect(tid("gr-made-on")?.getAttribute("href")).toBe(`/changes/p/1/commits/${"a".repeat(40)}`);
    await click(tid("gr-comment-anchor"));
    expect(opened).toEqual([7]);
  });
});

describe("the dockable panes", () => {
  it("collapse to rails, open as drawers, pin back, and focus mode restores what was there", () => {
    let s: Layout = DEFAULT_LAYOUT;
    s = reduce(s, { type: "toggle", pane: "tree" });
    expect(s.tree).toBe("rail");
    s = reduce(s, { type: "open", pane: "tree" });
    expect(s.tree).toBe("overlay");
    s = reduce(s, { type: "open", pane: "agent" });
    expect([s.tree, s.agent]).toEqual(["rail", "overlay"]);              // one drawer at a time
    s = reduce(s, { type: "pin", pane: "agent" });
    expect(s.agent).toBe("docked");
    s = reduce(s, { type: "focus" });
    expect([s.tree, s.agent, s.focus]).toEqual(["rail", "rail", true]);
    s = reduce(s, { type: "focus" });
    expect([s.tree, s.agent, s.focus]).toEqual(["rail", "docked", false]);
    expect(reduce(s, { type: "resize", pane: "tree", width: 9999 }).treeW).toBe(560);
  });
  it("three key presses on a resize handle are three steps, each from the width in the state", () => {
    let s: Layout = DEFAULT_LAYOUT;
    for (let i = 0; i < 3; i++) s = reduce(s, { type: "nudge", pane: "tree", by: 16 });
    expect(s.treeW).toBe(DEFAULT_LAYOUT.treeW + 48);
    expect(reduce(s, { type: "nudge", pane: "agent", by: -9999 }).agentW).toBe(260);
  });
  it("what was remembered survives a reload, even with React's double effects (StrictMode)", async () => {
    const key = layoutKey("", "change");
    window.localStorage.setItem(key, JSON.stringify({ ...DEFAULT_LAYOUT, treeW: 312, agent: "rail" }));
    const host = document.createElement("div");
    document.body.appendChild(host);
    const r = createRoot(host);
    await act(async () => r.render(<React.StrictMode><ReviewLayoutProvider page="change"><span /></ReviewLayoutProvider></React.StrictMode>));
    await act(async () => { await new Promise((ok) => setTimeout(ok, 0)); });
    expect(parseLayout(window.localStorage.getItem(key))).toMatchObject({ treeW: 312, agent: "rail" });
    expect(host.querySelector<HTMLElement>("[data-testid='gr-layout']")!.style.getPropertyValue("--gr-tree-col")).toBe("312px");
    act(() => r.unmount());
    host.remove();
    window.localStorage.removeItem(key);
  });
  it("nothing remembered keeps what is on screen (a resize before the identity arrived stays)", () => {
    window.localStorage.removeItem(layoutKey("usr_new", "change"));
    expect(loadLayout("usr_new", "change")).toBeNull();
  });
  it("keys: [ ] \\ — never while typing", () => {
    expect(keyAction("[", false)).toEqual({ type: "toggle", pane: "tree" });
    expect(keyAction("]", false)).toEqual({ type: "toggle", pane: "agent" });
    expect(keyAction("\\", false)).toEqual({ type: "focus" });
    expect(keyAction("[", true)).toBeNull();
  });
  it("a tablet makes the agent a drawer, a phone shows only the code", () => {
    expect(effective(DEFAULT_LAYOUT, 1440)).toEqual({ tree: "docked", agent: "docked" });
    expect(effective(DEFAULT_LAYOUT, 900)).toEqual({ tree: "docked", agent: "rail" });
    expect(effective(DEFAULT_LAYOUT, 390)).toEqual({ tree: "rail", agent: "rail" });
    expect(columns(DEFAULT_LAYOUT, 1440)).toEqual({ tree: "264px", agent: "340px" });
    expect(columns({ ...DEFAULT_LAYOUT, tree: "rail" }, 1440).tree).toBe("2.25rem");
  });
  it("what is remembered comes back safe (an open drawer comes back a rail)", () => {
    expect(parseLayout(JSON.stringify({ tree: "overlay", agent: "rail", treeW: 300, agentW: 10 }))).toMatchObject(
      { tree: "rail", agent: "rail", treeW: 300, agentW: 260 });
    expect(parseLayout("not json")).toEqual(DEFAULT_LAYOUT);
    expect(parseLayout(null)).toEqual(DEFAULT_LAYOUT);
  });
});

describe("the PR agent pane", () => {
  const agent = { run: { run_id: "crun_1", title: "SLA", status: "completed", phase: "done", running: false, builder: "Forge",
                         cost_usd: 0.41, updated_at: new Date(Date.now() - 12 * 60_000).toISOString() },
                  builder: null, agent: "Forge", open: 2, fixable: 2,
                  comments: [{ id: 1, path: "src/app.js", line: 39, body: "tick once a minute", state: "open" },
                             { id: 2, path: "src/sla.js", line: 12, body: "business hours", state: "open" },
                             { id: 3, path: "src/list.js", line: 8, body: "empty state copy", state: "addressed", fixed_in: "9e4f1c2aa" }],
                  messages: [{ id: 1, author: "Param", author_kind: "human", body: "Why before render?", delivery: "queued" }],
                  viewed: { person: "p", paths: {}, reviewed_sha: null } };
  it("counts open and fixed, and says idle with the last run", () => {
    const pane = parseAgent(agent);
    expect(commentCounts(pane.comments)).toEqual({ open: 2, fixed: 1, resolved: 0 });
    expect(agentStatus(pane).label).toBe("Idle · last run 12 min ago");
    expect(agentStatus({ ...pane, run: { ...pane.run!, running: true } }).label).toBe("Working");
  });
  it("Fix N open comments starts the fix run; a message goes to the agent", async () => {
    const cs = parseChangeSet({ number: 1, state: "open", branch: "boss/ct-41a", comments: [] });
    const calls = stubFetch((c) => (c.path.endsWith("/agent") ? { body: agent }
      : c.path.endsWith("/request-changes") ? { body: { status: "fix_run_started", run_id: "crun_2" } }
      : c.path.endsWith("/agent/messages") ? { body: { delivery: "queued", message: "Kept for the agent: it goes with the next fix run." } }
      : undefined));
    await mountAs(<AgentPane cs={cs} at={{ projectId: "p1", number: 1 }} onJump={() => undefined} onChanged={() => undefined} />);
    expect(tid("gr-agent-name")?.textContent).toBe("Forge");
    expect(tid("gr-agent-role")).toBeNull();                                   // no role given, none shown
    expect(tid("lr-open-count")?.textContent).toBe("2 open");
    expect(tid("gr-fix-open")?.textContent).toContain("Fix 2 open comments");
    await click(tid("gr-fix-open"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/projects/p1/local-changes/1/request-changes")).toBe(true);
    await type(tid("gr-chat-input"), "Move it after render");
    await click(tid("gr-chat-send"));
    const sent = calls.find((c) => c.path.endsWith("/agent/messages"));
    expect(sent?.body).toEqual({ body: "Move it after render" });
    expect($$("[data-testid='gr-agent-comment']").length).toBe(3);
  });
});

describe("the Commits tab", () => {
  function Harness({ shown }: { shown: string | null }) {
    const at = { projectId: "p1", number: 1 };
    const review = useLineReview({ at, threads: [], onChanged: () => undefined, commitFor: () => null });
    const cs = parseChangeSet({ number: 1, state: "open", base_branch: "main" });
    const commits = [C("c", "Fix the copy"), C("b", "Add sla.js")];
    return <CommitsTab cs={cs} at={at} commits={commits} shown={shown} onShow={() => undefined}
                       hrefFor={(sha) => (sha ? `/changes/p1/1/commits/${sha}` : "/changes/p1/1?tab=commits")}
                       wholeFiles={[{ path: "src/sla.js", status: "added", additions: 3, deletions: 0 }]} wholeDiff="" review={review}
                       inline={new Map()} onCompare={() => undefined} onViewFile={() => undefined} />;
  }
  it("lists All commits first (the default), each commit a real link with its +/−", async () => {
    stubFetch(() => undefined);
    await mountAs(<Harness shown={null} />);
    expect(tid("gr-commit-all")?.textContent).toContain("All commits");
    expect(tid("gr-commit-all-view")).toBeTruthy();
    const links = $$("[data-testid='gr-commit-open']");
    expect(links.map((a) => a.getAttribute("href"))).toEqual([`/changes/p1/1/commits/${"c".repeat(40)}`, `/changes/p1/1/commits/${"b".repeat(40)}`]);
    expect($$("[data-testid='gr-commit-stat']")[0].textContent).toContain("+1");
    await click($$("[data-testid='gr-commit-tick']")[0]);
    await click($$("[data-testid='gr-commit-tick']")[1]);
    expect(tid("gr-compare-bar")?.textContent).toContain(`${"b".repeat(7)} … ${"c".repeat(7)}`);
  });
  it("one commit: its own diff, with Previous and Next", async () => {
    stubFetch((c) => (c.path.includes("/commits/") ? { body: { sha: "b".repeat(40), parent: "a".repeat(40), message: "Add sla.js", author: "forge",
      files: [{ path: "src/sla.js", status: "added", additions: 1, deletions: 0 }],
      diff: "diff --git a/src/sla.js b/src/sla.js\nnew file mode 100644\n--- /dev/null\n+++ b/src/sla.js\n@@ -0,0 +1 @@\n+x\n" } } : undefined));
    await mountAs(<Harness shown={"b".repeat(40)} />);
    expect(tid("gr-commit-message")?.textContent).toBe("Add sla.js");
    expect(tid("gr-commit-next")?.getAttribute("href")).toBe(`/changes/p1/1/commits/${"c".repeat(40)}`);
    expect((tid("gr-commit-prev") as HTMLButtonElement).disabled).toBe(true);
    expect($$("[data-testid^='change-set-file-']").length).toBe(1);
  });
});

describe("the page uses the full width", () => {
  const css = readFileSync(join(__dirname, "../app/changes/[projectId]/[number]/change-set.css"), "utf8");
  const d = declarations(css);
  it("no .cs/.gr page container sets a max-width; the old caps are gone", () => {
    const capped = d.filter((x) => x.prop === "max-width" && /^\.(cs|gr)(\b|$|[\s,])/.test(x.selector)
                                   && /^\.(cs|gr|gr-layout|gr-body|gr-main|gr-tab|gr-files|lr-code)$/.test(x.selector.trim()));
    expect(capped).toEqual([]);
    expect(css).not.toMatch(/1240px|1560px/);
    expect(d.find((x) => x.selector === ".cs" && x.prop === "width")?.value).toBe("100%");
  });
  it("the tree and agent take their widths and the diff the rest, at 1280, 1920 and 2560", () => {
    expect(d.find((x) => x.selector === ".gr-body" && x.prop === "grid-template-columns")?.value).toBe("minmax(0, 1fr) var(--gr-agent-col)");
    expect(d.find((x) => x.selector === ".gr-files, .lr-code" && x.prop === "grid-template-columns")?.value).toBe("var(--gr-tree-col) minmax(0, 1fr)");
    for (const vw of [1280, 1920, 2560]) {
      const c = columns(DEFAULT_LAYOUT, vw);
      const tree = parseFloat(c.tree);
      const agent = parseFloat(c.agent);
      const gutters = 16 * 2 + 12 * 2;
      const diff = vw - gutters - tree - agent;
      expect(diff).toBe(vw - 56 - 264 - 340);                            // the diff takes every pixel left
      expect(diff / vw).toBeGreaterThan(vw === 1280 ? 0.45 : 0.6);       // 620px at 1280, 1652px at 2560
    }
  });
});
