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
  steps: [],
  latency_ms: 0,
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

  describe("what an attempt actually did", () => {
    const withSteps = () =>
      html(ws({
        runs: [run({
          id: "crun_s",
          status: "failed",
          latency_ms: 12_400,
          steps: [
            { stage: "spec", model: "claude-opus-5", engine: "claude_code", tokens: 4000,
              spend_usd: 0.25, latency_ms: 3_000, calls: 1, ok: true },
            { stage: "implement", model: "claude-sonnet-5", engine: "claude_code", tokens: 9000,
              spend_usd: 0.9, latency_ms: 9_400, calls: 3, ok: false },
          ],
        })],
      }));

    it("lists the steps the attempt ran", () => {
      // "attempt 3 — failed" and nothing else leaves reading logs as the only next move.
      const out = withSteps();
      expect(out).toContain('data-testid="v2-run-steps-crun_s"');
      expect(out).toMatch(/spec/);
      expect(out).toMatch(/implement/);
    });

    it("says which model ran each step", () => {
      const out = withSteps();
      expect(out).toContain("claude-opus-5");
      expect(out).toContain("claude-sonnet-5");
    });

    it("shows latency per step and for the whole attempt", () => {
      // What separates a slow model from a slow pipeline: a run that took ten minutes with
      // nine seconds of model time was not waiting on the model.
      const out = withSteps();
      expect(out).toContain('data-testid="v2-run-latency-crun_s"');
      expect(out).toMatch(/12\.4s/);
      expect(out).toMatch(/9\.4s/);
    });

    it("marks the step that failed, not the whole run", () => {
      // A run that failed at one step and succeeded at nine others is a different story.
      expect(withSteps()).toContain('data-testid="v2-step-failed-crun_s-1"');
      expect(withSteps()).not.toContain('data-testid="v2-step-failed-crun_s-0"');
    });

    it("shows no step list when nothing was recorded", () => {
      expect(html(ws({ runs: [run({ id: "crun_bare" })] })))
        .not.toContain('data-testid="v2-run-steps-crun_bare"');
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

// ---------------------------------------------------------------------------
// The governed pipeline's own record of an attempt
//
// v1 showed the quality checks, who ran them, and what the reviewers wrote. v2 rendered
// "attempt 1 · crun_…" and stopped, leaving reading logs as the only next move.
// ---------------------------------------------------------------------------
describe("what the pipeline checked", () => {
  const gate = (label: string, ok: boolean | null, skipped = false, note: string | null = null) =>
    ({ label, ok, skipped, note });

  it("shows a check that did not run as neither passed nor failed", () => {
    // The distinction the whole panel rests on. A blank or a green tick against an unrun check
    // claims evidence nobody gathered; a red cross claims a failure that never happened.
    const out = html(ws({ runs: [run({
      gates: [gate("Security clean", null, true)],
    })] }));
    expect(out).toMatch(/data-tone="skip"/);
    expect(out).toContain("not run");
    expect(out).not.toMatch(/data-tone="skip"[^>]*>\s*<span[^>]*>✓/);
  });

  it("distinguishes a failed check from an unrun one", () => {
    const out = html(ws({ runs: [run({
      gates: [gate("Consistency", false, false, "drift")],
    })] }));
    expect(out).toMatch(/data-tone="bad"/);
    expect(out).toContain("drift");
    expect(out).not.toMatch(/data-tone="skip"/);
  });

  it("says when failing checks were advisory and did not block the PR", () => {
    // A green header above a red row is unreadable unless you say which one won.
    const withAdvisory = html(ws({ runs: [run({
      advisory: true, gates: [gate("Consistency", false)],
    })] }));
    expect(withAdvisory).toContain("advisory");
    expect(withAdvisory).toMatch(/data-tone="warn"/);

    const without = html(ws({ runs: [run({ gates: [gate("Consistency", false)] })] }));
    expect(without).not.toContain("advisory");
    expect(without).toMatch(/data-tone="bad"/);
  });

  it("states whether the same model built and reviewed the change", () => {
    const same = html(ws({ runs: [run({ provenance: {
      build_engine: "claude_code", build_model: "claude-sonnet-5",
      review_engine: "claude_code", review_model: "claude-sonnet-5",
      cross_model: false, reviewer_independent: true, test_author_independent: false,
      profile: "balanced", risk_tier: 0,
    } })] }));
    expect(same).toContain("same model builds + reviews");
    expect(same).toContain("balanced");
    expect(same).toContain("risk tier");

    const cross = html(ws({ runs: [run({ provenance: {
      build_engine: "claude_code", build_model: "claude-sonnet-5",
      review_engine: "codex", review_model: "gpt-5",
      cross_model: true, reviewer_independent: true, test_author_independent: true,
      profile: "balanced", risk_tier: 0,
    } })] }));
    expect(cross).toContain("cross-model review");
    expect(cross).not.toContain("same model builds");
  });

  it("renders reviewer prose as markdown, not as raw asterisks", () => {
    const out = html(ws({ runs: [run({ evidence: [
      { key: "review_summary", title: "Adversarial reviewer findings",
        text: "**Verified** by running the toolchain.", truncated: false },
    ] })] }));
    expect(out).toContain("<strong>Verified</strong>");
    expect(out).not.toMatch(/\*\*Verified\*\*/);
  });

  it("says when a reviewer summary was clipped", () => {
    const out = html(ws({ runs: [run({ evidence: [
      { key: "review_summary", title: "Review", text: "Long.", truncated: true },
    ] })] }));
    expect(out).toContain("summary continues");
  });

  it("shows step timing with a total", () => {
    const out = html(ws({ runs: [run({ phase_timings: [
      { phase: "Cloning repo", ms: 6_000 },
      { phase: "Engineer implementing", ms: 1_803_000 },
    ] })] }));
    expect(out).toContain("Cloning repo");
    expect(out).toContain("30m 3s");          // read as minutes, not 1803.0s
    expect(out).toContain("Total");
  });

  it("shows the error text on a failed step rather than the word failed", () => {
    const out = html(ws({ runs: [run({ steps: [{
      stage: "consistency_check", model: "claude_code", engine: "claude_code",
      tokens: 0, spend_usd: 0, latency_ms: 421_000, turns: 0, retries: 0,
      ok: false, error: "claude code agent timed out after 420s",
    }] })] }));
    expect(out).toContain("timed out after 420s");
  });

  it("shows the acceptance criteria the work was accepted against", () => {
    const out = html(ws({
      criteria: ["Invoices can be created"],
      verification: ["npx playwright test passes"],
    }));
    expect(out).toContain("Acceptance criteria");
    expect(out).toContain("Invoices can be created");
    expect(out).toContain("npx playwright test passes");
  });

  it("draws no evidence panel at all when the pipeline recorded nothing", () => {
    // An empty panel titled "Steps & quality checks" reads as a pipeline that checked nothing.
    const out = html(ws({ runs: [run({})] }));
    expect(out).not.toContain("v2-run-evidence");
    expect(out).not.toContain("Acceptance criteria");
  });
});

// ---------------------------------------------------------------------------
// Saying what happened, who did it, and where it runs
// ---------------------------------------------------------------------------
describe("explaining an attempt", () => {
  it("shows a waiting attempt as waiting, not as broken", () => {
    const out = html(ws({ runs: [run({
      status: "blocked",
      narrative: {
        outcome: "Stopped at “Awaiting plan approval”. It got through standards, plan first.",
        why: "It finished planning and stopped for a human to approve the plan. Nothing failed — it is waiting on you.",
        waiting: true, phase_label: "Awaiting plan approval",
      },
    })] }));
    expect(out).toContain('data-waiting="true"');
    expect(out).toContain("waiting on you");
  });

  it("does not dress a real failure up as a pause", () => {
    const out = html(ws({ runs: [run({
      status: "failed",
      narrative: { outcome: "Failed during Engineer implementing.", waiting: false,
                   phase_label: "Engineer implementing" },
    })] }));
    expect(out).toContain('data-waiting="false"');
    expect(out).toContain("Failed during");
    expect(out).not.toContain("v2-narrative-why");
  });

  it("names the agent that owns each step", () => {
    const out = html(ws({ runs: [run({ steps: [{
      stage: "security_check", model: "claude-sonnet-5", engine: "claude_code",
      tokens: 0, spend_usd: 0, latency_ms: 1000, turns: 4, retries: 0, ok: true, error: null,
      agent: { role: "security_reviewer", registered: true, calls: 20, ok_calls: 19,
               success_rate: 0.95 },
    }] })] }));
    expect(out).toContain("security_reviewer");
    expect(out).toContain("95% of 20");        // the rate carries its denominator
  });

  it("flags a role the org registry does not contain", () => {
    // Real on the live deployment: BOSS attributes work to "reviewer" while the registry holds
    // "adversarial_reviewer", so that spend can never be matched to an agent.
    const out = html(ws({ runs: [run({ steps: [{
      stage: "review_code", model: "claude-sonnet-5", engine: "claude_code",
      tokens: 0, spend_usd: 0, latency_ms: 1000, turns: 4, retries: 0, ok: true, error: null,
      agent: { role: "reviewer", registered: false, calls: 5, ok_calls: 5, success_rate: 1 },
    }] })] }));
    expect(out).toContain("unregistered");
    expect(out).toContain('data-registered="false"');
  });

  it("shows no rate at all for a role that has never run", () => {
    // 100% of nothing is the most misleading number the screen could print.
    const out = html(ws({ runs: [run({ steps: [{
      stage: "write_tests", model: null, engine: null,
      tokens: 0, spend_usd: 0, latency_ms: 0, turns: 0, retries: 0, ok: true, error: null,
      agent: { role: "qa", registered: true, calls: 0, ok_calls: 0, success_rate: null },
    }] })] }));
    expect(out).toContain("qa");
    expect(out).not.toMatch(/%\s*of\s*0/);
  });
});

describe("where the app runs", () => {
  const dep = (over = {}) => ({
    target_id: "deptgt_1", environment: "staging", target_ref: "root@2.29.2.202:22",
    status: "active", url: "http://2.29.2.202:3013", domains: ["2.29.2.202:3013"],
    last_deploy: { status: "succeeded", commit: "19d5806", finished_at: null,
                   failure_reason: null },
    ...over,
  });

  it("links to the running app", () => {
    const out = html(ws({ deployment: dep() }));
    expect(out).toContain('href="http://2.29.2.202:3013"');
    expect(out).toContain("last deploy succeeded");
    expect(out).toContain("19d5806");
  });

  it("shows why the last deploy failed rather than just that it did", () => {
    const out = html(ws({ deployment: dep({
      last_deploy: { status: "failed", commit: null, finished_at: null,
                     failure_reason: "port 3014 already bound" } }) }));
    expect(out).toContain("port 3014 already bound");
  });

  it("renders no link when the target records no address", () => {
    // A broken link is worse than none.
    const out = html(ws({ deployment: dep({ url: null, domains: [] }) }));
    expect(out).toContain("no address recorded");
    expect(out).not.toContain('data-testid="v2-deploy-url"');
  });

  it("says nothing at all when the app is not deployed", () => {
    // "Not deployed" and "deployed somewhere I cannot find" must not render alike.
    expect(html(ws({}))).not.toContain('data-testid="v2-deployment"');
  });
});
