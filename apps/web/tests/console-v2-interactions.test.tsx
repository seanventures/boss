import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { codeOnly } from "./_helpers/source";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentChat } from "../components/v2/AgentChat";
import { LaunchModal } from "../components/v2/LaunchModal";
import type { V2Agent } from "../lib/v2/workforce";

/**
 * The two interactions: talking to an agent, and starting a project.
 *
 * Both replace controls that failed in specific, recorded ways — v1's chat lost the reply the
 * instant it arrived, and the old launch path created a task that sat at `open` forever. These
 * tests are about not repeating either.
 */

/** Source WITHOUT comments. A grep for a forbidden pattern otherwise matches the comment that
 *  explains why the pattern was removed — which is exactly what happened to the assertion
 *  below about revalidatePath. */
const src = (name: string) =>
  codeOnly(resolve(__dirname, `../components/v2/${name}.tsx`));

const agent: V2Agent = {
  id: "cto", name: "CTO", role: "Cto", status: "WORKING", task: "reviewing the build",
  model: "claude-opus-5", tokens: null, spend_usd: null, calls: null,
  autonomy_level: 1, trust_score: 50, successful_runs: 3, violations: 0,
  hidden_failures: 0, last_seen_seconds: 40, run_id: "run_1",
  project: null, last_touched: null, registered: true,
};

describe("agent chat", () => {
  const html = renderToStaticMarkup(
    <AgentChat agent={agent} projectId="boss-dogfood" actor="executive_console" onClose={() => {}} />,
  );

  it("holds the transcript itself rather than relying on a re-render", () => {
    // v1's chat called revalidatePath("/"), which re-rendered the console tree underneath the
    // panel and snapped it to its empty state — question sent, "3s" ticking, then the openers
    // again, as though nothing had been asked.
    const s = src("AgentChat");
    expect(s).toMatch(/useState<Turn\[\]>/);
    expect(s).not.toMatch(/revalidatePath|useActionState/);
  });

  it("goes through a route handler", () => {
    expect(src("AgentChat")).toContain('fetch("/api/agent-chat"');
  });

  it("shows what the agent is doing, so a question can be about the real work", () => {
    expect(html).toContain('data-testid="v2-chat-context"');
    expect(html).toContain("reviewing the build");
  });

  it("has a waiting signal", () => {
    // "It says it will monitor and update and I don't see any waiting signal, it just quits"
    // was a real complaint about this system.
    expect(src("AgentChat")).toContain('data-testid="v2-chat-pending"');
    expect(src("AgentChat")).toMatch(/thinking/);
  });

  it("treats an empty reply as a failure, not a silence", () => {
    // Rendering nothing would look like the agent chose not to answer.
    expect(src("AgentChat")).toMatch(/returned an empty reply/);
  });

  it("appends the operator's own words before the request", () => {
    // What they typed must never be the thing that goes missing while they wait.
    const s = src("AgentChat");
    const appended = s.indexOf('role: "you"');
    const requested = s.indexOf('fetch("/api/agent-chat"');
    expect(appended).toBeGreaterThan(-1);
    expect(appended).toBeLessThan(requested);
  });

  it("orders turns by arrival, not by timestamp", () => {
    // A question and its answer can share a created_at — Postgres now() is transaction start —
    // and sorting on that alone put four of five answers ABOVE their own question in v1.
    const s = src("AgentChat");
    expect(s).not.toMatch(/\.sort\(/);
  });

  it("Enter sends and Shift+Enter does not", () => {
    expect(src("AgentChat")).toMatch(/e\.key === "Enter" && !e\.shiftKey/);
  });

  it("says what an empty log means", () => {
    expect(html).toContain('data-testid="v2-chat-empty"');
  });
});

describe("launch", () => {
  const html = renderToStaticMarkup(
    <LaunchModal projectId="boss-dogfood" actor="executive_console" orgId="org_boss" onClose={() => {}} />,
  );

  it("names the sequence before the click", () => {
    // An operator once clicked an idea, watched "Working", and nothing else ever happened.
    expect(html).toContain('data-testid="v2-launch-what"');
    expect(html).toMatch(/No\s+code runs until you approve it/);
  });

  it("goes to the path that actually starts the sequence", () => {
    // Not the old "assign an outcome" path, which ran one planning job and stopped — nothing
    // downstream consumes a plan, so the task sat at `open` forever.
    const handler = readFileSync(resolve(__dirname, "../app/api/launch/route.ts"), "utf8");
    expect(handler).toContain("/v1/meetings/prd");
    expect(handler).toMatch(/dedupes on \(project, directive\)/);
  });

  it("reports what it started, with a link", () => {
    // A control that cannot confirm what it started cannot report a failure either.
    expect(src("LaunchModal")).toContain('data-testid="v2-launch-link"');
    expect(src("LaunchModal")).toMatch(/Watch the panel/);
  });

  it("says so when it started something it cannot link to", () => {
    expect(src("LaunchModal")).toContain('data-testid="v2-launch-noid"');
  });

  it("reports the API's error rather than a generic one", () => {
    expect(src("LaunchModal")).toMatch(/data\.error \?\? `HTTP \$\{res\.status\}`/);
  });

  it("cannot be submitted empty", () => {
    expect(src("LaunchModal")).toMatch(/disabled=\{busy \|\| !brief\.trim\(\)\}/);
  });

  it("the handler refuses a launch with no actor", () => {
    const handler = readFileSync(resolve(__dirname, "../app/api/launch/route.ts"), "utf8");
    expect(handler).toMatch(/actor is required/);
  });
});
