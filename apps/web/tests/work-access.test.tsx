// @vitest-environment happy-dom
/**
 * WHO CAN SEE IT (BOSSAPP-876): Private, the grants (a party typed wrong is not sent), the
 * break-glass notice; a private blocker shows only "Private item" and Read (audited) needs a
 * reason before it is sent.
 * BOSSAPP-1042 (e): turning Private OFF sends visibility "project", unticks at once, says
 * "Visible to the project." (never the Private words) and hides the grant chips and Grant field.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ItemAccess, PrivateBlocker } from "../app/projects/work-access";
import { parseNode } from "../lib/work-items";
import { $, mount, rerender, settle, unmount } from "./_helpers/live-render";

const ITEM = parseNode({
  id: "w1", title: "HR case", status: "todo", visibility: "private", restricted: true,
  access: [{ party: "person:hr", granted_by: "hr" }],
  break_glass_reads: [{ actor: "auditor", reason: "Regulator request", at: null }],
  children: [], labels: [], blocked_by: [],
});

afterEach(() => { unmount(); vi.restoreAllMocks(); });

function serve(calls: { url: string; body: unknown }[], reply: unknown = { access: [] }) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify(reply), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

function type(el: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("who can see a work item", () => {
  it("parses privacy and draws grants and the break-glass notice", async () => {
    expect(ITEM.visibility).toBe("private");
    expect(parseNode({ id: "x", blocked_by: [{ id: "s", title: "Private item", private: true }] }).blocked_by[0].private).toBe(true);
    const calls: { url: string; body: unknown }[] = [];
    serve(calls);
    await mount(<ItemAccess projectId="p1" item={ITEM} demo={false} onChanged={() => {}} />);
    expect(($('[data-testid="wt-private"]') as HTMLInputElement).checked).toBe(true);
    expect($('[data-testid="wt-break-glass-notice"]')?.textContent).toContain("auditor");
    type($('[data-testid="wt-grant-party"]') as HTMLInputElement, "legal");
    await settle();
    expect(($('[data-testid="wt-grant-add"]') as HTMLButtonElement).disabled).toBe(true);
    type($('[data-testid="wt-grant-party"]') as HTMLInputElement, "person:legal");
    await settle();
    ($('[data-testid="wt-grant-add"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url === "/v1/work-items/w1/access")?.body).toMatchObject({ party: "person:legal", revoke: false });
  });

  it("turning Private off says so, and the grants and their field go away", async () => {
    const calls: { url: string; body: unknown }[] = [];
    serve(calls, { item: {} });
    await mount(<ItemAccess projectId="p1" item={ITEM} demo={false} onChanged={() => {}} />);
    expect($('[data-testid="wt-grant-person:hr"]')).not.toBeNull();
    ($('[data-testid="wt-private"]') as HTMLInputElement).click();
    await settle();
    expect(calls.find((c) => c.url === "/v1/work-items/w1")?.body).toMatchObject({ visibility: "project" });
    expect(($('[data-testid="wt-private"]') as HTMLInputElement).checked).toBe(false);
    expect($('[data-testid="wt-access-said"]')?.textContent).toBe("Visible to the project.");
    expect(document.body.textContent).not.toContain("only its people");
    expect($('[data-testid="wt-grant-person:hr"]')).toBeNull();
    expect($('[data-testid="wt-grant-party"]')).toBeNull();
    expect(document.body.textContent).not.toContain("Private through the item above it");
    // The item reloads as visible: still no chips; the grants are kept server-side, not revoked.
    await rerender(<ItemAccess projectId="p1" item={{ ...ITEM, visibility: "project", restricted: false }} demo={false} onChanged={() => {}} />);
    expect($('[data-testid="wt-grant-person:hr"]')).toBeNull();
    expect(calls.some((c) => c.url.includes("/access"))).toBe(false);
    // On again: the words and the chips come back.
    ($('[data-testid="wt-private"]') as HTMLInputElement).click();
    await settle();
    expect($('[data-testid="wt-access-said"]')?.textContent).toBe("Private: only its people and those granted can see it.");
    expect($('[data-testid="wt-grant-person:hr"]')).not.toBeNull();
  });

  it("reads a private blocker only with a reason, and says it was recorded", async () => {
    const calls: { url: string; body: unknown }[] = [];
    serve(calls, { item: { id: "s1", human_key: "WI-9", title: "Acquisition", status: "todo", children: [] } });
    await mount(<PrivateBlocker projectId="p1" blocker={{ id: "s1", human_key: null, title: "Private item", status: "todo", private: true }} demo={false} />);
    ($('[data-testid="wt-break-glass"]') as HTMLButtonElement).click();
    await settle();
    type($('[data-testid="wt-break-glass-reason"]') as HTMLInputElement, "short");
    await settle();
    expect(($('[data-testid="wt-break-glass-go"]') as HTMLButtonElement).disabled).toBe(true);
    type($('[data-testid="wt-break-glass-reason"]') as HTMLInputElement, "Regulator request 42");
    await settle();
    ($('[data-testid="wt-break-glass-go"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url === "/v1/work-items/s1/break-glass")?.body).toMatchObject({ reason: "Regulator request 42" });
    expect($('[data-testid="wt-break-glass-seen"]')?.textContent).toContain("WI-9 · Acquisition");
  });
});
