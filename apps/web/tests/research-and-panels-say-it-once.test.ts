import { describe, expect, it } from "vitest";

import {
  briefLine, composerHref, dossierTitle, dossierVerdict, fallbackGist, meetingFromUrl, meetingOutcome,
  panelStages, prdSections, readComposerPrefill, repoOf, researchStages, seatGist, shortTitle, took,
} from "../lib/research-panels";
import { fetchMeetingRoom } from "../lib/meeting-room-detail";

/**
 * Research (dossiers) and Panels (meetings) say each thing once: the full pages, the cards and
 * the compact views inside a workflow run's step panel read titles, verdicts, stages and
 * outcomes through lib/research-panels.ts — shaped here on shared-server's real records.
 */
const SEED = "Build a lead triage inbox.\nWho it is for: a small team drowning in inbound that is mostly noise.\n"
  + "What it does: score each enquiry, draft a reply, one-click send.";

describe("titles a card can carry", () => {
  it("take the first sentence, without 'Build a' or the brief that follows", () => {
    expect(shortTitle(SEED)).toBe("Lead triage inbox");
    expect(shortTitle("PRD meeting: Build a feature flag expiry tracker. Who it is for: a team")).toBe("Feature flag expiry tracker");
    expect(shortTitle("Go/no-go on a US-equity rule-based signal screener with generated plain-English rationale"))
      .toBe("Go/no-go on a US-equity rule-based signal screener with generated plain-English…");
    expect(shortTitle("")).toBe("Untitled");
  });

  it("prefer the run's own short title to its seed", () => {
    expect(dossierTitle({ title: "Feature flag expiry tracker", seed: SEED })).toBe("Feature flag expiry tracker");
    expect(dossierTitle({ title: SEED, seed: SEED })).toBe("Lead triage inbox");
  });

  it("the hero line reads who it is for and what it does", () => {
    expect(briefLine(SEED)).toBe("For a small team drowning in inbound that is mostly noise. Score each enquiry, draft a reply, one-click send.");
    expect(briefLine("Ship the thing. Then watch it.")).toBe("Then watch it.");
  });
});

describe("a dossier's verdict and phases", () => {
  it("one verdict per state, each in one filter", () => {
    const sum = (verdict: string | null) => ({ status: "completed", summary: { verdict } });
    expect(dossierVerdict(sum("promising"))).toEqual({ label: "Promising", tone: "good", filter: "promising" });
    expect(dossierVerdict(sum("pursue")).filter).toBe("promising");
    expect(dossierVerdict(sum("needs_more_research"))).toEqual({ label: "Needs more research", tone: "warn", filter: "needs_more" });
    expect(dossierVerdict(sum("not_promising")).filter).toBe("not_worth");
    expect(dossierVerdict({ status: "failed" }).label).toBe("Failed");
    expect(dossierVerdict({ status: "running" }).tone).toBe("live");
  });

  it("phases tick while it runs and show where a failed one stopped", () => {
    const states = (status: string, phase: string) => researchStages({ status, phase }).map((s) => s.state);
    expect(states("running", "vet")).toEqual(["done", "done", "now", "todo", "todo"]);
    expect(states("running", "queued")).toEqual(["now", "todo", "todo", "todo", "todo"]);
    expect(states("failed", "shape")).toEqual(["done", "done", "done", "stopped", "todo"]);
    expect(states("completed", "done")).toEqual(["done", "done", "done", "done", "done"]);
    expect(researchStages({ status: "completed", phase: "done" }).map((s) => s.label))
      .toEqual(["Signal sweep", "Synthesize", "Critic", "Shape outcomes", "Evidence gate"]);
  });
});

describe("a panel's stages and outcome", () => {
  const done = { status: "completed", phase: "done", pull_request_url: "https://github.com/paramg/boss-sandbox/pull/185",
                 pull_request_number: 185, prd_markdown: "# PRD\n## 1. Summary\n## 2. Problem & Context\nx\n### not this\n## Risks" };
  it("Opening views → Debate → Synthesis → PRD → Build, live while it runs", () => {
    const states = (m: object) => panelStages(m as never).map((s) => `${s.label}:${s.state}`);
    expect(states(done)).toEqual(["Opening views:done", "Debate:done", "Synthesis:done", "PRD:done", "Build:now"]);
    expect(states({ status: "running", phase: "round2" }))
      .toEqual(["Opening views:done", "Debate:now", "Synthesis:todo", "PRD:todo", "Build:todo"]);
    expect(states({ status: "running", phase: "research" })[0]).toBe("Opening views:now");
    expect(states({ status: "failed", phase: "synthesis" })[2]).toBe("Synthesis:stopped");
  });

  it("says where it ended up", () => {
    expect(meetingOutcome(done as never)).toEqual({ pill: "Agreed · PRD ready", tone: "good", headline: "PRD in pull request #185", stage: "published" });
    expect(meetingOutcome({ status: "running" } as never).pill).toBe("In session");
    expect(meetingOutcome({ status: "completed", prd_markdown: "# x", prd_state: "awaiting_approval" } as never).headline)
      .toBe("PRD awaiting your approval");
    expect(repoOf(done.pull_request_url)).toBe("paramg/boss-sandbox");
    expect(prdSections(done.prd_markdown)).toEqual(["Summary", "Problem & Context", "Risks"]);
    expect(took("2026-09-12T17:15:24Z", "2026-09-12T17:15:56Z")).toBe("32s");
  });

  it("a seat's card reads the API's gist, and cuts one from an older API's markdown", () => {
    const m = { round1: { "UX Designer": "**UX – take**\n\n| a | b |\n- Score at a glance, always.\n" }, round2: {},
                round1_gists: { "UX Designer": { headline: "From the seat", points: ["p"] } } };
    expect(seatGist(m as never, 1, "UX Designer")).toEqual({ headline: "From the seat", points: ["p"] });
    expect(seatGist({ ...m, round1_gists: {} } as never, 1, "UX Designer"))
      .toEqual({ headline: "UX – take", points: ["Score at a glance, always."] });
    expect(seatGist(m as never, 2, "UX Designer")).toBeNull();
    expect(fallbackGist("```js\ncode()\n```\n# Head\nA point long enough.").headline).toBe("Head");
  });
});

describe("every way into a meeting room still lands in it", () => {
  const meetings = [{ id: "mrun_1", task_id: "tsk_1" }, { id: "mtg_2", task_id: null }];
  it("meeting=, a run id in run=, the dossier's task=, and #id", () => {
    expect(meetingFromUrl("?area=meetings&meeting=mrun_9", "", meetings)).toBe("mrun_9");
    expect(meetingFromUrl("?area=meetings&run=mrun_1", "", meetings)).toBe("mrun_1");
    expect(meetingFromUrl("?area=meetings&task=tsk_1", "", meetings)).toBe("mrun_1");
    expect(meetingFromUrl("?area=meetings", "#mtg_2", meetings)).toBe("mtg_2");
    expect(meetingFromUrl("?area=meetings&run=crun_5", "", meetings)).toBeNull();
    expect(meetingFromUrl("?area=meetings", "", meetings)).toBeNull();
  });

  it("one meeting is read by id through the same-origin proxy", async () => {
    const seen: string[] = [];
    const ok = await fetchMeetingRoom("mrun_1", "boss-dogfood", (async (url: string) => {
      seen.push(url);
      return Response.json({ id: "mrun_1", round1: {}, round2: {} });
    }) as typeof fetch);
    expect(seen).toEqual(["/v1/meeting-rooms/mrun_1?project_id=boss-dogfood"]);
    expect(ok.ok && ok.meeting.id).toBe("mrun_1");
    const missing = await fetchMeetingRoom("mrun_x", "p", (async () =>
      Response.json({ detail: "meeting not found" }, { status: 404 })) as typeof fetch);
    expect(missing).toEqual({ ok: false, error: "meeting not found" });
  });
});

describe("Build it opens the New project composer with the brief written in", () => {
  it("keeps who you are, carries the brief, and the composer reads it back", () => {
    const href = composerHref("?org=org_boss&area=discovery&run=drun_1", { title: "Lead triage inbox", brief: "Score it.\nWho it is for: agencies" });
    const q = new URLSearchParams(href.slice(1));
    expect(q.get("org")).toBe("org_boss");
    expect(q.get("area")).toBe("delivery_workflows");
    expect(q.get("compose")).toBe("new");
    expect(q.get("new")).toBe("1");
    expect(q.get("run")).toBeNull();
    expect(readComposerPrefill(href.slice(href.indexOf("?")))).toEqual({ title: "Lead triage inbox", directive: "Score it.\nWho it is for: agencies" });
    expect(readComposerPrefill("?area=delivery_workflows&new=1")).toBeNull();
  });
});
