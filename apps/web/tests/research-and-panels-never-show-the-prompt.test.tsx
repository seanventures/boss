// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildStageOf, visibleStepLinks,
} from "../app/delivery-workflows-ui";
import {
  humanBrief, meetingOutcome, panelStages, prdDigest, researchRunParam, shortTitle, voteSentence,
} from "../lib/research-panels";

/**
 * The independent walk of rp/research-panels (2026-10-05) found the panels showing their own
 * prompt — "--- RESEARCH ALREADY DONE FOR THIS --- … ## What was researched {" — in the hero,
 * cards and drawer; "Build it" prefilling the composer with that prompt; an outcome card that
 * said "PRD approved" under "Agreed · PRD drafted" beside "Publishing waits for your approval";
 * a go/no-go rendered as a PRD panel; a vote result as a JSON dump. Each is pinned here by what
 * the screen does, not by a line of source.
 */
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { MeetingsHome } = await import("../app/meeting-room-ui");

const SCAFFOLDED = "Build a small internal status page that lists services and their last check time.\n\n"
  + "--- RESEARCH ALREADY DONE FOR THIS ---\nBOSS researched this before convening you. Argue from it.\n\n"
  + "# Recovery smoke test\n\n## What was researched\n{\n \"questions\": [\n  \"Who is the user?\"\n ]\n}";
const SCAFFOLD_WORDS = ["RESEARCH ALREADY", "What was researched", "BOSS researched this", "{", "[boss:",
                        "Produce a PRD for this"];

describe("the person's words, never the prompt", () => {
  it("humanBrief takes every known scaffold off, and prefers the API's brief", () => {
    expect(humanBrief(SCAFFOLDED)).toBe("Build a small internal status page that lists services and their last check time.");
    expect(humanBrief("--- WHAT THIS PANEL IS FOR ---\nYou are the panel inside X.\n--- WHAT YOU ARE BEING ASKED ABOUT ---\n\nAdd snooze [boss:s=1].\n\n--- RESEARCH ALREADY DONE FOR THIS ---\nx"))
      .toBe("Add snooze.");
    expect(humanBrief("Produce a PRD for this evidence-backed opportunity, surfaced by BOSS Discovery research on the seed: \"x\".\n\nOUTCOME TO BUILD: Batch triage\nTARGET PILOT: agencies\n"))
      .toBe("Batch triage\nWho it is for: agencies");
    expect(humanBrief(SCAFFOLDED, "Just this")).toBe("Just this");
  });

  it("a title keeps its question mark", () => {
    expect(shortTitle("Should we ship snooze this week? More text.")).toBe("Should we ship snooze this week?");
    expect(shortTitle("PRD meeting: Build a lead triage inbox. Who it is for: x")).toBe("Lead triage inbox");
  });

  it("Build it hands over the AGREED PRD, never the directive", () => {
    const md = "# PRD\n## 1. Summary\nScore inbound enquiries and draft replies.\n## 3. Goals & Non-Goals\n- Score every enquiry\n- Nothing sends without a click\n## 5. Functional Requirements\n| a | b |\n- Batch import\n- Editable fit profile";
    const digest = prdDigest(md, "Lead triage inbox", "https://github.com/o/r/pull/185");
    expect(digest).toBe("Lead triage inbox\n\nScore inbound enquiries and draft replies.\n\nGoals:\n- Score every enquiry\n- Nothing sends without a click\n\nWhat it must do:\n- Batch import\n- Editable fit profile\n\nRead the full PRD: https://github.com/o/r/pull/185");
    for (const w of SCAFFOLD_WORDS) expect(digest).not.toContain(w);
  });
});

describe("the outcome follows the PRD's real state", () => {
  const base = { status: "completed", meeting_type: "prd_panel", prd_markdown: "# PRD" } as const;
  it.each([
    [{ prd_state: "awaiting_approval" }, "awaiting", "Agreed · PRD drafted"],
    [{ prd_state: "approved" }, "approved", "Agreed · PRD approved"],
    [{ pull_request_url: "https://github.com/o/r/pull/9", pull_request_number: 9 }, "published", "Agreed · PRD ready"],
    [{ pull_request_url: "u", build: { id: "crun_1", status: "running" } }, "building", "Building"],
    [{ status: "simulated", prd_state: "awaiting_approval" }, "simulated", "Simulated"],
    [{ status: "failed" }, "failed", "Stopped"],
  ])("%j → %s", (patch, stage, pill) => {
    const o = meetingOutcome({ ...base, ...patch } as never);
    expect([o.stage, o.pill]).toEqual([stage, pill]);
  });

  it("a go/no-go says its verdict; a stand-up says what it recorded", () => {
    expect(meetingOutcome({ status: "simulated", meeting_type: "go_no_go", verdict: "no_go" } as never).pill).toBe("NO-GO");
    expect(meetingOutcome({ status: "simulated", meeting_type: "standup", action_items: [{ title: "a" }] } as never).headline)
      .toBe("1 action recorded");
    expect(panelStages({ status: "simulated", phase: "done", meeting_type: "go_no_go" } as never).map((s) => s.label))
      .toEqual(["Evidence", "Debate", "Verdict"]);
    expect(panelStages({ status: "simulated", phase: "done", meeting_type: "standup" } as never).map((s) => s.label))
      .toEqual(["Check-ins", "Blockers", "Recap"]);
  });

  it("the Build stage is the run's build step when the panel is a workflow step", () => {
    const m = { status: "completed", phase: "done", meeting_type: "prd_panel", pull_request_url: "u" } as never;
    expect(panelStages(m, buildStageOf([{ capability: "build", status: "pending" }] as never)).at(-1)!.state).toBe("todo");
    expect(panelStages(m, buildStageOf([{ capability: "build", status: "skipped" }] as never)).at(-1)!.state).toBe("skipped");
    expect(panelStages(m, buildStageOf([{ capability: "build", status: "succeeded" }] as never)).at(-1)!.state).toBe("done");
  });
});

describe("one way in, and words not codes", () => {
  it("a panel step shows its PR once (the compact view's Open the PR)", () => {
    const links = [{ label: "open the panel" }, { label: "pull request #185" }, { label: "open the project" }];
    expect(visibleStepLinks(links, { kind: "meeting" }).map((l) => l.label)).toEqual(["open the project"]);
    expect(visibleStepLinks(links, null).map((l) => l.label)).toEqual(links.map((l) => l.label));
  });

  it("a vote reads as a sentence", () => {
    expect(voteSentence({ outcome: "approved", approve: 3, reject: 1, abstain: 1, quorum_met: true }))
      .toBe("Approved · 3 for, 1 against, 1 abstain · quorum met");
    expect(voteSentence({ outcome: "no_quorum", approve: 1, votes_cast: 1, quorum: 4, quorum_met: false }))
      .toBe("No quorum · 1 for, 0 against, 0 abstain · quorum not met (1 of 4 needed)");
  });

  it("a meeting run in run= is never taken for a dossier", () => {
    expect(researchRunParam(undefined, "mrun_1")).toBeUndefined();
    expect(researchRunParam(undefined, "crun_1")).toBeUndefined();
    expect(researchRunParam(undefined, "drun_1")).toBe("drun_1");
    expect(researchRunParam("drun_2", "mrun_1")).toBe("drun_2");
  });
});

// ── The rooms, mounted ───────────────────────────────────────────────────────────────────────

let root: Root | null = null;
let host: HTMLElement | null = null;
beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});
async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const meeting = (patch: Record<string, unknown>) => ({
  id: "mrun_1", title: "PRD meeting: Build a small internal status page that lists services and their last check time.",
  purpose: "", status: "completed", phase: "done", participants: ["Product Manager", "Facilitator / Chief of Staff"],
  created_at: "2026-10-01T10:00:00Z", completed_at: "2026-10-01T10:00:30Z", directive: SCAFFOLDED,
  round1: { "Product Manager": "## PM\n\nA point long enough." }, round2: {}, meeting_type: "prd_panel",
  prd_markdown: "# PRD\n## Summary\nA page.", prd_state: "awaiting_approval", prd_label: "awaiting approval",
  decision_id: "dec_7", can_vote: true, ...patch,
});
async function open(m: Record<string, unknown>, fetchBody: unknown = { detail: "meeting not found" }, status = 404) {
  const fetched: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (u: unknown) => { fetched.push(String(u)); return Response.json(fetchBody, { status }); }));
  window.history.replaceState(null, "", `/?area=meetings&meeting=${m.id}`);
  act(() => root!.render(<MeetingsHome projectId="p" actor="ceo" meetings={[m] as never} roles={[]} tasks={[]} apiReachable />));
  await settle();
  return fetched;
}

describe("the meeting room", () => {
  it("shows the ask, never the scaffolding, anywhere on the page", async () => {
    await open(meeting({}));
    const text = host!.textContent ?? "";
    for (const w of SCAFFOLD_WORDS) expect(text).not.toContain(w);
    expect(text).toContain("Small internal status page that lists services and their last check time");
  });

  it("an awaiting PRD sends you to ITS decision, once", async () => {
    await open(meeting({}));
    const review = host!.querySelector<HTMLAnchorElement>('[data-testid="meeting-review"]')!;
    expect(new URLSearchParams(review.getAttribute("href")!.slice(1)).get("decision")).toBe("dec_7");
    expect(host!.querySelector('[data-testid="meeting-outcome"]')!.textContent).not.toContain("approved");
  });

  it("a go/no-go shows its verdict and dissent, and no PRD or Approvals", async () => {
    await open(meeting({ meeting_type: "go_no_go", status: "simulated", verdict: "go", decision_id: null,
      dissent: [{ role: "QA / Test", position: "no-go", reason: "Flake open" }], prd_markdown: "# Simulated go/no-go minutes" }));
    expect(host!.querySelector('[data-testid="meeting-verdict"]')!.textContent).toBe("GO");
    expect(host!.textContent).toContain("Flake open");
    expect(host!.querySelector('[data-testid="meeting-prd"]')).toBeNull();
    expect(host!.querySelector('[data-testid="meeting-review"]')).toBeNull();
  });

  it("an opened PRD reads at the page's width, in its own row", async () => {
    await open(meeting({ prd_markdown: "# PRD — HelpDesk AI\n\n## 1. Summary\n\nTickets and roles." }));
    const side = host!.querySelector('[data-testid="meeting-prd"]')!;
    const read = [...side.querySelectorAll("button")].find((b) => b.textContent?.includes("Read the PRD"))!;
    await act(async () => { read.click(); });
    const wide = host!.querySelector('[data-testid="meeting-prd-full"]')!;
    expect(wide).not.toBeNull();
    expect(wide.classList.contains("rp-prd-wide")).toBe(true);
    expect(wide.textContent).toContain("Tickets and roles.");
    // The narrow column keeps its outline only; the document itself is not drawn twice.
    expect(side.textContent).not.toContain("Tickets and roles.");
  });

  it("a failed panel says why and offers to convene it again", async () => {
    await open(meeting({ status: "failed", phase: "synthesis", error: "The budget cap was reached." }));
    expect(host!.querySelector('[data-testid="meeting-error"]')!.textContent).toContain("The budget cap was reached.");
    expect(host!.querySelector('[data-testid="meeting-convene-again"]')).not.toBeNull();
  });

  it("the drawer keeps ids and codes behind Copy id", async () => {
    await open(meeting({ context: "### Research already done for this\n\n- **Questions**\n  - Who?" }));
    const drawer = host!.querySelector('[data-testid="meeting-drawer"]')!.textContent ?? "";
    expect(drawer).not.toMatch(/mrun_|dec_|art_|prd_panel/);
    expect(drawer).toContain("Context BOSS added");
    expect(host!.querySelector('[data-testid="rp-copy-id"]')).not.toBeNull();
  });

  it("a panel that is not there is said once, and not polled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const fetched = await open({ id: "mrun_gone" } as never);
      // Not in the list: the room asks for it by id… (the listed stub here has no status)
      window.history.replaceState(null, "", "/?area=meetings&meeting=mrun_missing");
      act(() => root!.render(<MeetingsHome projectId="p" actor="ceo" meetings={[]} roles={[]} tasks={[]} apiReachable />));
      window.dispatchEvent(new PopStateEvent("popstate"));
      await settle();
      const before = fetched.filter((u) => u.includes("mrun_missing")).length;
      await act(async () => { vi.advanceTimersByTime(20_000); });
      await settle();
      expect(before).toBe(1);
      expect(fetched.filter((u) => u.includes("mrun_missing")).length).toBe(1);
      expect(host!.querySelector('[data-testid="meeting-not-found"]')!.textContent).toContain("isn't here any more");
    } finally {
      vi.useRealTimers();
    }
  });

  it("task= finds the panel held for that task when it is not on this page", async () => {
    vi.stubGlobal("fetch", vi.fn(async (u: unknown) => Response.json(
      String(u).includes("task_id=tsk_9") ? { items: [meeting({ id: "mrun_old", task_id: "tsk_9" })] } : { detail: "x" },
      { status: String(u).includes("task_id=tsk_9") ? 200 : 404 })));
    window.history.replaceState(null, "", "/?area=meetings&task=tsk_9");
    act(() => root!.render(<MeetingsHome projectId="p" actor="ceo" meetings={[]} roles={[]} tasks={[]} apiReachable />));
    await settle();
    expect(host!.querySelector('[data-testid="meeting-room"]')!.getAttribute("data-status")).toBe("completed");
  });
});

// ── The re-check (f923b20c) ──────────────────────────────────────────────────────────────────
import { dissentLine } from "../lib/research-panels";

describe("the re-check of f923b20c", () => {
  it("a vote's dissent never names a machine id", () => {
    expect(dissentLine({ role: "r0", reason: "voted to reject" })).toBe("Voted to reject");
    expect(dissentLine({ role: "panel", reason: "1 seat voted to reject" })).toBe("1 seat voted to reject");
    expect(dissentLine({ role: "QA / Test", reason: "Flake open" })).toBe("QA / Test: Flake open");
  });

  it("the room's Build and PRD stages say what happened, not what might", () => {
    const done = { status: "completed", phase: "done", meeting_type: "prd_panel" };
    const stages = (m: object) => Object.fromEntries(panelStages(m as never).map((s) => [s.key, s.state]));
    expect(stages({ ...done, prd_markdown: "# PRD", pull_request_url: "u" })).toMatchObject({ prd: "done", build: "todo" });
    expect(stages({ ...done })).toMatchObject({ prd: "skipped", build: "todo" });
    expect(stages({ ...done, prd_markdown: "# P", build: { id: "c", status: "running" } })).toMatchObject({ build: "now" });
  });

  it("the kind is not repeated in the title", () => {
    expect(shortTitle("Go/No-Go: Should we ship snooze this week?")).toBe("Should we ship snooze this week?");
    expect(shortTitle("Standup: Daily blockers")).toBe("Daily blockers");
  });

  it("Panels home: attach-to titles, server totals, and a vote with nothing to vote on", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({}, { status: 404 })));
    window.history.replaceState(null, "", "/?area=meetings");
    act(() => root!.render(
      <MeetingsHome projectId="p" actor="ceo" meetings={[meeting({ can_vote: false })] as never}
                    totals={{ panels: 140, prds: 96 }} roles={[]} apiReachable
                    tasks={[{ id: "t1", title: `PRD meeting: ${SCAFFOLDED}` }]} />));
    await settle();
    const options = [...host!.querySelectorAll('[data-testid="meeting-task-select"] option')].map((o) => o.textContent);
    expect(options[1]).toMatch(/^Small internal status page that lists/);
    for (const w of SCAFFOLD_WORDS) expect(options[1]).not.toContain(w);
    expect(host!.querySelector('[data-testid="panels-counts"]')!.textContent).toContain("140 panels, 96 became PRDs");
    act(() => (host!.querySelector('[data-testid="maint-vote"]') as HTMLButtonElement).click());
    expect(host!.querySelector('[data-testid="maint-vote-none"]')!.textContent).toContain("nothing to vote on");
  });

  it("a panel with no task does not offer a linked task in the drawer's summary", async () => {
    await open(meeting({ task_id: null }));
    expect(host!.querySelector('[data-testid="meeting-drawer"] > summary')!.textContent).not.toContain("Linked task");
  });
});
