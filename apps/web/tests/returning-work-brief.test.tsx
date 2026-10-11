// @vitest-environment happy-dom
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseCommandCenterView } from "../lib/command-center-view";
import { lastSeenKey, returningBrief } from "../lib/returning-brief";
import { ReturningBrief } from "../app/command-center/returning-brief";
import { quietView } from "./_helpers/command-center-view-fixture";

const before = "2026-10-10T10:00:00Z";
const updated = "2026-10-10T11:00:00Z";
const confirmed = "2026-10-10T12:00:00Z";
const view = () => parseCommandCenterView(quietView({ project_id: "p1", generated_at: confirmed,
  pulse: { latest: [{ kind: "build", run_id: "attempt-old", verb: "opened a draft PR for", subject: "Docs change", at: updated }] },
  recent: [{ run_id: "wf-current", name: "Check docs", outcome: "failed", finished_at: updated, error: "Check failed" }] }));

describe("returning records stay bounded and attributable", () => {
  it.each([null, "invalid", "2026-10-10T13:00:00Z"])("does not invent changes for cutoff %j", (cutoff) => {
    expect(returningBrief(view(), "p1", cutoff)).toEqual([]);
  });
  it("rejects a foreign project snapshot", () => {
    expect(returningBrief(view(), "other", before)).toEqual([]);
  });
  it("links the exact historical attempt without certifying a newer attempt", () => {
    expect(returningBrief(view(), "p1", before)).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "build:attempt-old", text: "opened a draft PR for Docs change",
        patch: expect.objectContaining({ project_id: "p1", build: "attempt-old" }) }),
      expect.objectContaining({ text: "Check docs: failed", patch: expect.objectContaining({ wfr: "wf-current" }) }),
    ]));
  });
  it("excludes future, invalid and already seen records and collapses repeated run history", () => {
    const v = view();
    v.recent.push({ ...v.recent[0], finishedAt: "2026-10-10T11:30:00Z" },
      { ...v.recent[0], runId: "future", finishedAt: "2026-10-10T13:00:00Z" },
      { ...v.recent[0], runId: "invalid", finishedAt: "bad" },
      { ...v.recent[0], runId: "seen", finishedAt: before });
    expect(returningBrief(v, "p1", before)).toHaveLength(2);
    expect(returningBrief(v, "p1", before)[0].at).toBe("2026-10-10T11:30:00Z");
  });
  it("timestamps are scoped independently to organization, project and viewer", () => {
    const a = lastSeenKey("org", "p1", "owner");
    expect(a).not.toBe(lastSeenKey("other", "p1", "owner"));
    expect(a).not.toBe(lastSeenKey("org", "p2", "owner"));
    expect(a).not.toBe(lastSeenKey("org", "p1", "member"));
  });
});

let host: HTMLDivElement;
let root: Root;
let shown: boolean;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  host = document.createElement("div"); document.body.append(host);
  shown = true;
  vi.spyOn(host, "getClientRects").mockImplementation(() => (shown ? [new DOMRect()] : []) as unknown as DOMRectList);
  root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); });
const render = (props: Partial<React.ComponentProps<typeof ReturningBrief>> = {}) => root.render(
  <ReturningBrief view={view()} projectId="p1" orgId="org" viewer="owner" reachable visible
    container={{ current: host }} {...props} />);
const key = lastSeenKey("org", "p1", "owner");

it("first visit remains uncluttered and stores only the confirmed snapshot timestamp", async () => {
  await act(async () => render());
  expect(host.querySelector("details")).toBeNull();
  expect(window.localStorage.getItem(key)).toBe(confirmed);
  expect(window.localStorage.length).toBe(1);
});
it("return visit opens only scoped records and labels partial history", async () => {
  window.localStorage.setItem(key, before);
  await act(async () => render());
  expect(host.textContent).toContain("Since your last visit");
  expect(host.textContent).toContain("not a complete activity log");
  expect(host.textContent).toContain("does not mean human acceptance");
  const links = [...host.querySelectorAll("a")];
  expect(links).toHaveLength(2);
  expect(links.every((a) => a.getAttribute("href")?.includes("org=org") && a.getAttribute("href")?.includes("project_id=p1"))).toBe(true);
  expect(host.querySelector("button")).toBeNull();
});
it.each(["unreachable", "background", "hidden-area"])("does not mark unseen work as seen while %s", async (mode) => {
  window.localStorage.setItem(key, before);
  shown = mode !== "hidden-area";
  await act(async () => render({ reachable: mode !== "unreachable", visible: mode !== "background" }));
  expect(window.localStorage.getItem(key)).toBe(before);
  if (mode === "unreachable") expect(host.textContent).toContain("Current state is unavailable");
});
it("switching viewer does not show the prior viewer's return history", async () => {
  window.localStorage.setItem(key, before);
  await act(async () => render());
  expect(host.querySelector("details")).not.toBeNull();
  await act(async () => render({ viewer: "member" }));
  expect(host.querySelector("details")).toBeNull();
});
