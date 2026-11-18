/**
 * The Approvals page, the rail badge and the bell count ONE set (stranger test r3, 2026-11-18).
 *
 * A fresh install: the open project (the default one) has nothing waiting, and the sample, Hello
 * World and the to-do app have 8 set-up asks between them. The page said "Needs action 0 / All 0",
 * the rail "+8" and the bell "8", and the rail's "+N" was org total − this project's live count
 * while the page summed the other projects, so they split whenever the summary's row for the open
 * project and its live count differed.
 *
 * Breaks if the page's tabs stop showing the "+N in other projects" the rail shows, or if the rail,
 * bell and page compute "elsewhere" from different sums.
 */
import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ProjectCard, ProjectsSummary } from "../lib/project-experience";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", { ...props, type: props.type ?? "submit" }, props.children),
}));

const card = (id: string, needs: number, archived = false) =>
  ({ id, name: id, needs_you: needs, archived } as unknown as ProjectCard);
// The summary's own row for the open project says 2 (stale); its live count says 0.
const FRESH: ProjectsSummary = {
  week_cap_usd: null,
  items: [card("boss-dogfood", 2), card("sample", 3), card("hello-world", 3), card("todo", 2),
          card("old", 5, true)],
};

vi.mock("../lib/agents-live", () => ({ useAttention: () => ({ count: 0, setup: 0, items: [] }) }));
vi.mock("../lib/project-experience", async (orig) => {
  const real = await orig<typeof import("../lib/project-experience")>();
  return { ...real, usePxRead: () => ({ state: "ready", data: FRESH }) };
});

const { approvalCountsOf } = await import("../lib/project-experience");
const { approvalsBadge } = await import("../app/ConsoleShell");
const { DecisionInbox, waitingElsewhere } = await import("../app/decision-inbox-ui");

describe("one set for the page, the rail and the bell", () => {
  it("here is the live count, elsewhere the other open projects, total their sum", () => {
    const c = approvalCountsOf(0, FRESH, "boss-dogfood");
    expect(c).toEqual({ here: 0, elsewhere: 8, total: 8 });
    // The rail: 0 here, "+8"; the old org-total − here said "+10".
    expect(approvalsBadge({ count: 0 }, c.total, 0, true)).toEqual({ count: 0, elsewhere: 8 });
    // The page's pointer counts the same 8.
    expect(waitingElsewhere(FRESH, "boss-dogfood").total).toBe(c.elsewhere);
    // Before the live count answers, the summary's own row stands in.
    expect(approvalCountsOf(null, FRESH, "boss-dogfood")).toEqual({ here: 2, elsewhere: 8, total: 10 });
    // No project open: all of it is elsewhere. No summary yet: only what is known here.
    expect(approvalCountsOf(null, FRESH, null)).toEqual({ here: null, elsewhere: 8 + 2, total: 10 });
    expect(approvalCountsOf(3, null, "x")).toEqual({ here: 3, elsewhere: 0, total: 3 });
  });

  it("the Approvals tabs show the rail's +8 beside their own 0", () => {
    const markup = renderToStaticMarkup(
      <DecisionInbox feed={{ projectId: "boss-dogfood", apiReachable: true, items: [] }}
                     tasks={[]} roles={[]} projectId="boss-dogfood" actorId="ceo" orgId="org_1" />,
    );
    const tab = (name: string) => {
      const m = markup.match(new RegExp(`data-testid="decision-filter-${name}"[^>]*>(.*?)</button>`));
      return m ? m[1] : "";
    };
    for (const name of ["needs-action", "all"]) {
      expect(tab(name)).toContain('<span class="dq-filter-count">0</span>');
      expect(tab(name)).toContain('data-testid="decision-filter-elsewhere"');
      expect(tab(name)).toContain(">+8</span>");
      expect(tab(name)).toContain('title="8 in other projects"');
    }
    expect(markup).toContain("8 waiting in other projects");
  });

  it("the shell feeds the bell, the badge and the title from the shared counts", () => {
    const shell = readFileSync("app/ConsoleShell.tsx", "utf8");
    expect(shell).toContain("useApprovalCounts(scope.projectId ?? scope.project, titleAttention?.count).total");
    expect(shell).not.toContain("useOrgNeeds()");
  });
});
