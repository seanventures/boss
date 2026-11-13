// @vitest-environment happy-dom
/**
 * DOCUMENTS ON A WORK ITEM (BOSSAPP-874): a save names its base and a stale one shows the other
 * writer's revision (Use theirs / Keep mine); a selected passage takes a comment anchored to it;
 * threads say where their passage went; a revision shows its changes.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocEditor } from "../app/projects/work-docs";
import { conflictOf, parseDoc } from "../lib/documents";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const DOC = { document: {
  id: "d1", kind: "plan", title: "Plan", current_revision_id: "r2", current_revision_no: 2, open_threads: 1,
  current: { id: "r2", revision_no: 2, author_kind: "person", author_id: "ceo", body: "Step one.\nStep two.\n" },
  revisions: [{ id: "r2", revision_no: 2, author_kind: "person", author_id: "ceo" },
              { id: "r1", revision_no: 1, author_kind: "agent", author_id: "agent:architect" }],
  threads: [{ id: "t1", revision_id: "r2", quote: "Step two.", anchor_state: "moved", status: "open", created_by: "ceo",
              comments: [{ id: "c1", author: "ceo", body: "Who owns it?" }] }],
} };
const THEIRS = { id: "r3", revision_no: 3, author_kind: "agent", author_id: "agent:architect", body: "Their text." };

afterEach(() => { unmount(); vi.restoreAllMocks(); });

function serve(calls: { url: string; method: string; body: unknown }[]) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (method === "POST" && String(url).endsWith("/revisions")) {
      return new Response(JSON.stringify({ detail: { message: "changed", locked: false, current: THEIRS } }),
        { status: 409, headers: { "Content-Type": "application/json" } });
    }
    const body = String(url).includes("/diff") ? { from: 1, to: 2, lines: ["--- a", "+++ b", "-Old.", "+New."], added: 1, removed: 1 }
      : method === "GET" ? DOC : { thread: {} };
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

function type(el: HTMLTextAreaElement | HTMLInputElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("a work item's documents", () => {
  it("parses a document and a 409's winning revision", () => {
    const d = parseDoc(DOC.document);
    expect(d.threads?.[0].anchor_state).toBe("moved");
    expect(d.revisions?.map((r) => r.revision_no)).toEqual([2, 1]);
    expect(conflictOf({ detail: { current: THEIRS, locked: false } }).revision?.revision_no).toBe(3);
    expect(conflictOf({ detail: "no" }).revision).toBeNull();
  });

  it("saves from its base, shows a conflict, and comments on a selected passage", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    serve(calls);
    await mount(<DocEditor projectId="p1" id="d1" demo={false} onChanged={() => {}} />);
    expect($('[data-testid="wt-thread-t1"] .wt-ev')?.textContent).toBe("Passage moved");
    // Select "Step two." and comment on it.
    const box = $('[data-testid="wt-doc-text"]') as HTMLTextAreaElement;
    box.setSelectionRange(10, 19);
    box.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    await settle();
    type($('[data-testid="wt-doc-comment"]') as HTMLInputElement, "Name the owner");
    await settle();
    ($('[data-testid="wt-doc-comment-add"]') as HTMLButtonElement).click();
    await settle();
    const thread = calls.find((c) => c.method === "POST" && c.url.endsWith("/threads"));
    expect(thread?.body).toMatchObject({ revision_id: "r2", start: 10, end: 19, body: "Name the owner" });
    // An edit saved from revision 2 that lost the race shows the other writer's text.
    type(box, "My text.");
    await settle();
    ($('[data-testid="wt-doc-save"]') as HTMLButtonElement).click();
    await settle();
    const save = calls.find((c) => c.method === "POST" && c.url.endsWith("/revisions"));
    expect(save?.body).toMatchObject({ base_revision_id: "r2", body: "My text." });
    expect($('[data-testid="wt-doc-conflict"]')?.textContent).toContain("Their text.");
    expect($('[data-testid="wt-doc-said"]')?.textContent).toContain("now revision 3");
    ($('[data-testid="wt-doc-take-theirs"]') as HTMLButtonElement).click();
    await settle();
    expect(($('[data-testid="wt-doc-text"]') as HTMLTextAreaElement).value).toBe("Their text.");
    // A revision's changes.
    ($('[data-testid="wt-doc-diff-2"]') as HTMLButtonElement).click();
    await settle();
    expect($$('[data-testid="wt-doc-diff"] [data-d]').map((s) => s.dataset.d)).toEqual(["del", "add"]);
  });
});
