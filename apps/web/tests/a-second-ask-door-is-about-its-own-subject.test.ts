// @vitest-environment happy-dom

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { dockChatKey, openDockAbout, parseSubjectRef, subjectRef } from "../lib/dock-subject";

/**
 * A second "Ask about this" press is about ITS subject, even with the dock already open.
 *
 * Review of 20b0abf (boss-1 follow-up): the shell mounted AgentChat under the key
 * `ask|role|discussing|fresh`. AgentChat keeps the subject it was mounted with, so the key is
 * the only thing that moves a conversation to a new subject. Questions are now the operator's
 * own words — "Why did the Preview step fail, and what is the fix?" is identical for every run —
 * so "Ask the Qa" on run B's Preview step, with the dock still open on run A's, produced the
 * same key, no remount, and a turn sent with run A's step as its external_ref.
 *
 * Driven through the real door helper and read back from the URL the way the shell reads it.
 */
const QUESTION = "Why did the Preview step fail, and what is the fix?";

function pressDoor(runId: string) {
  openDockAbout({
    subject: { kind: "workflow_step", id: runId, step: "preview" },
    question: QUESTION,
    role: "Qa",
  });
  const params = new URLSearchParams(window.location.search);
  // What ConsoleShell's read() takes from the URL. dock_ask is consumed by the shell, so it is
  // read here before anything else could clear it.
  return {
    agent: params.get("agent"),
    ask: params.get("dock_ask") ?? "",
    role: params.get("dock_role") ?? "",
    discussing: params.get("dock_mode") === "discuss",
    fresh: 0,
  };
}

describe("a second Ask door is about its own subject", () => {
  it("two doors with the same question and role but different subjects are two conversations", () => {
    const a = pressDoor("wfr_aaaaaaaaaaaaaaaaaaaa");
    const b = pressDoor("wfr_bbbbbbbbbbbbbbbbbbbb");
    expect(a.ask).toBe(b.ask);
    expect(a.role).toBe(b.role);
    // The external_ref each turn would carry.
    expect(a.agent).toBe("workflow_step:wfr_aaaaaaaaaaaaaaaaaaaa:preview");
    expect(b.agent).toBe("workflow_step:wfr_bbbbbbbbbbbbbbbbbbbb:preview");
    expect(subjectRef(parseSubjectRef(b.agent)!)).toBe(b.agent);
    // And the mount key differs, so AgentChat remounts holding run B's step.
    expect(dockChatKey(a)).not.toBe(dockChatKey(b));
  });

  it("the shell mounts the dock's chat under dockChatKey, with the subject in it", () => {
    const shell = readFileSync(join(process.cwd(), "app", "ConsoleShell.tsx"), "utf8");
    expect(shell).toMatch(/key=\{dockChatKey\(\{\s*agent:\s*dockedRun,/);
    expect(shell).not.toContain("key={`${dockAsk}|${dockRole}|${dockDiscussing}|${dockFresh}`}");
  });
});
