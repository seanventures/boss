// @vitest-environment happy-dom
/**
 * THE PROJECT PAGE RIGHT AFTER CREATE (BOSSAPP-946, findings 4 and 5).
 *
 * 4. A project whose start was refused after it was kept (the consent sheet cancelled, a reload)
 *    shows "Not started yet" with ONE Start, which sends POST …/add-project/start; a consent still
 *    owed opens the consent sheet (withConsent) — never a half-made project nothing can resume.
 * 5. The PRD card's tag is the PRD's own state from the engine: "drafting" while the panel meets
 *    (even though the meeting step is "waiting"), "waiting for you" only once the PRD exists, and
 *    "approved" after — never back to "drafting".
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { StartedCards } from "../app/projects/started-cards";
import type { PxCtx } from "../app/projects/project-page";
import { click, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

const SEED = { kind: "idea", words: "A help desk", brief: { title: "HelpDesk" }, picks: {}, created_at: null };
const ctx = { projectId: "hd", work: null, overview: { recent_work: [] }, open: vi.fn(), tab: vi.fn(),
              refreshWork: vi.fn() } as unknown as PxCtx;

afterEach(() => unmount());

describe("a project made but not started", () => {
  it("offers one Start, which sends the kept start", async () => {
    let started = false;
    const calls = stubFetch((c) => {
      if (c.method === "GET" && c.path === "/v1/projects/hd/add-project/started") {
        return { body: started
          ? { seed: SEED, run: { id: "wfr_1", status: "running" }, nodes: [], now: null, workflow: null, prd: null }
          : { seed: SEED, run: null, nodes: [], now: null, workflow: null, prd: null,
              not_started: { resumable: true, reason: "consent_required", words: "Made, not started: it waits for your consent to the agents. Start sends it again." } } };
      }
      if (c.method === "POST" && c.path === "/v1/projects/hd/add-project/start") {
        started = true;
        return { body: { created: true, project_id: "hd", started: { run_id: "wfr_1" } } };
      }
      return undefined;
    });
    await mountAs(<StartedCards ctx={ctx} />);
    expect(tid("px-started-not-started-words")?.textContent).toContain("consent");
    await click(tid("px-started-start"));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual(["/v1/projects/hd/add-project/start"]);
    expect(tid("px-started-not-started")).toBeNull();
  });
});

describe("the PRD card reads the PRD's own state", () => {
  const view = (prd: string, approve: string) => ({
    seed: SEED, run: { id: "wfr_1", status: "running" },
    nodes: [{ key: "plan", label: "Plan", type: "agent", state: prd === "drafting" ? "now" : "done" },
            { key: "approve", label: "You approve", type: "you", state: approve }],
    now: null, workflow: null, prd: { state: prd, decision_id: prd === "drafting" ? null : "dec_prd" },
  });
  for (const [prd, approve, word] of [["drafting", "todo", "drafting"], ["waiting", "you", "waiting for you"],
                                      ["approved", "done", "approved"]] as const) {
    it(`${prd}: "${word}"`, async () => {
      stubFetch((c) => (c.method === "GET" ? { body: view(prd, approve) } : undefined));
      await mountAs(<StartedCards ctx={ctx} />);
      const card = tid("px-started-prd")!;
      expect(card.querySelector(".px-tag")?.textContent).toBe(word);
      expect(card.textContent?.includes("Approve PRD")).toBe(prd === "waiting");
    });
  }
});
