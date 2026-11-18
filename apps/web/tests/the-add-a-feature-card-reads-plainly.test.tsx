// @vitest-environment happy-dom
/**
 * The new project's "Add a feature" card (stranger test r2, 2026-11-17): it rendered a
 * struck-through "Workflows › New build › Add a feature" and unstyled paragraphs as large body
 * text. Now: a heading, one styled line at the card's meta size, and the New work button.
 * Breaks if the card regains an <s>, an unstyled paragraph, or loses its button.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StartedCards } from "../app/projects/started-cards";
import type { PxCtx } from "../app/projects/project-page";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { parseOverview } from "../lib/project-experience";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

afterEach(() => unmount());

describe("the Add a feature card", () => {
  it("is a heading, one styled line and New work", async () => {
    const ctx = { projectId: "hd", work: null, open: vi.fn(), tab: vi.fn(), refreshWork: vi.fn(),
                  overview: parseOverview(FX_OVERVIEW) } as unknown as PxCtx;
    stubFetch((c) => (c.method === "GET" ? { body: {
      seed: { kind: "idea", words: "a desk", brief: {}, picks: {}, created_at: null },
      run: { id: "wfr_1", status: "running" }, nodes: [], now: null, workflow: null,
      prd: { state: "drafting", decision_id: null } } } : undefined));
    await mountAs(<StartedCards ctx={ctx} />);
    const card = tid("px-started-add")!;
    expect(card).not.toBeNull();
    expect(card.querySelector("h2")?.textContent).toBe("Add a feature");
    expect(card.querySelector("s, del, strike")).toBeNull();
    for (const p of card.querySelectorAll("p")) expect(p.className, p.textContent ?? "").toContain("px-st-add-why");
    expect(tid("px-started-add-why")?.textContent).toMatch(/^Builds onto the app’s branch/);
    expect(tid("px-started-new-work")?.textContent).toContain("New work");
    const css = readFileSync(join(__dirname, "../app/projects/started.css"), "utf8");
    expect(css).toMatch(/\.px-st-add-why \{[^}]*font-size: var\(--fs-meta\)/);
  });
});
