// @vitest-environment happy-dom
/**
 * A change-set lane row shows head_sha (first 7 characters) and the addition and deletion
 * counts only when those fields are actually on the lane. A null or missing field adds
 * nothing, and the sha is never taken from merged_sha, the branch, or any other field.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChangeSetView, Lane } from "../lib/multirepo";
import { changeSetFixture, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

const { ChangeSetCockpit } = await import("../app/multirepo/change-set-cockpit");

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });

function without<T extends object>(row: T, keys: (keyof T)[]): T {
  const copy = { ...row };
  for (const key of keys) delete copy[key];
  return copy;
}

function mount(cs: ChangeSetView) {
  stubFetch((c) => {
    if (c.method === "GET" && c.path === "/v1/change-sets/rcs_1") return { body: cs };
    return undefined;
  });
  return mountAs(<ChangeSetCockpit projectId="p1" changeSetId="rcs_1" actor="ceo" onClose={() => {}} />, "owner");
}

const row = (order: number) => tid(`mr-lane-${order}`)?.textContent ?? "";

describe("a change-set lane shows its head and its diff", () => {
  it("shows the first 7 characters of head_sha and does not borrow another field", async () => {
    const cs = changeSetFixture();
    cs.lanes[0] = {
      ...cs.lanes[0],
      head_sha: "abcdef1234567890",
      merged_sha: "zzzzzzznotthis",
      branch: "boss/not-a-sha",
    };
    await mount(cs);
    const text = row(1);
    expect(text).toContain("abcdef1");
    expect(text).not.toContain("abcdef12");
    expect(text).not.toContain("zzzzzzz");
    expect(text).not.toContain("boss/no");
  });

  it("shows an addition and a deletion when each one is a number, including zero", async () => {
    const cs = changeSetFixture();
    cs.lanes[0] = { ...cs.lanes[0], additions: 0, deletions: 0 };
    cs.lanes[1] = {
      ...cs.lanes[1],
      head_sha: "",
      additions: 9,
      deletions: null,
      changed_files: null,
      merged_sha: "cafebabecafebabe",
    };
    cs.lanes[2] = {
      ...cs.lanes[2],
      head_sha: null,
      additions: null,
      deletions: 4,
      changed_files: null,
      merged_sha: "feedfacefeedface",
    };
    await mount(cs);
    expect(row(1)).toContain("+0");
    expect(row(1)).toContain("−0");
    expect(row(2)).toContain("+9");
    expect(row(2)).not.toMatch(/−/);
    expect(row(2)).not.toContain("cafebab");
    expect(row(3)).toContain("−4");
    expect(row(3)).not.toContain("+");
    expect(row(3)).not.toContain("feedfac");
  });

  it("adds nothing when head_sha, additions, and deletions are absent or null", async () => {
    const cs = changeSetFixture();
    cs.lanes[1] = {
      ...cs.lanes[1],
      head_sha: null,
      additions: null,
      deletions: null,
      changed_files: null,
      merged_sha: "cafebabecafebabe",
      branch: "boss/not-a-sha",
    };
    const quiet = without(cs.lanes[2], ["head_sha", "additions", "deletions", "changed_files"]);
    cs.lanes[2] = {
      ...quiet,
      merged_sha: "feedfacefeedface",
      branch: "boss/not-a-sha",
      pr_number: null,
      pr_url: null,
    } as Lane;
    await mount(cs);
    expect(row(2)).not.toMatch(/\+[0-9]|−[0-9]/);
    expect(row(2)).not.toContain("cafebab");
    expect(row(2)).not.toContain("boss/no");
    expect(row(3)).not.toMatch(/\+[0-9]|−[0-9]/);
    expect(row(3)).not.toContain("feedfac");
    expect(row(3)).not.toContain("boss/no");
    expect(row(1)).not.toContain("$");
    expect(row(2)).not.toContain("$");
    expect(row(3)).not.toContain("$");
  });
});
