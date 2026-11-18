// @vitest-environment happy-dom
/**
 * SETUP › CONFLUENCE (BOSSAPP-1384): the org's one Confluence connection, under Setup › Documents.
 * Empty state, the connect form (the token is a password field, sent once, never kept in the page),
 * the Jira-credential option, a refusal said in words, the connected row (who BOSS acts as, shared
 * from another project, last test), Test connection, choosing the spaces kept in knowledge, Sync
 * now, and Disconnect asking first. No network: fetch is stubbed.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfluenceRow } from "../app/setup/confluence-row";
import { docsSummary, parseDocsConnection, siteHost } from "../lib/docs-connections";
import { $, mount, settle, unmount } from "./_helpers/live-render";

const CONN = {
  id: "dcn_1", provider: "confluence", label: "Confluence", status: "connected", base_url: "https://acme.atlassian.net/wiki",
  deployment: "cloud", auth_method: "basic", email: "bot@acme.test", acts_as: "BOSS Bot", default_spaces: ["ENG"],
  knowledge_spaces: ["ENG"], sync_state: { ENG: { synced_at: "2026-11-17 10:30", pages: 12 } },
  home_project: { id: "boss-dogfood", name: "BOSS dogfood" }, reach: "project", last_tested_at: "2026-11-17T10:00:00Z",
  last_test_ok: true, can_manage: true, last4: "a1f3", credential_from: "",
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
type Call = { url: string; method: string; body: Record<string, unknown> };

function api(over: (url: string, method: string, body: Record<string, unknown>) => Response | undefined = () => undefined, list: unknown[] = [CONN]) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = String(init?.method ?? "GET");
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ url: String(url), method, body });
    const o = over(String(url), method, body);
    if (o) return o;
    if (url.startsWith("/v1/docs-connections/spaces")) return json({ spaces: [{ id: "98", key: "ENG", name: "Engineering" }, { id: "99", key: "OPS", name: "Operations" }] });
    if (url.startsWith("/v1/docs-connections?project_id=") && method === "GET") return json({ connections: list });
    if (url === "/v1/docs-connections/test") return json({ connection: CONN, test: { ok: true, account: "BOSS Bot" } });
    if (url === "/v1/docs-connections/sync") return json({ job: { status: "running", spaces: { ENG: { status: "waiting" } } } }, 202);
    if (url.startsWith("/v1/docs-connections/sync?")) return json({ job: { status: "done", spaces: { ENG: { status: "done", seen: 12, indexed: 2, updated: 1, unchanged: 9, failed: [] } } } });
    if (method === "POST" && url === "/v1/docs-connections") return json({ connection: CONN, test: { ok: true, account: "BOSS Bot" } }, 201);
    return json({ connection: CONN });
  }));
  return calls;
}
const type = async (sel: string, value: string) => {
  const el = $(sel) as HTMLInputElement;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await settle();
};
const click = async (sel: string) => { ($(sel) as HTMLElement).click(); await settle(); };

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("words", () => {
  it("parses a connection defensively and says the folded line", () => {
    const c = parseDocsConnection(CONN);
    expect(c.status).toBe("connected");
    expect(siteHost(c.base_url)).toBe("acme.atlassian.net");
    expect(docsSummary(c)).toBe("Confluence · acme.atlassian.net · 1 space in knowledge");
    expect(docsSummary(null)).toBe("Not connected");
    expect(parseDocsConnection({}).status).toBe("disconnected");
  });
});

describe("Setup › Confluence", () => {
  it("says it is not connected and connects with a test first, the token sent once", async () => {
    const calls = api(undefined, []);
    await mount(<ConfluenceRow projectId="p1" />);
    expect($('[data-testid="cf-empty"]')?.textContent).toContain("Not connected");
    await click('[data-testid="cf-add"]');
    expect(($('[data-testid="cf-token"]') as HTMLInputElement).type).toBe("password");
    await type('[data-testid="cf-url"]', "https://acme.atlassian.net/wiki");
    await type('[data-testid="cf-email"]', "bot@acme.test");
    await type('[data-testid="cf-token"]', "SECRET-TOKEN-VALUE");
    await type('[data-testid="cf-spaces"]', "ENG, OPS");
    await click('[data-testid="cf-connect"]');
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/docs-connections")!;
    expect(post.body).toMatchObject({ project_id: "p1", provider: "confluence", base_url: "https://acme.atlassian.net/wiki",
      fields: { email: "bot@acme.test" }, secrets: { api_token: "SECRET-TOKEN-VALUE" }, default_spaces: ["ENG", "OPS"], use_jira_credential: false });
    expect($('[data-testid="cf-said"]')?.textContent).toBe("Confluence is connected · signed in as BOSS Bot.");
    expect(document.body.innerHTML).not.toContain("SECRET-TOKEN-VALUE");   // never kept in the page
    expect($('[data-testid="cf-form"]')).toBeNull();
  });

  it("can reuse the org's Jira credential and then asks for no email or token", async () => {
    const calls = api(undefined, []);
    await mount(<ConfluenceRow projectId="p1" />);
    await click('[data-testid="cf-add"]');
    await click('[data-testid="cf-jira"]');
    expect($('[data-testid="cf-token"]')).toBeNull();
    expect($('[data-testid="cf-email"]')).toBeNull();
    await click('[data-testid="cf-connect"]');
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/docs-connections")!;
    expect(post.body).toMatchObject({ use_jira_credential: true, secrets: {}, fields: {} });
  });

  it("says a refusal in words and keeps the form open", async () => {
    api((url, method) => (method === "POST" && url === "/v1/docs-connections"
      ? json({ detail: { error: "auth_failed", message: "Confluence rejected the credential. Reconnect Confluence in Setup › Connections." } }, 424) : undefined), []);
    await mount(<ConfluenceRow projectId="p1" />);
    await click('[data-testid="cf-add"]');
    await type('[data-testid="cf-url"]', "https://acme.atlassian.net");
    await type('[data-testid="cf-email"]', "bot@acme.test");
    await type('[data-testid="cf-token"]', "nope");
    await click('[data-testid="cf-connect"]');
    expect($('[data-testid="cf-said"]')?.textContent).toContain("Confluence rejected the credential");
    expect($('[data-testid="cf-form"]')).not.toBeNull();
  });

  it("shows the connected row: site, who BOSS acts as, sharing, spaces", async () => {
    api(undefined, [{ ...CONN, reach: "org", credential_from: "jira" }]);
    await mount(<ConfluenceRow projectId="helpdesk" />);
    expect($('[data-testid="cf-status"]')?.textContent).toContain("Connected");
    expect($('[data-testid="cf-site"]')?.textContent).toBe("acme.atlassian.net");
    const account = $('[data-testid="cf-account"]')?.textContent ?? "";
    expect(account).toContain("BOSS acts as BOSS Bot");
    expect(account).toContain("Shared · added in BOSS dogfood");
    expect(account).toContain("same credential as Jira");
    expect($('[data-testid="cf-knowledge"]')?.textContent).toContain("ENG");
  });

  it("tests the connection with one call and says who it signed in as", async () => {
    const calls = api();
    await mount(<ConfluenceRow projectId="p1" />);
    await click('[data-testid="cf-menu"]');
    await click('[data-testid="cf-test"]');
    expect(calls.some((c) => c.method === "POST" && c.url === "/v1/docs-connections/test")).toBe(true);
    expect($('[data-testid="cf-said"]')?.textContent).toBe("Confluence works · signed in as BOSS Bot.");
  });

  it("chooses the spaces kept in knowledge and syncs them", async () => {
    const calls = api();
    await mount(<ConfluenceRow projectId="p1" />);
    await click('[data-testid="cf-pick"]');
    await click('[data-testid="cf-space-OPS"]');
    await click('[data-testid="cf-space-save"]');
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.body).toMatchObject({ project_id: "p1", knowledge_spaces: ["ENG", "OPS"] });
    await click('[data-testid="cf-sync"]');
    expect(calls.some((c) => c.url === "/v1/docs-connections/sync")).toBe(true);
    expect($('[data-testid="cf-said"]')?.textContent).toBe("Synced: 2 new, 1 updated, 9 unchanged.");
  });

  it("asks before disconnecting", async () => {
    const calls = api();
    await mount(<ConfluenceRow projectId="p1" />);
    await click('[data-testid="cf-menu"]');
    await click('[data-testid="cf-off"]');
    expect($('[data-testid="cf-confirm"]')?.textContent).toContain("Agents lose access");
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await click('[data-testid="cf-confirm-yes"]');
    expect(calls.some((c) => c.method === "DELETE" && c.url.startsWith("/v1/docs-connections?project_id=p1"))).toBe(true);
  });

  it("says why a read failed instead of an empty page", async () => {
    api((url) => (url.startsWith("/v1/docs-connections?") ? json({ detail: "You do not have permission." }, 403) : undefined));
    await mount(<ConfluenceRow projectId="p1" />);
    expect($('[data-testid="cf-error"]')?.textContent).toContain("permission");
  });
});
