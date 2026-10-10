// @vitest-environment happy-dom
import React, { act } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { click, mount, settle, unmount } from "./_helpers/live-render";

const gate = vi.hoisted(() => ({ allowed: true }));
vi.mock("../components/identity/identity-provider", () => ({ useCan: () => ({ allowed: gate.allowed }) }));
import { SavedChecks } from "../app/factory/saved-checks";
import { ResultReview } from "../app/factory/result-review";

const saved = () => ({ template_id: "checks_owned", revision: 1, revision_hash: "d".repeat(64),
  title: "Recorded CSV checks", criteria: ["Export selected records", "Include a header"],
  source: { run_id: "run_owned", review_id: "review_owned", snapshot_hash: "a".repeat(64),
    delivered_head: "b".repeat(40), check_state: "invalidated" } });
let items: ReturnType<typeof saved>[];
let posts: { url: string; body: Record<string, unknown> }[];
let responseStatus: number;
const copy = vi.fn();
const button = (text: string) => [...document.querySelectorAll("button")].find(b => b.textContent === text) ?? null;
const props = { projectId: "project_owned", runId: "run_owned", acceptedReviewId: "review_owned",
  snapshotHash: "a".repeat(64), criteria: ["Export selected records"] };

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  items = []; posts = []; responseStatus = 200; copy.mockReset(); copy.mockResolvedValue(undefined); gate.allowed = true;
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copy } });
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url: String(url), body: JSON.parse(String(init.body)) });
      if (responseStatus !== 200) return Response.json({ detail: "These checks changed. Reload before saving." }, { status: responseStatus });
      items = [saved()]; return Response.json(saved());
    }
    return Response.json({ items });
  }));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

it("creates only after an explicit accepted-check action and carries exact source identity", async () => {
  await mount(<SavedChecks {...props} />);
  expect(posts).toHaveLength(0);
  await click(button("Save accepted checks"));
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ project_id: "project_owned", run_id: "run_owned", review_id: "review_owned", snapshot_hash: props.snapshotHash });
  expect(posts[0].body).not.toHaveProperty("actor");
  expect(document.body.textContent).toContain("No work started");
  expect(document.body.textContent).toContain("invalidated");
  expect(button("Start")).toBeNull();
});

it("unaccepted or empty criteria cannot save, and read-only permission disables writes", async () => {
  await mount(<SavedChecks {...props} acceptedReviewId={null} />);
  expect(button("Save accepted checks")!.disabled).toBe(true);
  expect(document.body.textContent).toContain("Accept the current recorded result");
  unmount();
  await mount(<SavedChecks {...props} criteria={[]} />);
  expect(button("Save accepted checks")!.disabled).toBe(true);
  unmount(); gate.allowed = false;
  await mount(<SavedChecks {...props} />);
  expect(button("Save accepted checks")!.getAttribute("aria-disabled")).toBe("true");
  await click(button("Save accepted checks"));
  expect(posts).toHaveLength(0);
});

it("copies only bounded checks with explicit fresh agreement instructions and no write", async () => {
  items = [saved()];
  await mount(<SavedChecks {...props} />);
  await click(button("Copy checks for a fresh task"));
  expect(copy).toHaveBeenCalledWith("- Export selected records\n- Include a header");
  expect(posts).toHaveLength(0);
  expect(document.body.textContent).toContain("review its current work agreement before starting");
  expect(document.body.textContent).not.toContain("approved to start");
});

it("failed clipboard does not claim copied and gives a manual fallback", async () => {
  items = [saved()]; copy.mockRejectedValue(new Error("owned clipboard refused"));
  await mount(<SavedChecks {...props} />);
  await click(button("Copy checks for a fresh task"));
  expect(document.querySelector("[role=alert]")!.textContent).toContain("copy them manually");
  expect(document.querySelector("[role=status]")).toBeNull();
});

it("an edit carries the displayed revision precondition and retries uncertainty unchanged", async () => {
  items = [saved()]; responseStatus = 503;
  await mount(<SavedChecks {...props} />);
  await click(button("Edit saved checks"));
  await click(button("Save check revision"));
  expect(posts[0].url).toBe("/v1/acceptance-checks/checks_owned/revisions");
  expect(posts[0].body.expected_revision_hash).toBe("d".repeat(64));
  responseStatus = 200;
  await click(button("Save check revision"));
  expect(posts[1]).toEqual(posts[0]);
  expect(document.querySelector("textarea")).toBeNull();
});

it("a pending save blocks duplicate presses and preserves input on a stale edit", async () => {
  items = [saved()];
  let finish!: (r: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url: String(_url), body: JSON.parse(String(init.body)) });
      return new Promise<Response>(resolve => { finish = resolve; });
    }
    return Response.json({ items });
  }));
  await mount(<SavedChecks {...props} />);
  await click(button("Edit saved checks"));
  await act(async () => { button("Save check revision")!.click(); button("Save check revision")!.click(); });
  expect(posts).toHaveLength(1);
  await act(async () => { finish(Response.json({ detail: "Reload before saving." }, { status: 409 })); });
  await settle();
  expect(document.querySelector("textarea")!.value).toBe("Export selected records\nInclude a header");
  expect(document.querySelector("[role=alert]")!.textContent).toContain("Reload before saving");
});

it("mounts on actual ResultReview and only the latest current accepted snapshot enables save", async () => {
  const data = { snapshot_hash: "a".repeat(64), reviewable: true, unavailable_reason: null,
    snapshot: { project_id: "project_owned", delivered_head: "b".repeat(40), pull_request_url: null,
      receipt_id: "receipt_owned", criteria: ["Export selected rows"],
      checks: { state: "failed", checked_head: null, artifact_id: null, quality_status: "fail", gates_failed: [], commands: [] },
      cost: { observed_usd_micros: null, estimated_usd_micros: 0, included_equivalent_usd_micros: 0, held_usd_micros: 0, confidence: "unknown", unresolved_records: 0, included_calls: 0 } },
    history: [{ id: "review_owned", actor: "person_owned", outcome: "accepted", note: "", created_at: "now", snapshot_hash: "c".repeat(64), snapshot: { delivered_head: "b".repeat(40) } }] };
  vi.stubGlobal("fetch", vi.fn(async (url: unknown) => Response.json(String(url).includes("acceptance-checks") ? { items: [] } : data)));
  await mount(<ResultReview runId="run_owned" onChangeRequested={vi.fn()} />);
  expect(button("Save accepted checks")!.disabled).toBe(true);
  unmount(); data.history[0].snapshot_hash = data.snapshot_hash;
  await mount(<ResultReview runId="run_owned" onChangeRequested={vi.fn()} />);
  expect(button("Save accepted checks")!.disabled).toBe(false);
  expect(document.body.textContent).toContain("Checks: failed");
});

it("an older successful list request cannot erase a failed save", async () => {
  let finish!: (r: Response) => void;
  vi.stubGlobal("fetch", vi.fn(async (_url: unknown, init?: RequestInit) => {
    if (init?.method === "POST") return Response.json({ detail: "The source review changed." }, { status: 409 });
    return new Promise<Response>(resolve => { finish = resolve; });
  }));
  await mount(<SavedChecks {...props} />);
  await click(button("Save accepted checks"));
  expect(document.querySelector("[role=alert]")!.textContent).toContain("source review changed");
  await act(async () => { finish(Response.json({ items: [] })); });
  await settle();
  expect(document.querySelector("[role=alert]")!.textContent).toContain("source review changed");
  expect(document.querySelector("[role=status]")).toBeNull();
});
