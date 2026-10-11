// @vitest-environment happy-dom
import React, { act, useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

const actions = vi.hoisted(() => ({ propose: vi.fn(), start: vi.fn() }));
vi.mock("../app/actions", () => ({
  submitCodeTaskFeedbackAction: (...args: unknown[]) => actions.propose(...args),
  confirmCodeTaskFeedbackAction: (...args: unknown[]) => actions.start(...args),
  conveneMeetingAction: vi.fn(),
}));
vi.mock("../components/identity/identity-provider", () => ({
  useCan: () => ({ allowed: true }),
}));
import { ResultReview, type ResultReviewData } from "../app/factory/result-review";
import { ChangeIt } from "../app/factory/change-it";

const fixture = (): ResultReviewData => ({
  snapshot_hash: "a".repeat(64), reviewable: true, unavailable_reason: null,
  snapshot: {
    delivered_head: "b".repeat(40), pull_request_url: "https://example.test/owned/repo/pull/1",
    receipt_id: "receipt_owned_fixture", criteria: ["Export selected rows"],
    checks: { state: "invalidated", checked_head: "c".repeat(40), artifact_id: "artifact_owned_fixture",
      quality_status: "fail", gates_failed: ["tests_passed"], commands: ["pytest owned"] },
    cost: { observed_usd_micros: null, estimated_usd_micros: 80000, held_usd_micros: 80000,
      confidence: "incomplete", unresolved_records: 1, included_calls: 1, included_equivalent_usd_micros: 70000 },
  }, history: [],
});
let current: ResultReviewData;
let posts: Record<string, unknown>[];
let responseStatus: number;
let change: ReturnType<typeof vi.fn>;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  current = fixture(); posts = []; responseStatus = 200; change = vi.fn();
  actions.propose.mockReset(); actions.start.mockReset();
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (String(url).includes("delivery-workflows")) return Response.json({ items: [] });
    if (init?.method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      return Response.json(responseStatus === 200 ? { id: "review_owned_fixture" } : { detail: "The evidence changed. Reload the result." }, { status: responseStatus });
    }
    return Response.json(current);
  }));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });
const button = (label: string) => [...document.querySelectorAll("button")].find(b => b.textContent === label) ?? null;
async function typeNote(text: string) {
  await act(async () => {
    const input = document.querySelector("textarea")!;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await settle();
}

it("shows pinned revision, failed or invalidated evidence, unknown cost and holds", async () => {
  await mount(<ResultReview runId="run_owned" onChangeRequested={change} />);
  expect(document.body.textContent).toContain("invalidated");
  expect(document.body.textContent).toContain("Observed cost: Unknown");
  expect(document.body.textContent).toContain("Outstanding holds: $0.08");
  expect(document.body.textContent).toContain("Included value: $0.07");
  expect(document.querySelector("a")!.href).toBe("https://example.test/owned/repo/pull/1/commits/" + "b".repeat(40));
  expect(posts).toHaveLength(0);
});

it("records usefulness only and retries an uncertain submission with the same identity", async () => {
  responseStatus = 503;
  await mount(<ResultReview runId="run_owned" onChangeRequested={change} />);
  await click(button("Accept as useful"));
  expect($("[role=alert]")!.textContent).toContain("evidence changed");
  responseStatus = 200;
  await click(button("Accept as useful"));
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[0].snapshot_hash).toBe(current.snapshot_hash);
  expect(posts[0].outcome).toBe("accepted");
  expect(change).not.toHaveBeenCalled();
  expect(actions.start).not.toHaveBeenCalled();
  expect(document.body.textContent).toContain("Accepted as useful for this recorded revision");
});

it("two immediate presses wait for one pending review", async () => {
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") { posts.push(JSON.parse(String(init.body))); return pending; }
    return Response.json(current);
  }));
  await mount(<ResultReview runId="run_owned" onChangeRequested={change} />);
  await act(async () => {
    button("Accept as useful")!.click();
    button("Accept as useful")!.click();
  });
  expect(posts).toHaveLength(1);
  await act(async () => { finish(Response.json({ id: "review_owned_fixture" })); });
  await settle();
  expect(document.body.textContent).toContain("Accepted as useful for this recorded revision");
});

it("keeps a stale submission visible, retains the note and reloads before another review", async () => {
  responseStatus = 409;
  await mount(<ResultReview runId="run_owned" onChangeRequested={change} />);
  await typeNote("Only add the CSV header");
  await click(button("Request a change"));
  expect(posts).toHaveLength(1);
  expect(change).not.toHaveBeenCalled();
  expect(document.querySelector("textarea")!.value).toBe("Only add the CSV header");
  current = { ...current, snapshot_hash: "d".repeat(64) };
  responseStatus = 200;
  await click(button("Reload result"));
  await click(button("Request a change"));
  expect(posts[1].snapshot_hash).toBe("d".repeat(64));
  expect(posts[1].submission_id).not.toBe(posts[0].submission_id);
  expect(change).toHaveBeenCalledWith(expect.stringContaining("Only add the CSV header"));
});

it("missing delivery stays read-only and review history retains earlier revisions", async () => {
  current.reviewable = false; current.unavailable_reason = "Recorded revision unavailable.";
  current.history = [{ id: "prior", actor: "owned_person", outcome: "accepted", note: "Earlier useful result",
    snapshot_hash: "e".repeat(64), snapshot: { delivered_head: "f".repeat(40) }, created_at: "2026-10-10T12:00:00Z" }];
  await mount(<ResultReview runId="run_owned" onChangeRequested={change} />);
  expect(button("Accept as useful")).toBeNull();
  expect(document.body.textContent).toContain("Earlier evidence snapshot");
  expect(document.body.textContent).toContain("Earlier useful result");
});

it("a bounded change opens existing Change it without proposing or starting automatically", async () => {
  function Surface() {
    const [message, setMessage] = useState("");
    return <><ResultReview runId="run_owned" onChangeRequested={setMessage} />
      {message ? <ChangeIt projectId="owned_project" runId="run_owned" actor="owned_person"
        initialMessage={message} onClose={() => setMessage("")} /> : null}</>;
  }
  await mount(<Surface />);
  expect((button("Request a change") as HTMLButtonElement).disabled).toBe(true);
  await typeNote("Only add the CSV header");
  await click(button("Request a change"));
  const dialog = document.querySelector("[role=dialog]")!;
  expect(dialog).not.toBeNull();
  expect(dialog.querySelector("textarea")!.value).toContain("Review of run_owned at " + "b".repeat(40));
  expect(dialog.querySelector("textarea")!.value).toContain("Only add the CSV header");
  expect(actions.propose).not.toHaveBeenCalled();
  expect(actions.start).not.toHaveBeenCalled();
  expect(posts).toHaveLength(1);
});
