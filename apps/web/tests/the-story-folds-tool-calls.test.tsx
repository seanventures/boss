// @vitest-environment happy-dom
/**
 * The Story folds tool calls (factory spec §4.5.4): a tool row is name · ok/failed · latency and
 * nothing else; a fold control appears only when there is something under it; `Now:` is one node
 * replaced in place; rows that arrive while the reader is scrolled up raise `n new ↓` and do not
 * move the list.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseCockpit } from "../lib/cockpit";
import { $, $$, click, mount, rerender, unmount } from "./_helpers/live-render";
import { stubFetch } from "./_helpers/cockpit-render";
import { cockpit } from "./_helpers/factory-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Story } = await import("../app/factory/story");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

function view(c = parseCockpit(cockpit())) {
  return (
    <Story projectId="p1" runId={c.run.id} viewer="ceo" rows={c.story} stations={c.stations} now={c.now}
           comments={c.outputs.comments.items} filter={null} onClearFilter={() => {}} onFace={() => {}} onLogs={() => {}} />
  );
}

describe("the Story", () => {
  it("shows rows under station heads, oldest first, with server sentences", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect($$(".fx-story-station").map((h) => h.textContent)).toEqual(["Plan", "Tests", "Build"]);
    expect($('[data-testid="fx-story-act_TESTS0000000001"] .fx-row-what')?.textContent).toBe("Testing, Write tests");
    expect($('[data-testid="fx-story-b1"] .fx-verb')?.textContent).toBe("Cloned the repo");
  });

  it("tool rows say name · ok/failed · ms only, loaded when opened", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect($('[data-testid="fx-tools-body-act_BUILD0000000001"]')).toBeNull();
    expect($('[data-testid="fx-tools-act_BUILD0000000001"]')?.textContent).toContain("31 tool calls");
    await click($('[data-testid="fx-tools-act_BUILD0000000001"]'));
    const rows = $$('[data-testid="fx-tools-body-act_BUILD0000000001"] .fx-tool').map((r) => r.textContent);
    expect(rows).toEqual(["Edit, ok, 340ms", "Bash, failed, 1200ms"]);
    expect($('[data-testid="fx-tools-log"]')?.textContent).toBe("Open in Activity log ↗");
  });

  it("a fold control is never a promise: no tools, files or Said when there are none", async () => {
    stubFetch(cockpit());
    await mount(view());
    // The plan act made no tool calls and wrote no files.
    expect($('[data-testid="fx-tools-act_PLAN0000000001"]')).toBeNull();
    expect($('[data-testid="fx-files-act_PLAN0000000001"]')).toBeNull();
    expect($('[data-testid="fx-said-act_PLAN0000000001"]')).not.toBeNull();
    expect($('[data-testid="fx-said-act_TESTS0000000001"]')).toBeNull();
    expect($('[data-testid="fx-files-act_TESTS0000000001"]')?.textContent).toContain("2 files");
  });

  it("Now: is one node, replaced in place", async () => {
    stubFetch(cockpit());
    await mount(view());
    const first = $('[data-testid="fx-now"]');
    expect(first?.textContent).toBe("Now: Edit src/invoices/export.ts");
    const next = parseCockpit(cockpit({ now: { act_id: "act_BUILD0000000001", tool: "Bash", arg: "npm test", at: "2026-10-04T09:22:00.000Z" } }));
    await rerender(view(next));
    expect($$('[data-testid="fx-now"]')).toHaveLength(1);
    expect($('[data-testid="fx-now"]')).toBe(first);
    expect(first?.textContent).toBe("Now: Bash npm test");
  });

  it("new rows while scrolled up raise n new ↓ and leave the list where it is", async () => {
    stubFetch(cockpit());
    await mount(view());
    const list = $('[data-testid="fx-story-list"]')!;
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 2000 });
    Object.defineProperty(list, "clientHeight", { configurable: true, value: 400 });
    await act(async () => { list.scrollTop = 100; list.dispatchEvent(new Event("scroll")); });
    const base = parseCockpit(cockpit());
    const more = { ...base, story: [...base.story,
      { ...base.story[1], id: "act_REV10000000001", actId: "act_REV10000000001", station: "review" as const, title: "Code review" },
      { ...base.story[1], id: "act_REV20000000001", actId: "act_REV20000000001", station: "review" as const, title: "Security check" }] };
    await rerender(view(more));
    expect($('[data-testid="fx-story-new"]')?.textContent).toBe("2 new ↓");
    expect(list.scrollTop).toBe(100);
    // Arrived while scrolled up: they do not rise in.
    expect($('[data-testid="fx-story-act_REV10000000001"]')?.hasAttribute("data-enter")).toBe(false);
    await click($('[data-testid="fx-story-new"]'));
    expect($('[data-testid="fx-story-new"]')).toBeNull();
  });

  it("rows at first paint never animate", async () => {
    stubFetch(cockpit());
    await mount(view());
    expect($$(".fx-ck-row[data-enter]")).toHaveLength(0);
  });
});
