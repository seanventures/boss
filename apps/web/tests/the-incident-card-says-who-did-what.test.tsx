// @vitest-environment happy-dom
/**
 * WHO DID WHAT (D2 operations): the incident card names every part of the response — the
 * triaging agent in its own words, the investigator with its runbook, evidence and skills, the
 * build's crew with each reviewer's verdict, the regression test red then green, the person who
 * approved, and BOSS's deploy said as BOSS's (not an agent's). Before, it drew coloured dots.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IncidentCardView } from "../app/observability/incident-card";
import { parseIncidentResponse } from "../lib/incident-response";
import { INCIDENT_NOW, incidentResponseWire } from "./_helpers/incident-fixtures";
import { $, $$, click, mount, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); });

const agent = (role_key: string, role: string, skills: string[] = []) => ({ kind: "agent", role_key, role, how: "named", skills });
const RESPONDERS = [
  { step_id: "triage:alrt_1", kind: "triage", label: "Triage", job: "Decide what the alert is and what BOSS does with it",
    status: "succeeded", started_at: "2026-10-19T05:00:00Z", ended_at: "2026-10-19T05:00:20Z",
    who: { ...agent("sre", "Site Reliability Engineer"), how: "on_call", model: "claude-sonnet-5" },
    did: "Opened an incident", words: "POST /orders fails on three-line orders since release 1.2.", detail: { alert: "OrdersFailing", severity: "sev2", confidence: 0.86 } },
  { step_id: "investigate", kind: "investigate_incident", label: "Investigate the incident", job: "Find the root cause",
    status: "succeeded", started_at: "2026-10-19T05:01:00Z", ended_at: "2026-10-19T05:01:06Z",
    who: agent("sre", "Site Reliability Engineer", ["repo.read", "deployment.health.check"]),
    did: "Root cause, 90% sure", words: "Release 1a2b3c4 added shipping.py; 12× KeyError at shipping.py:14.",
    detail: { runbook: { title: "The app is serving badly", url: "boss:runbook/app-serving", kind: "builtin", markdown: "Which release is serving?" },
              evidence: [{ kind: "metrics", word: "POST /orders: 21% of 96 requests failing" },
                         { kind: "logs", word: "12× KeyError at shipping.py:14" }, { kind: "release", word: "1 commit(s), 2 file(s) changed" }] } },
  { step_id: "build", kind: "build", label: "Build, review and test", job: "Write the fix with a regression test",
    status: "succeeded", started_at: "2026-10-19T05:01:10Z", ended_at: "2026-10-19T05:06:10Z",
    who: agent("engineering_captain", "Engineering Captain"), did: "Opened the fix's pull request", words: null,
    detail: { pull_request_url: "https://github.com/acme/sandbox/pull/240",
              crew: [{ stage: "write_tests", label: "Write tests", job: "Writes the regression test first", status: "complete",
                       who: agent("qa", "QA Lead"), finding: "Wrote test_shipping.py; it fails on the faulty release", verdict: "red" },
                     { stage: "review_code", label: "Code review", job: "Tries to break the fix", status: "complete",
                       who: agent("adversarial_reviewer", "Adversarial Reviewer"), finding: "The lookup falls back safely.", verdict: "survived" }],
              tests: { files: ["order-api/tests/test_shipping.py"], red: { ran: true, failed: true, failing: ["test_three_lines"], word: "Fails on the faulty code" },
                       green: true, suite: "All 43 tests pass." } } },
  { step_id: "approve", kind: "release_approval", label: "Approve the release", job: "Approve the release", status: "succeeded",
    started_at: null, ended_at: "2026-10-19T05:08:00Z", who: { kind: "person", role_key: null, name: "Seed Owner", role: "Approver", how: "approved" },
    did: "Approved the release", words: null, detail: {} },
  { step_id: "deploy", kind: "deploy", label: "Deploy", job: "Ship the fix", status: "succeeded",
    started_at: "2026-10-19T05:08:05Z", ended_at: "2026-10-19T05:08:50Z",
    who: { kind: "boss", role_key: null, role: "BOSS's deploy", how: "run_actor", on_behalf_of: "Seed Owner" },
    did: "Deployed 9f9f9f9 · succeeded", words: null, detail: {} },
];

const view = () => parseIncidentResponse(incidentResponseWire({ responders: RESPONDERS }));

describe("who did what", () => {
  it("names each part's agent, person or BOSS, with its job and what it did", async () => {
    await mount(<IncidentCardView projectId="p1" r={view()} now={Date.parse(INCIDENT_NOW)} />);
    const rows = $$("[data-testid^='obs-responder-']").filter((e) => e.classList.contains("obs-responder"));
    expect(rows.map((r) => r.getAttribute("data-kind"))).toEqual(["triage", "investigate_incident", "build", "release_approval", "deploy"]);
    const who = (kind: string) => $(`[data-testid='obs-responder-${kind}'] [data-testid='obs-responder-who']`)!;
    expect(who("triage").textContent).toContain("Beacon");
    expect(who("triage").textContent).toContain("Site Reliability Engineer");
    expect(who("triage").getAttribute("href")).toContain("role=sre");
    expect($("[data-testid='obs-responder-triage'] [data-testid='obs-responder-words']")?.textContent).toContain("three-line orders");
    expect(who("build").textContent).toContain("Helm");
    expect(who("release_approval").textContent).toContain("Seed Owner");
    expect(who("deploy").textContent).toBe("BOSS's deployfor Seed Owner");
    expect(who("deploy").tagName).toBe("SPAN");          // BOSS has no agent page to link
    // the dots are gone once the parts are named
    expect($("[data-testid='obs-incident-steps']")).toBeNull();
  });

  it("the investigation shows its evidence, its runbook and the agent's skills", async () => {
    await mount(<IncidentCardView projectId="p1" r={view()} now={Date.parse(INCIDENT_NOW)} />);
    const ev = $$("[data-testid='obs-responder-evidence'] li").map((l) => l.getAttribute("data-kind"));
    expect(ev).toEqual(["metrics", "logs", "release"]);
    expect($("[data-testid='obs-responder-skills']")?.textContent).toBe("2 skills ↗");
    await click($("[data-testid='obs-responder-runbook']")!);
    expect($("[data-testid='obs-responder-runbook-md']")?.textContent).toContain("Which release is serving?");
  });

  it("the build names its crew with verdicts, and the test red on the faulty release then green", async () => {
    await mount(<IncidentCardView projectId="p1" r={view()} now={Date.parse(INCIDENT_NOW)} />);
    expect($("[data-testid='obs-crew-write_tests']")?.textContent).toContain("Vera");
    expect($("[data-testid='obs-crew-review_code']")?.textContent).toContain("Rook");
    expect($("[data-testid='obs-crew-review_code']")?.textContent).toContain("survived");
    expect($("[data-testid='obs-tests-red']")?.textContent).toBe("red on the faulty release");
    expect($("[data-testid='obs-tests-green']")?.textContent).toBe("green with the fix");
  });

  it("an older view without responders keeps the step dots", async () => {
    await mount(<IncidentCardView projectId="p1" r={parseIncidentResponse(incidentResponseWire())} now={Date.parse(INCIDENT_NOW)} />);
    expect($("[data-testid='obs-incident-responders']")).toBeNull();
    expect($("[data-testid='obs-incident-steps']")).not.toBeNull();
  });
});

describe("the rule builder's runbook", () => {
  it("a rule on the app's HTTP series follows BOSS's serving runbook unless a person picks another", async () => {
    const { defaultRunbook } = await import("../app/observability/rule-builder");
    expect(defaultRunbook("http_requests_total")).toBe("boss:runbook/app-serving");
    expect(defaultRunbook("http_request_duration_seconds")).toBe("boss:runbook/app-serving");
    expect(defaultRunbook("orders_created_total")).toBe("");
  });
});

describe("an alert's triage", () => {
  it("names the on-call agent apart from the model it thought with", async () => {
    const { parseAlert } = await import("../lib/app-alerts");
    const a = parseAlert({ id: "a1", triage: { state: "done", word: "Opened an incident", reason: "Release 1.2 broke it.",
                                               agent: { role_key: "sre", role: "Site Reliability Engineer" }, model: "m-1" } });
    expect(a.triage.agent).toEqual({ roleKey: "sre", role: "Site Reliability Engineer" });
    expect(a.triage.model).toBe("m-1");
    expect(parseAlert({ id: "a2", triage: { state: "done", word: "Opened an incident" } }).triage.agent).toBeNull();
  });
});
