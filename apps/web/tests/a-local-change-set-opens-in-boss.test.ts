/**
 * LOCAL-FIRST PROJECTS (BOSSAPP-915): a local project's repository and its change sets have no page
 * on GitHub. Every console link goes through lib/repo-links.ts, so a `local_<project>/<name>`
 * repository never becomes a github.com link and a `http://boss-local/...` PR URL opens BOSS's own
 * change-set view (/changes/<project_id>/<number>).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

import {
  blobHref, changeHref, changeSubHref, isLocalChangeUrl, isLocalRepo, linkTarget, parseChangeSet,
  parseLocalChange, prLinkLabel, repoHostLabel, repoHref,
} from "../lib/repo-links";
import { codeHref } from "../lib/kg-view";

const LOCAL_PR = "http://boss-local/local_prj_notes/notes/pull/3";
const ROOT = join(__dirname, "..");

describe("repo-links", () => {
  it("knows a local repository and a local change set", () => {
    expect(isLocalRepo("local_prj_notes/notes")).toBe(true);
    expect(isLocalRepo("acme/store")).toBe(false);
    expect(isLocalChangeUrl(LOCAL_PR)).toBe(true);
    expect(isLocalChangeUrl("https://github.com/acme/store/pull/3")).toBe(false);
    expect(parseLocalChange(LOCAL_PR)).toEqual({ projectId: "prj_notes", repo: "local_prj_notes/notes", number: 3 });
    expect(parseLocalChange("https://github.com/acme/store/pull/3")).toBeNull();
  });

  it("opens a local change set in BOSS and a GitHub PR on GitHub", () => {
    expect(changeHref(LOCAL_PR)).toBe("/changes/prj_notes/3");
    expect(changeSubHref(LOCAL_PR, "files")).toBe("/changes/prj_notes/3");
    expect(changeHref("https://github.com/acme/store/pull/3")).toBe("https://github.com/acme/store/pull/3");
    expect(changeSubHref("https://github.com/acme/store/pull/3", "files")).toBe("https://github.com/acme/store/pull/3/files");
    expect(changeHref(null)).toBeNull();
    expect(linkTarget(LOCAL_PR)).toEqual({});
    expect(linkTarget("https://github.com/acme/store/pull/3")).toEqual({ target: "_blank", rel: "noreferrer" });
    expect(prLinkLabel(LOCAL_PR)).toBe("Open change set");
    expect(prLinkLabel("https://github.com/acme/store/pull/3")).toBe("Open PR on GitHub");
  });

  it("never links a local repository to github.com", () => {
    expect(repoHref("local_prj_notes/notes", null)).toBe("/?project_id=prj_notes&area=project&tab=knowledge");
    expect(repoHref("acme/store", null)).toBe("https://github.com/acme/store");
    expect(blobHref("local_prj_notes/notes", "README.md")).toBeNull();
    expect(codeHref("local_prj_notes/notes", "app.py", 3)).toBeNull();
    expect(codeHref("acme/store", "app.py", 3)).toBe("https://github.com/acme/store/blob/HEAD/app.py#L3");
    expect(repoHostLabel("local")).toBe("Local repo");
    expect(repoHostLabel("local_prj_notes/notes")).toBe("Local repo");
    expect(repoHostLabel("acme/store")).toBe("GitHub");
  });

  it("reads the change-set view's answer, missing fields and all", () => {
    const cs = parseChangeSet({ number: 3, title: "Notes", state: "open", repository: "local_prj_notes/notes",
      branch: "boss/code/x", base_branch: "main", files: [{ path: "app.py", status: "added", additions: 2, deletions: 0 }],
      commits: [{ sha: "a".repeat(40), message: "add" }], review: [{ id: 1, kind: "review", body: "ok", comments: [] }],
      merge: { how: "approve the review decision", decision_id: "dec_1" } });
    expect(cs.files[0]).toEqual({ path: "app.py", status: "added", additions: 2, deletions: 0 });
    expect(cs.merge?.decision_id).toBe("dec_1");
    expect(parseChangeSet(null).files).toEqual([]);
  });

  it("no console file opens a pull_request_url without lib/repo-links.ts", () => {
    // A raw `href={…pull_request_url}` would send a local change set to a dead boss-local link.
    const offenders: string[] = [];
    const skip = /(^|\/)(node_modules|\.next|tests|stub)(\/|$)/;
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        const rel = relative(ROOT, full);
        if (skip.test(rel)) continue;
        if (statSync(full).isDirectory()) { walk(full); continue; }
        if (!/\.tsx$/.test(name)) continue;
        readFileSync(full, "utf8").split("\n").forEach((line, i) => {
          if (/href=\{([\w?]*[.?])?(pull_request_url|pr_url|prUrl)\}/.test(line)) offenders.push(`${rel}:${i + 1}: ${line.trim().slice(0, 100)}`);
        });
      }
    };
    for (const top of ["app", "components"]) walk(join(ROOT, top));
    expect(offenders, "wrap PR links in changeHref()").toEqual([]);
  });
});
