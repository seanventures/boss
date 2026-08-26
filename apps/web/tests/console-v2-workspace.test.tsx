import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { WorkspaceScreen } from "../components/v2/WorkspaceScreen";
import type { V2Run, Workspace } from "../lib/v2/workspace";

/**
 * The v2 project workspace.
 *
 * The property under test throughout is that the screen never states something it does not
 * know. Three different empty states render as an empty page and mean opposite things, and
 * this console has shipped each confusion at least once: an outage read as an empty company,
 * a deployed app read as never deployed, a disabled button with no reason beside it.
 */

const run = (over: Partial<V2Run> = {}): V2Run => ({
  id: "crun_1",
  status: "completed",
  phase: "done",
  attempt: 1,
  turns: 12,
  spend_usd: 1.25,
  pull_request_number: null,
  pull_request_url: null,
  error: null,
  created_at: "2026-08-26T00:00:00Z",
  ...over,
});

const ws = (over: Partial<Workspace> = {}): Workspace => ({
  found: true,
  app_id: "app_1",
  project_id: "boss-dogfood",
  title: "Shorty",
  description: "A self-hosted URL shortener",
  repo: "paramg/boss-sandbox",
  stage: "SHIPPED",
  status: "DONE",
  pct: 100,
  spend_usd: 4.5,
  runs: [run()],
  deployable: true,
  deployable_reason: "",
  latest_run_id: "crun_1",
  phases: [
    { id: "DISCOVERY", reached: true, label: "Discovery" },
    { id: "BUILD", reached: true, label: "Build" },
    { id: "REVIEW", reached: true, label: "Review" },
    { id: "SHIPPED", reached: true, label: "Shipped" },
  ],
  ...over,
});

const html = (w: Workspace) => renderToStaticMarkup(<WorkspaceScreen workspace={w} orgId="org_boss" />);

describe("v2 project workspace", () => {
  describe("the three empty states are distinguishable", () => {
    it("an unreachable control plane says so, and disclaims the page", () => {
      const out = html(ws({ found: false, blockedReason: "API returned 503" }));
      expect(out).toContain('data-testid="v2-workspace-unreachable"');
      expect(out).toContain("503");
      // Must not be readable as a statement about the app.
      expect(out).toMatch(/Nothing here is a statement about this app/i);
    });

    it("an unknown app names the id rather than showing a blank page", () => {
      const out = html(ws({ found: false, app_id: "app_gone" }));
      expect(out).toContain('data-testid="v2-workspace-missing"');
      expect(out).toContain("app_gone");
    });

    it("a real app with no runs says exactly that", () => {
      const out = html(ws({ runs: [] }));
      expect(out).toContain('data-testid="v2-workspace-no-runs"');
      expect(out).toMatch(/nothing has run yet/i);
    });

    it("the three states are different testids, not one empty div", () => {
      const ids = [
        html(ws({ found: false, blockedReason: "boom" })),
        html(ws({ found: false })),
        html(ws({ runs: [] })),
      ].map((h) => h.match(/data-testid="(v2-workspace-[a-z-]+)"/)![1]);
      expect(new Set(ids).size).toBe(3);
    });
  });

  describe("what it says about shipping", () => {
    it("says why it cannot ship, not just that it cannot", () => {
      // A disabled control with no reason is what sends people to the API to find out.
      const out = html(ws({
        deployable: false,
        deployable_reason: "nothing reviewed yet — a run must complete and open a PR before it can ship",
      }));
      expect(out).toContain('data-deployable="false"');
      expect(out).toMatch(/nothing reviewed yet/);
    });

    it("says plainly when it is ready", () => {
      expect(html(ws({ deployable: true }))).toMatch(/ready to ship/i);
    });
  });

  describe("the lineage, not the latest run", () => {
    it("lists every attempt", () => {
      const out = html(ws({
        runs: [run({ id: "crun_3", attempt: 3 }), run({ id: "crun_2", attempt: 2 }), run({ id: "crun_1" })],
      }));
      for (const id of ["crun_3", "crun_2", "crun_1"]) {
        expect(out).toContain(`data-testid="v2-run-${id}"`);
      }
    });

    it("shows the lineage total spend, which is more than any one attempt", () => {
      // Every retry cost money; showing only the last understates the app by however many it
      // took to get there.
      const out = html(ws({ spend_usd: 4.5, runs: [run({ spend_usd: 1.25 })] }));
      expect(out).toMatch(/\$4\.50/);
    });

    it("a failed attempt carries its reason", () => {
      // "failed" with no text is a dead end — the only move left is to go and read logs.
      const out = html(ws({
        runs: [run({ id: "crun_x", status: "failed", error: "health gate rejected: /healthz returned 502" })],
      }));
      expect(out).toContain('data-testid="v2-run-error-crun_x"');
      expect(out).toContain("502");
    });

    it("a successful attempt shows no error slot at all", () => {
      expect(html(ws({ runs: [run({ id: "crun_ok" })] })))
        .not.toContain('data-testid="v2-run-error-crun_ok"');
    });
  });

  describe("links", () => {
    it("a PR links to the URL the run recorded", () => {
      // A card once linked to /pull/111 for a PR that did not exist, because the number was
      // used to build a URL rather than the URL the run actually stored.
      const out = html(ws({
        runs: [run({
          id: "crun_pr",
          pull_request_number: 111,
          pull_request_url: "https://github.com/paramg/boss-sandbox/pull/111",
        })],
      }));
      expect(out).toContain('href="https://github.com/paramg/boss-sandbox/pull/111"');
      expect(out).toContain("#111");
    });

    it("a PR number with no URL is shown but not linked", () => {
      const out = html(ws({ runs: [run({ id: "crun_np", pull_request_number: 9, pull_request_url: null })] }));
      expect(out).toContain("#9");
      expect(out).not.toContain('data-testid="v2-run-pr-crun_np"');
    });

    it("the way back keeps the org", () => {
      // Losing the org on a back link is how an operator gets thrown into another company's
      // data, or into the org picker.
      expect(html(ws())).toContain('href="/v2?org=org_boss"');
    });
  });

  describe("progress", () => {
    it("marks how far the app actually got", () => {
      const out = html(ws({
        phases: [
          { id: "DISCOVERY", reached: true, label: "Discovery" },
          { id: "BUILD", reached: true, label: "Build" },
          { id: "REVIEW", reached: false, label: "Review" },
          { id: "SHIPPED", reached: false, label: "Shipped" },
        ],
      }));
      expect(out).toMatch(/data-testid="v2-phase-BUILD"[^>]*/);
      const review = out.match(/<li[^>]*data-testid="v2-phase-REVIEW"[^>]*>/)![0];
      expect(review).toContain('data-reached="false"');
    });

    it("status is a word as well as a colour", () => {
      // Colour alone does not survive greyscale or a colourblind reader.
      expect(html(ws({ status: "STUCK" }))).toMatch(/STUCK/);
    });
  });
});
