/**
 * This week's deploy point shows the short commit beside its label, and nowhere else.
 * The label stays the engine's words. A missing commit adds nothing, and a full commit on
 * another field is not turned into one.
 */
import React from "react";
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

import { Timeline } from "../app/projects/kit";
import { parseOverview } from "../lib/project-experience";

const WEEK = [
  { at: "2026-11-14T12:00:00Z", kind: "deploy", label: "Staging deploy", href: "?area=infra", sha7: "1234567" },
  { at: "2026-11-15T10:00:00Z", kind: "deploy", label: "Production deploy", href: "?area=infra" },
  { at: "2026-11-15T11:00:00Z", kind: "pr", label: "PR #3 opened", href: "https://example.test/pull/3" },
];

describe("This week shows a deploy's short commit", () => {
  it("puts sha7 beside the label and shows nothing when the commit is absent", () => {
    const week = parseOverview({ week: WEEK }).week;
    expect(week.map((p) => p.sha7)).toEqual(["1234567", undefined, undefined]);
    expect(week.map((p) => p.label)).toEqual(["Staging deploy", "Production deploy", "PR #3 opened"]);

    const html = renderToStaticMarkup(<Timeline points={week} testid="px-ov-week" />);
    const captions = [...html.matchAll(/class="px-tl-cap"[^>]*>(.*?)<\/a>/g)].map((m) => m[1]);
    expect(captions).toEqual([
      "Staging deploy <span class=\"px-tl-sha\">1234567</span>",
      "Production deploy",
      "PR #3 opened",
    ]);
    expect(html.match(/px-tl-sha/g)).toHaveLength(1);
  });

  it("does not invent a short commit from a full commit_sha", () => {
    const week = parseOverview({
      week: [{ at: "2026-11-14T12:00:00Z", kind: "deploy", label: "Staging deploy", commit_sha: "abcdef1234567890" }],
    }).week;
    expect(week[0].label).toBe("Staging deploy");
    expect(week[0].sha7).toBeUndefined();
    const html = renderToStaticMarkup(<Timeline points={week} />);
    expect(html).toContain("Staging deploy");
    expect(html).not.toContain("abcdef1");
    expect(html).not.toContain("px-tl-sha");
  });
});
