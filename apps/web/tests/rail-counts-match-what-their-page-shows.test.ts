/**
 * EVERY COUNT ON THE RAIL MATCHES ITS PAGE (stranger test 2026-11-17):
 *   - the rail said "All projects (1)" (the shell's Default project) over a page that said
 *     "Add your first project": the rail now counts the summary's projects, as All projects does
 *   - the first-run heading says "first" only while there is no project
 *   - the Approvals badge said 5 over a queue that said 0: it is the queue's count now
 */
import { describe, expect, it } from "vitest";

import { approvalsBadge } from "../app/ConsoleShell";
import { firstRunHeading } from "../app/projects/first-run";
import { railList } from "../app/projects/nav";
import type { ProjectCard } from "../lib/project-experience";

const card = (id: string, extra: Partial<ProjectCard> = {}) =>
  ({ id, name: id, needs_you: 0, running: 0, stage: "ready", updated_at: "", archived: false, ...extra }) as ProjectCard;
const SHELL = [{ id: "default", name: "Default project" }];

describe("the rail's project list", () => {
  it("once the summary answered with none, lists none (the Default project is not counted)", () => {
    expect(railList("ready", [], SHELL)).toEqual([]);
  });
  it("counts what All projects counts: the summary's live projects", () => {
    const cards = [card("hello"), card("tiny"), card("old", { archived: true })];
    expect(railList("ready", cards, SHELL).map((p) => p.id)).toEqual(["hello", "tiny"]);
  });
  it("waits while the summary loads, and falls back to the shell's list only when it cannot be read", () => {
    expect(railList("loading", undefined, SHELL)).toEqual([]);
    expect(railList("missing", null, SHELL).map((p) => p.id)).toEqual(["default"]);
    expect(railList("error", null, SHELL).map((p) => p.id)).toEqual(["default"]);
  });
});

describe("the first-run heading", () => {
  it("says first only while there is no project", () => {
    expect(firstRunHeading([])).toBe("Add your first project");
    expect(firstRunHeading([{ archived: true }])).toBe("Add your first project");
    expect(firstRunHeading([{ archived: false }])).toBe("Add a project");
  });
});

describe("the Approvals badge", () => {
  it("is the queue's Needs action for the open project, not the org-wide bell", () => {
    expect(approvalsBadge({ count: 0 }, 5)).toBe(0);
    expect(approvalsBadge({ count: 2 }, 0)).toBe(2);
    expect(approvalsBadge(null, 4)).toBe(4);
  });
});
