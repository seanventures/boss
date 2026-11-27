// @vitest-environment happy-dom
/**
 * SETUP › TICKET SYSTEMS (BOSSAPP-1047 T1 + T2): the connected systems in one line each, Test /
 * Settings / Disconnect, Add with the catalog's tiles, a write-only token sent once, the test
 * result, mappings, write-back chips, the pick-up rule (ask first | start automatically), the
 * Advanced webhook shown once, Connect with Atlassian. Every state: loading, empty, error in
 * words, permission denied.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { tileLine } from "../app/setup/add-ticket-system";
import { TicketSystemsRow, ticketSummary } from "../app/setup/ticket-systems-row";
import { ConnectionSection } from "../app/skills/connection-section";
import { askOwnerWords, codeWords, oauthReady, parseConnection, pickupWords, writesWords } from "../lib/tickets";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const JIRA = {
  id: "wtp_1", project_id: "p1", provider: "jira", label: "Jira", icon: "jira", status: "connected",
  base_url: "https://northpeak.atlassian.net", fields: { email: "boss-bot@northpeak.example", project_key: "LEND" },
  account: "BOSS Bot", last4: "a1f3", fingerprint: "fp", capabilities: { read: true, browse: true, write_comment: true, transition: true, webhook: true },
  settings: { mappings: [{ external_project: "LEND", boss_project_id: "p1", default: true }], write_back: { comment: true, transition: true, link_pr: true },
              status_map: { todo: "", doing: "", review: "In Review", done: "" }, pickup: { label: "boss", mode: "ask" },
              inbound: { mode: "webhook", poll_minutes: 5 }, project_keys: ["LEND", "OPS"] },
  managed_from: null, mirrors: [], bindings: { total: 3, paused: 0 },
  oauth_app: { client_id: "atl-app", secret_set: true, from_host: false, callback_url: "https://boss.example/v1/ticket-systems/oauth/callback" },
};
const CATALOG = { items: [
  { provider: "jira", label: "Jira", icon: "jira", description: "Atlassian Jira Cloud — issues, bugs, and epics.", docs_url: "",
    auth_fields: [{ name: "base_url", label: "Site URL", placeholder: "https://your-domain.atlassian.net" }, { name: "email", label: "Atlassian account email" },
                  { name: "project_key", label: "Project key" }, { name: "api_token", label: "API token", secret: true },
                  { name: "issue_type", label: "Default issue type", required: false }],
    auth_kinds: [{ kind: "api_token" }, { kind: "oauth2" }], base_url_field: "base_url", capabilities: { webhook: true },
    status_map_defaults: { review: "In Review" }, webhook: true },
  { provider: "plane", label: "Plane", icon: "plane", description: "Plane — cloud or self-hosted.", docs_url: "",
    auth_fields: [{ name: "api_token", label: "API key", secret: true }], auth_kinds: [{ kind: "api_token" }], base_url_field: "", capabilities: {}, status_map_defaults: {}, webhook: true },
] };

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
type Call = { url: string; method: string; body: Record<string, unknown> };

function api(over: (url: string, method: string, body: Record<string, unknown>) => Response | undefined = () => undefined) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = String(init?.method ?? "GET");
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ url: String(url), method, body });
    const o = over(String(url), method, body);
    if (o) return o;
    if (url.startsWith("/v1/ticket-systems/catalog")) return json(CATALOG);
    if (url.startsWith("/v1/auth/options")) return json({ org_name: "Example Labs" });
    if (url.startsWith("/v1/ticket-systems?project_id=")) return json({ connections: [JIRA] });
    if (url.endsWith("/projects")) return json({ projects: [{ key: "LEND", name: "Lending" }, { key: "OPS", name: "Operations" }] });
    if (url.endsWith("/test")) return json({ connection: JIRA, test: { ok: true, account: "BOSS Bot", project_keys: ["LEND", "OPS"] } });
    if (url.endsWith("/webhook")) return json({ url: "https://boss.example/v1/webhooks/tickets/wtp_1", secret: "whsec_ONCE", last4: "ONCE", rotated: false });
    if (url.endsWith("/oauth/start/load")) return json({ authorize_url: "https://auth.atlassian.com/authorize?x=1" });
    if (method === "POST" && url === "/v1/ticket-systems") return json({ connection: JIRA, test: { ok: true, account: "BOSS Bot", capabilities: { read: true, write_comment: true, transition: true } } }, 201);
    if (method === "PATCH" || method === "DELETE") return json({ connection: JIRA });
    return json({});
  }));
  return calls;
}
const type = async (sel: string, value: string) => {
  const el = $(sel) as HTMLInputElement | HTMLSelectElement;
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  await settle();
};
const click = async (sel: string) => { ($(sel) as HTMLElement).click(); await settle(); };

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("words", () => {
  it("says write-back and the pick-up rule in one line each, and a code as a sentence", () => {
    const c = parseConnection(JIRA);
    expect(writesWords(c.settings.write_back)).toBe("Comments · status · PR link");
    expect(pickupWords(c.settings)).toBe("Label “boss” → ask first");
    expect(pickupWords({ ...c.settings, pickup: { label: "boss:coding", mode: "auto_start" } })).toBe("Label “boss:coding” → start");
    expect(pickupWords({ ...c.settings, inbound: { mode: "off", poll_minutes: 5 } })).toBe("Off");
    expect(codeWords("auth_failed")).toMatch(/refused BOSS's sign-in/);
    expect(codeWords("auth_failed", "Jira said 401.")).toBe("Jira said 401.");
  });
});

describe("Setup › Ticket systems (T1)", () => {
  // 2026-11-15: "I added the Jira ticket system but it does not show up". A ticket system is
  // connected once for the org: from another project it shows, says where it was added and
  // where its tickets go, and cannot be disconnected from there.
  it("shows a ticket system added from another project of the org", async () => {
    const shared = { ...JIRA, id: "wtp_home", project_id: "boss-dogfood", reach: "org",
                     home_project: { id: "boss-dogfood", name: "BOSS dogfood bootstrap" },
                     settings: { ...JIRA.settings, mappings: [], project_keys: ["KAN"] } };
    api((url) => (url.startsWith("/v1/ticket-systems?project_id=") ? json({ connections: [shared] }) : undefined));
    await mount(<TicketSystemsRow projectId="helpdesk-laptop" />);
    expect($('[data-testid="tk-systems-empty"]')).toBeNull();
    expect($('[data-testid="tk-shared-wtp_home"]')?.textContent?.trim()).toBe("Shared");
    expect($('[data-testid="tk-shared-from-wtp_home"]')?.textContent).toBe("added in BOSS dogfood bootstrap");
    // Nothing maps here: say so; where each key lands is the line below, the mapping the tooltip.
    expect($('[data-testid="tk-maps-wtp_home"]')?.textContent).toBe("None here");
    expect($('[data-testid="tk-maps-wtp_home"]')?.getAttribute("title")).toBe("KAN → BOSS dogfood bootstrap");
    // Nothing lands here: say where KAN tickets go and open New work THERE (never a made-up key).
    expect($('[data-testid="tk-where-new"]')).toBeNull();
    expect($('[data-testid="tk-where-lands"]')?.textContent).toBe("KAN tickets land in BOSS dogfood bootstrap.");
    expect($('[data-testid="tk-where-there"]')?.getAttribute("href")).toBe("?area=project&project_id=boss-dogfood&tab=work&compose=1");
    expect($('[data-testid="tk-where-there"]')?.textContent).toBe("New work in BOSS dogfood bootstrap →");
    expect($('[data-testid="tk-map-here"]')).toBeNull();   // not this viewer's to map
    expect(parseConnection(shared).reach).toBe("org");
    await click('[data-testid="tk-menu-wtp_home"]');
    expect(($('[data-testid="tk-off-wtp_home"]') as HTMLButtonElement | null)?.disabled
           ?? $('[data-testid="tk-off-wtp_home"]')?.getAttribute("aria-disabled") === "true").toBeTruthy();
  });

  // 2026-11-15: "I dont see anything … why are you not showing the jira ticket system that was
  // added ?" — the org HAS a Jira; the project was told "No ticket system yet". Now the headline
  // is the org's connection, with its one way out.
  const HIDDEN = { id: "wtp_other", provider: "jira", label: "Jira", status: "not_shared", reach: "unshared",
                   home_project: { id: "boss-dogfood", name: "BOSS dogfood bootstrap", owners: ["Param G", "Sean"] } };

  it("a project with none of its own headlines the org's unshared one, and Share it opens its switch", async () => {
    const calls = api((url) => {
      if (url.startsWith("/v1/ticket-systems?project_id=")) return json({ connections: [{ ...HIDDEN, can_manage: true }] });
      if (url.startsWith("/v1/ticket-systems/org?")) return json({ connections: [{ ...JIRA, id: "wtp_other", project_id: "boss-dogfood",
        reach: "unshared", usable_here: false, can_manage: true, home_project: HIDDEN.home_project, project_names: { p1: "Lending" } }] });
      return undefined;
    });
    window.history.replaceState(null, "", "/?org=org_boss&area=setup&project_id=northgate&focus=ticket_systems");
    await mount(<TicketSystemsRow projectId="northgate" />);
    expect($('[data-testid="tk-systems-empty"]')).toBeNull();
    expect(document.body.textContent).not.toContain("No ticket system yet");
    expect($('[data-testid="tk-org-headline"]')?.textContent).toBe("Jira is connected in BOSS dogfood bootstrap, not shared here yet");
    const link = $('[data-testid="tk-unshared-wtp_other-share"]') as HTMLAnchorElement;
    expect(link.textContent?.trim()).toBe("Share it");
    // The deep link keeps who you are and where you are, and names the connection.
    expect(link.getAttribute("href")).toBe("?org=org_boss&area=setup&focus=ticket_systems&project_id=northgate&tk=all&share=wtp_other");
    expect($('[data-testid="tk-add-empty"]')?.textContent).toBe("or add another ticket system");
    // Pressed here, it switches to All in org with the share asked: one press from done.
    await click('[data-testid="tk-unshared-wtp_other-share"]');
    await settle();
    expect($('[data-testid="tk-view-all"]')?.getAttribute("aria-selected")).toBe("true");
    expect($('[data-testid="tk-system-wtp_other"]')?.getAttribute("data-spot")).toBe("true");
    expect($('[data-testid="tk-share-confirm"]')?.textContent).toContain("Everyone in Example Labs and their agents can read tickets");
    expect(new URLSearchParams(window.location.search).get("share")).toBe("wtp_other");
    await click('[data-testid="tk-share-yes"]');
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect([patch.url, patch.body]).toEqual(["/v1/ticket-systems/wtp_other", { share_with_org: true }]);
  });

  it("a person who cannot manage it there is told whom to ask, never sent to a switch", async () => {
    api((url) => (url.startsWith("/v1/ticket-systems?project_id=") ? json({ connections: [{ ...HIDDEN, can_manage: false }] }) : undefined));
    await mount(<TicketSystemsRow projectId="northgate" />);
    expect($('[data-testid="tk-org-headline"]')?.textContent).toBe("Jira is connected in BOSS dogfood bootstrap, not shared here yet");
    expect($('[data-testid="tk-unshared-wtp_other-share"]')).toBeNull();
    expect($('[data-testid="tk-unshared-wtp_other-ask"]')?.textContent?.trim())
      .toBe("Ask Param G or Sean (owner of BOSS dogfood bootstrap) to share it");
    expect(askOwnerWords({ id: "x", name: "Ops", owners: [] })).toBe("Ask an owner of Ops to share it");
    // A project the viewer may not read is never named: an org owner is asked.
    expect(askOwnerWords({ id: "", name: "another project", owners: ["Param G"], hidden: true })).toBe("Ask an org owner (Param G) to share it");
    expect(askOwnerWords({ id: "", name: "another project", owners: [], hidden: true })).toBe("Ask an org owner to share it");
  });

  it("All in org lists every connection with where it was added and whose token, managed per row", async () => {
    api((url) => (url.startsWith("/v1/ticket-systems/org?") ? json({ connections: [
      { ...JIRA, id: "wtp_mine", reach: "project", usable_here: true, can_manage: true, home_project: { id: "p1", name: "Lending", owners: [] } },
      { ...JIRA, id: "wtp_theirs", project_id: "ops", reach: "unshared", usable_here: false, can_manage: false, last4: null,
        home_project: { id: "ops", name: "Ops", owners: ["Dana"] }, settings: { ...JIRA.settings, mappings: [{ external_project: "OPS", boss_project_id: "ops", default: true }] },
        project_names: { ops: "Ops" } },
    ] }) : undefined));
    window.history.replaceState(null, "", "/?area=setup&tk=all");
    await mount(<TicketSystemsRow projectId="p1" />);
    expect($('[data-testid="tk-home-wtp_mine"]')?.textContent).toContain("This project");
    expect($('[data-testid="tk-home-wtp_mine"]')?.textContent).toContain("as BOSS Bot");
    expect($('[data-testid="tk-home-wtp_theirs"]')?.textContent).toContain("Ops");
    expect($('[data-testid="tk-system-wtp_theirs"]')?.textContent).toContain("OPS → Ops (default)");
    // Sharing is one control in its own column; a row the viewer cannot manage is locked, with whom to ask.
    expect(($('[data-testid="tk-share-wtp_mine"]') as HTMLButtonElement).disabled).toBe(false);
    expect(($('[data-testid="tk-share-wtp_theirs"]') as HTMLButtonElement).disabled).toBe(true);
    expect($('[data-testid="tk-share-wtp_theirs"]')?.getAttribute("title")).toBe("Ask Dana to change who can use it.");
    expect($('[data-testid="tk-org-ask-wtp_theirs-ask"]')?.textContent?.trim()).toBe("Ask Dana (owner of Ops) to share it");
    window.history.replaceState(null, "", "/");
  });

  it("sharing is a pill on the row whose press asks in whose-token words, then switches it", async () => {
    const calls = api();
    window.history.replaceState(null, "", "/");
    await mount(<TicketSystemsRow projectId="p1" />);
    const pill = $('[data-testid="tk-share-wtp_1"]') as HTMLButtonElement;
    expect(pill.textContent?.trim()).toBe("This project only");
    expect(pill.getAttribute("aria-pressed")).toBe("false");
    expect(pill.getAttribute("title"))
      .toBe("Only this project uses it. Press to share: Everyone in Example Labs and their agents can read tickets through this connection using BOSS Bot's token.");
    await click('[data-testid="tk-share-wtp_1"]');
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    expect($('[data-testid="tk-share-confirm"]')?.textContent)
      .toContain("Everyone in Example Labs and their agents can read tickets through this connection using BOSS Bot's token.");
    await click('[data-testid="tk-share-yes"]');
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect([patch.url, patch.body]).toEqual(["/v1/ticket-systems/wtp_1", { share_with_org: true }]);
    expect($('[data-testid="tk-said"]')?.textContent).toBe("Jira is shared with the whole org.");
  });

  it("names the project New work opens in, and offers Map KAN here to whoever may", async () => {
    const calls = api((url) => {
      if (url.startsWith("/v1/ticket-systems?project_id=northgate")) return json({ project_name: "Northgate Gateway", connections: [
        { ...JIRA, id: "wtp_home", project_id: "boss-dogfood", reach: "org", can_manage: true,
          home_project: { id: "boss-dogfood", name: "BOSS dogfood bootstrap", owners: [] },
          settings: { ...JIRA.settings, mappings: [{ external_project: "KAN", boss_project_id: "boss-dogfood", default: true }], project_keys: ["KAN"] } }] });
      return undefined;
    });
    await mount(<TicketSystemsRow projectId="northgate" />);
    expect($('[data-testid="tk-where-there"]')?.textContent).toBe("New work in BOSS dogfood bootstrap →");
    expect($('[data-testid="tk-map-here"]')?.textContent).toBe("Map KAN here");
    await click('[data-testid="tk-map-here"]');
    // Asked first, in words that say who stops seeing them; nothing is sent until Map.
    expect($('[data-testid="tk-map-confirm"]')?.textContent)
      .toContain("Moves KAN tickets from BOSS dogfood bootstrap to Northgate Gateway; people there stop seeing new KAN tickets.");
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    // One ask at a time: the line's button waits while its confirm is open.
    expect(($('[data-testid="tk-map-here"]') as HTMLButtonElement).disabled).toBe(true);
    await click('[data-testid="tk-map-yes"]');
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect([patch.url, patch.body]).toEqual(["/v1/ticket-systems/wtp_home",
      { mappings: [{ external_project: "KAN", boss_project_id: "northgate", default: true }] }]);
    expect($('[data-testid="tk-said"]')?.textContent).toBe("KAN tickets now land in Northgate Gateway.");
  });

  it("says where each key really lands, not the home", async () => {
    api((url) => (url.startsWith("/v1/ticket-systems?project_id=") ? json({ connections: [
      { ...JIRA, id: "wtp_home", project_id: "boss-dogfood", reach: "org", can_manage: false,
        home_project: { id: "boss-dogfood", name: "BOSS dogfood bootstrap", owners: [] }, project_names: { ops: "Ops" },
        settings: { ...JIRA.settings, mappings: [{ external_project: "KAN", boss_project_id: "ops", default: false }], project_keys: ["KAN", "SAM"] } }] })
      : undefined));
    await mount(<TicketSystemsRow projectId="northgate" />);
    expect($('[data-testid="tk-where-lands"]')?.textContent).toBe("KAN tickets land in Ops. SAM tickets land in BOSS dogfood bootstrap.");
    expect($('[data-testid="tk-where-there"]')?.getAttribute("href")).toBe("?area=project&project_id=ops&tab=work&compose=1");
  });

  it("when keys land here, New work names this project and the key, without a made-up number", async () => {
    api((url) => (url.startsWith("/v1/ticket-systems?project_id=") ? json({ project_name: "Northgate Gateway", connections: [JIRA] }) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    expect($('[data-testid="tk-where-new"]')?.textContent).toBe("New work in Northgate Gateway →");
    expect($('[data-testid="tk-where-key"]')?.textContent).toBe("LEND");
    expect($('[data-testid="tk-where"]')?.textContent).not.toMatch(/LEND-\d/);
  });

  it("the folded section's line says what is there", () => {
    const c = parseConnection({ ...JIRA, settings: { ...JIRA.settings, share_with_org: true,
      mappings: [{ external_project: "KAN", boss_project_id: "a", default: true }, { external_project: "SAM", boss_project_id: "b" }] } });
    expect(ticketSummary([c], [], "a")).toBe("Jira · shared · KAN here");
    // Mapped, but not to this project: never "2 projects mapped".
    expect(ticketSummary([c], [], "elsewhere")).toBe("Jira · shared · maps elsewhere");
    expect(ticketSummary([], [parseConnection(HIDDEN)], "a")).toBe("Jira in BOSS dogfood bootstrap · not shared here");
    expect(ticketSummary([], [], "a")).toBe("None yet");
  });

  it("lists a connection, tests it, opens its settings and disconnects only after confirming", async () => {
    const calls = api();
    await mount(<TicketSystemsRow projectId="p1" />);
    expect($('[data-testid="tk-system-wtp_1"]')?.textContent).toContain("northpeak.atlassian.net");
    expect($('[data-testid="tk-maps-wtp_1"]')?.textContent).toBe("LEND");
    expect($('[data-testid="tk-maps-wtp_1"]')?.getAttribute("title")).toBe("LEND → this project (default)");
    expect($('[data-testid="tk-pickup-wtp_1"]')?.textContent).toBe("Label “boss” → ask first");
    expect($('[data-testid="tk-where"]')).not.toBeNull();
    expect(document.body.textContent).not.toContain("whsec");

    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-test-wtp_1"]');
    expect(calls.some((c) => c.method === "POST" && c.url === "/v1/ticket-systems/wtp_1/test")).toBe(true);
    // The guide's promise: "You should see your name and the projects you can see."
    expect($('[data-testid="tk-said"]')?.textContent).toBe("Jira works · signed in as BOSS Bot · sees 2 projects: LEND, OPS.");

    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-off-wtp_1"]');
    expect($('[data-testid="tk-off-confirm"] span')?.getAttribute("title")).toContain("3 work items keep their links");
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await click('[data-testid="tk-off-no"]');
    expect($('[data-testid="tk-off-confirm"]')).toBeNull();
    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-off-wtp_1"]');
    await click('[data-testid="tk-off-yes"]');
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.url)).toEqual(["/v1/ticket-systems/wtp_1"]);

    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-edit-wtp_1"]');
    expect($('[data-testid="tk-drawer"]')?.getAttribute("aria-label")).toBe("Jira settings");
    // The token is never prefilled, and no part of it is shown (not even its last four).
    expect(($('[data-testid="tk-secret-set"]') as HTMLInputElement).value).toBe("Stored in BOSS's vault");
    expect($('[data-testid="tk-drawer"]')?.textContent).not.toContain("a1f3");
    expect($('[data-testid="tk-secret"]')).toBeNull();
  });

  it("an empty project says so with one Add; an error and a 403 say why in words", async () => {
    api((url) => (url.startsWith("/v1/ticket-systems?") ? json({ connections: [] }) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    expect($('[data-testid="tk-systems-empty"]')?.textContent).toContain("No ticket system yet");
    await click('[data-testid="tk-add-empty"]');
    expect($('[data-testid="tk-drawer"]')).not.toBeNull();
    unmount();

    api((url) => (url.startsWith("/v1/ticket-systems?") ? json({ detail: { error: "upstream_error", message: "Jira did not answer." } }, 502) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    expect($('[data-testid="tk-systems-error"]')?.textContent?.trim()).toBe("Ticket systems: Jira did not answer. Try again");
    await click('[data-testid="tk-systems-retry"]');
    unmount();

    api((url) => (url.startsWith("/v1/ticket-systems?") ? json({ detail: "Needs “View integrations”, ask an Admin." }, 403) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    expect($('[data-testid="tk-systems-denied"]')?.textContent).toContain("Needs “View integrations”");
  });
});

describe("Add a ticket system (T2)", () => {
  it("picks Jira, sends the token once, shows the test, maps a project, sets write-back and the rule, saves", async () => {
    const calls = api((url) => (url.startsWith("/v1/ticket-systems?") ? json({ connections: [] }) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-add"]');
    expect($$('[data-testid^="tk-tile-"]').map((t) => t.dataset.testid)).toEqual(["tk-tile-jira", "tk-tile-plane", "tk-tile-yours"]);
    expect($('[data-testid="tk-tile-yours"]')?.getAttribute("href")).toContain("TICKET_CONNECTORS.md");
    await click('[data-testid="tk-tile-plane"]');
    expect($('[data-testid="tk-oauth"]')).toBeNull();
    await click('[data-testid="tk-tile-jira"]');

    // Nothing typed: the reason, and nothing sent.
    await click('[data-testid="tk-signin-test"]');
    expect($('[role="alert"]')?.textContent).toMatch(/Enter the Site URL/);
    expect(calls.filter((c) => c.method === "POST")).toEqual([]);

    await type('[data-testid="tk-field-base"]', "https://northpeak.atlassian.net");
    await type('[data-testid="tk-field-email"]', "boss-bot@northpeak.example");
    await type('[data-testid="tk-field-project_key"]', "LEND");
    await type('[data-testid="tk-secret"]', "atl-secret");
    expect(($('[data-testid="tk-secret"]') as HTMLInputElement).type).toBe("password");
    // Connect with Atlassian waits for a saved connection.
    expect(($('[data-testid="tk-oauth-start"]') as HTMLButtonElement).disabled).toBe(true);
    await click('[data-testid="tk-signin-test"]');
    const connect = calls.find((c) => c.method === "POST" && c.url === "/v1/ticket-systems")!;
    expect(connect.body).toEqual({ project_id: "p1", provider: "jira", base_url: "https://northpeak.atlassian.net",
                                   fields: { email: "boss-bot@northpeak.example", project_key: "LEND" }, secrets: { api_token: "atl-secret" } });
    expect($('[data-testid="tk-test-result"]')?.textContent).toContain("signed in as BOSS Bot");
    // The token field is gone: write-only, never shown again.
    expect($('[data-testid="tk-secret"]')).toBeNull();
    expect(document.body.innerHTML).not.toContain("atl-secret");

    await click('[data-testid="tk-oauth-start"]');
    expect(calls.some((c) => c.url === "/v1/ticket-systems/wtp_1/oauth/start/load" && c.method === "POST")).toBe(true);

    await click('[data-testid="tk-map-add"]');
    await type('[data-testid="tk-map-from-1"]', "OPS");
    await click('[data-testid="tk-map-remove-1"]');
    await click('[data-testid="tk-wb-link_pr"]');
    await type('[data-testid="tk-pickup-label"]', "boss");
    await type('[data-testid="tk-pickup-mode"]', "auto_start");
    // Sharing is a choice, in plain words; a personal API token starts unshared.
    expect(($('[data-testid="tk-share-toggle"]') as HTMLInputElement).checked).toBe(false);
    expect($('[data-testid="tk-share-words"]')?.textContent)
      .toContain("Everyone in Example Labs and their agents can read tickets through this connection using BOSS Bot's token.");
    await click('[data-testid="tk-save"]');
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toBe("/v1/ticket-systems/wtp_1");
    expect(patch.body.share_with_org).toBe(false);
    expect(patch.body).toMatchObject({ mappings: [{ external_project: "LEND", boss_project_id: "p1", default: true }],
                                       write_back: { comment: true, transition: true, link_pr: false },
                                       pickup: { label: "boss", mode: "auto_start" }, inbound: { mode: "webhook", poll_minutes: 5 } });
    expect($('[data-testid="tk-drawer"]')).toBeNull();
    expect($('[data-testid="tk-said"]')?.textContent).toBe("Jira saved.");
  });

  it("Advanced: status names, poll minutes, and a webhook URL + secret shown once with Copy", async () => {
    const calls = api();
    const writes: string[] = [];
    vi.stubGlobal("navigator", { clipboard: { writeText: (t: string) => { writes.push(t); return Promise.resolve(); } } });
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-edit-wtp_1"]');
    await click('[data-testid="tk-adv-toggle"]');
    await click('[data-testid="tk-hook"]');
    expect(calls.some((c) => c.method === "POST" && c.url === "/v1/ticket-systems/wtp_1/webhook")).toBe(true);
    expect($('[data-testid="tk-hook-secret"]')?.textContent).toBe("whsec_ONCE");
    await click('[data-testid="tk-hook-copy-url"]');
    await click('[data-testid="tk-hook-copy-secret"]');
    expect(writes).toEqual(["https://boss.example/v1/webhooks/tickets/wtp_1", "whsec_ONCE"]);
    await type('[data-testid="tk-status-doing"]', "Building");
    await type('[data-testid="tk-inbound-mode"]', "poll");
    await type('[data-testid="tk-poll"]', "10");
    await click('[data-testid="tk-secret-replace"]');
    expect(($('[data-testid="tk-secret"]') as HTMLInputElement).value).toBe("");
    await click('[data-testid="tk-save"]');
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.body).toMatchObject({ status_map: { review: "In Review", doing: "Building" }, inbound: { mode: "poll", poll_minutes: 10 } });
  });

  it("a connector whose token is a service/bot token starts shared", async () => {
    const BOT = { items: CATALOG.items.map((c) => (c.provider === "jira" ? { ...c, share_default: true } : c)) };
    api((url) => (url.startsWith("/v1/ticket-systems?project_id=") ? json({ connections: [] })
      : url.startsWith("/v1/ticket-systems/catalog") ? json(BOT) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-add"]');
    await click('[data-testid="tk-tile-jira"]');
    expect(($('[data-testid="tk-share-toggle"]') as HTMLInputElement).checked).toBe(true);
  });

  it("a refused sign-in says the API's sentence, and Cancel closes without a write", async () => {
    const calls = api((url, method) => (method === "POST" && url === "/v1/ticket-systems"
      ? json({ detail: { error: "auth_failed", message: "Jira refused the token (401)." } }, 424) : url.startsWith("/v1/ticket-systems?") ? json({ connections: [] }) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-add"]');
    await type('[data-testid="tk-field-base"]', "https://x.atlassian.net");
    await type('[data-testid="tk-field-email"]', "a@b.c");
    await type('[data-testid="tk-field-project_key"]', "X");
    await type('[data-testid="tk-secret"]', "bad");
    await click('[data-testid="tk-signin-test"]');
    expect($('[data-testid="tk-test-result"]')?.textContent).toContain("Jira refused the token (401).");
    await click('[data-testid="tk-cancel"]');
    expect($('[data-testid="tk-drawer"]')).toBeNull();
    expect(calls.filter((c) => c.method === "PATCH")).toEqual([]);
  });
});

describe("Connect with Atlassian: the connection's own app (setup guide Part 2)", () => {
  const NO_APP = { ...JIRA, oauth_app: { client_id: "", secret_set: false, from_host: false, callback_url: "" } };
  it("asks for the Client ID and Client secret first, shows the callback URL to paste, and saves them write-only", async () => {
    const writes: string[] = [];
    vi.stubGlobal("navigator", { clipboard: { writeText: (t: string) => { writes.push(t); return Promise.resolve(); } } });
    const calls = api((url) => (url.startsWith("/v1/ticket-systems?") ? json({ connections: [NO_APP] }) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-edit-wtp_1"]');
    // No app yet: Connect waits, and says what to do in words.
    expect(($('[data-testid="tk-oauth-start"]') as HTMLButtonElement).disabled).toBe(true);
    expect($('[data-testid="tk-oauth-why"]')?.textContent).toBe("Add the Atlassian app's Client ID and Client secret below, then Save");
    // The API named no public origin: the callback is this console's own address + the path.
    expect($('[data-testid="tk-oauth-callback"]')?.textContent).toBe(`${window.location.origin}/v1/ticket-systems/oauth/callback`);
    await click('[data-testid="tk-oauth-callback-copy"]');
    expect(writes).toEqual([`${window.location.origin}/v1/ticket-systems/oauth/callback`]);

    // A secret without its client id is said, not sent.
    await type('[data-testid="tk-oauth-client-secret"]', "app-secret-1");
    await click('[data-testid="tk-save"]');
    expect($('[role="alert"]')?.textContent).toBe("Add the client ID with its secret.");
    expect(calls.filter((c) => c.method === "PATCH")).toEqual([]);

    await type('[data-testid="tk-oauth-client-id"]', "atl-app-9");
    expect(($('[data-testid="tk-oauth-client-secret"]') as HTMLInputElement).type).toBe("password");
    await click('[data-testid="tk-save"]');
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.body.oauth_app).toEqual({ client_id: "atl-app-9", client_secret: "app-secret-1" });
    expect($('[data-testid="tk-said"]')?.textContent).toBe("Jira saved.");
  });

  it("with an app stored: Connect starts, the secret says Stored, and a save without changes sends no app", async () => {
    const calls = api();
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-edit-wtp_1"]');
    expect(($('[data-testid="tk-oauth-client-id"]') as HTMLInputElement).value).toBe("atl-app");
    expect(($('[data-testid="tk-oauth-client-secret"]') as HTMLInputElement).placeholder).toBe("Stored, type to replace");
    expect($('[data-testid="tk-oauth-callback"]')?.textContent).toBe("https://boss.example/v1/ticket-systems/oauth/callback");
    expect($('[data-testid="tk-oauth-why"]')).toBeNull();
    await click('[data-testid="tk-oauth-start"]');
    expect(calls.some((c) => c.url === "/v1/ticket-systems/wtp_1/oauth/start/load" && c.method === "POST")).toBe(true);
    await click('[data-testid="tk-save"]');
    expect(calls.find((c) => c.method === "PATCH")!.body).not.toHaveProperty("oauth_app");
  });

  it("knows when sign-in can start: an app on the connection or on the host", () => {
    expect(oauthReady(parseConnection(JIRA))).toBe(true);
    expect(oauthReady(parseConnection(NO_APP))).toBe(false);
    expect(oauthReady(parseConnection({ ...NO_APP, oauth_app: { ...NO_APP.oauth_app, from_host: true } }))).toBe(true);
    expect(oauthReady(parseConnection({ ...JIRA, oauth_app: null }))).toBe(false);
  });
});

describe("the Add drawer's words", () => {
  it("a tile's second line never repeats its name, and the rule reads Ask me first / Start automatically", async () => {
    expect(tileLine({ label: "GitHub Issues", description: "GitHub Issues — track work next to the code." })).toBe("Track work next to the code");
    expect(tileLine({ label: "Jira", description: "Atlassian Jira Cloud — issues, bugs, and epics." })).toBe("Atlassian Jira Cloud");
    api();
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-edit-wtp_1"]');
    expect($$('[data-testid="tk-pickup-mode"] option').map((o) => o.textContent)).toEqual(["Ask me first", "Start automatically"]);
    expect($('[data-testid="tk-secret-replace"]')?.textContent).toBe("Replace API token");
  });

  it("a mapping can send a project's tickets to another BOSS project", async () => {
    const calls = api((url) => (url === "/v1/projects" ? json({ items: [{ id: "p1", name: "Lending" }, { id: "p2", name: "Payments" }] }) : undefined));
    await mount(<TicketSystemsRow projectId="p1" />);
    await click('[data-testid="tk-menu-wtp_1"]');
    await click('[data-testid="tk-edit-wtp_1"]');
    expect($$('[data-testid="tk-map-to-0"] option').map((o) => o.textContent)).toEqual(["Lending (this project)", "Payments"]);
    await type('[data-testid="tk-map-to-0"]', "p2");
    await click('[data-testid="tk-save"]');
    expect(calls.find((c) => c.method === "PATCH")!.body.mappings).toEqual([{ external_project: "LEND", boss_project_id: "p2", default: true }]);
  });
});

describe("Skills & MCP › File tickets points at Setup", () => {
  it("shows the one place to configure, and keeps the Kit form folded", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({})));
    const ability = { key: "file_tickets", label: "File tickets", status: "setup", connection: { provider: "linear", providers: [], set_fields: {}, connected: false },
                      tools: [], holders: [], last_test: null } as unknown as Parameters<typeof ConnectionSection>[0]["ability"];
    await mount(<ConnectionSection ability={ability} projectId="p1" actor="ceo" onChanged={() => {}} />);
    expect($('[data-testid="kit-conn-tickets-open"]')?.getAttribute("href")).toBe("?area=setup&focus=ticket_systems&project_id=p1");
    expect($('[data-testid="kit-conn-tickets-fold"]')?.hasAttribute("open")).toBe(false);
  });
});
