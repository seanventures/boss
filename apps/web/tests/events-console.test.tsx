// @vitest-environment happy-dom
/**
 * SETUP › EVENTS (P9a-7, BOSSAPP-1099; wireframes E1 E2 E4 E5 E6 E7), each screen's behaviour:
 *  - the rule sentence ⇄ the route filter language, and the event as a flow (pure);
 *  - E1 tiles and source cards, Test, a viewer's Connect pre-greyed;
 *  - E2 Slack: secrets write-only and sent once, endpoint then connection, Socket Mode's token,
 *    the SSO attestation shown only to an identity-provider admin;
 *  - E4 rules across agents in order, the live "would match", Save with identity / limits /
 *    budget / quiet hours / triage off by default;
 *  - E5 chips filter, an event opens as a flow;
 *  - E6 reprocess, force needs a reason;
 *  - E7 people with method chips, revoke after confirming, attestation Turn off for admins only.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const denied = new Set<string>();
vi.mock("../components/identity/identity-provider", async (orig) => ({
  ...(await orig<typeof import("../components/identity/identity-provider")>()),
  useCan: (p: string) => (denied.has(p)
    ? { allowed: false, reason: `Needs “${p}”, ask an Admin`, needsStepUp: false }
    : { allowed: true, reason: "", needsStepUp: false }),
}));

import { EventsHome } from "../app/events/events-home";
import { eventFlow, flowSentence, parseEventDetail, parsePreview } from "../lib/intake";
import { emptyDraft, fromMatch, ruleWords, toMatch } from "../lib/intake-rules";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

const PID = "p1";
const SLACK = {
  id: "icn_s", project_id: PID, kind: "slack", label: "Slack", name: "northpeak", mode: "push", status: "active",
  config: { chat_endpoint_id: "che_1", team_id: "T0NP" }, secret_fields: [], url_path: "/v1/intake/slack/tok", chat_endpoint_id: "che_1",
  limits: {}, health: {}, rules: 2, last_hour: 840, backlog: 0, dead: 0, last_event_at: new Date(Date.now() - 8000).toISOString(),
};
const HOOK = { ...SLACK, id: "icn_w", kind: "webhook", label: "Generic webhook", name: "Grafana alerts", config: {}, chat_endpoint_id: null,
  secret_fields: ["signing_secret"], rules: 1, last_hour: 3, dead: 2 };
const now = Date.now();
const iso = (msAgo: number) => new Date(now - msAgo).toISOString();
const EVENTS = {
  events: [
    { id: "iev_1", connection_id: "icn_s", connection_name: "northpeak", kind: "slack", status: "routed", type: "com.slack.message.app_mention",
      subject: "C0SUPPORT/1", text: "why did last night's payments job fail?", actor: { kind: "user", display: "priya", external_id: "U0PRIYA" },
      runs: ["arun_4127"], received_at: iso(60_000), event_time: iso(60_100), processed_at: iso(59_000), attempts: 1, duplicates: 0 },
    { id: "iev_2", connection_id: "icn_s", connection_name: "northpeak", kind: "slack", status: "refused", type: "com.slack.message.app_mention",
      subject: "C0SUPPORT/2", text: "can you check my PR?", actor: { kind: "user", display: "dev.k", external_id: "U0DEV" },
      runs: [], received_at: iso(120_000), event_time: iso(120_050), processed_at: iso(119_000), attempts: 1, duplicates: 0 },
  ],
  counts: { routed: 316, ignored: 4410, filtered: 61, refused: 12, dead: 3, digest: 1, failed: 0, shed: 0, received: 0, processing: 0, total: 4803 },
};
const DETAIL_ROUTED = {
  ...EVENTS.events[0], dedupe_key: "slack:Ev1", conversation_key: "C0SUPPORT/1", verify_scheme: "slack_v0",
  envelope: { data: { channel: "C0SUPPORT", text: "why did…" }, source: "slack://T0NP" }, screen: { verdict: "clean" }, screen_verdict: "clean",
  outcome: [{ trigger_id: "atrg_1", fire_outcome: "admitted", run_id: "arun_4127", thread_id: "thr_1", reason: "matched route Mentions in #support",
              subject: { kind: "person", mode: "person", method: "sso_email", display: "Priya", principal_id: "usr_p" } }],
  fires: [{ id: "f1", trigger_id: "atrg_1", outcome: "admitted", run_id: "arun_4127", trigger_name: "Mentions in #support", fired_at: iso(59_000) }],
  raw: { event: { text: "hi" } }, people_redacted: false,
};
const DETAIL_REFUSED = {
  ...EVENTS.events[1], dedupe_key: "slack:Ev2", verify_scheme: "slack_v0", envelope: { data: { channel: "C0SUPPORT" } }, screen: {},
  outcome: [{ trigger_id: "atrg_1", fire_outcome: "refused", run_id: null, reason: "unlinked sender: unlinked (link prompt sent)",
              subject: { kind: "none", mode: "person", method: null, display: "dev.k", reason: "unlinked (no_attestation)" } }],
  fires: [], raw: null,
};
const RULE = (id: string, agent: string, priority: number, name: string, match: unknown, addressing = "mention_or_dm") => ({
  id, kind: "intake", name, agent_key: agent, environment: "prod", connection_id: "icn_s", priority, status: "on", on: true,
  identity_mode: "person", identity_modes: ["person", "person_or_owner", "owner"], config: { match, addressing }, limits: {}, quiet_hours: {},
  match_ok: true, fires: 3,
});
const T = "com.slack.message.thread_reply";
const RULES: Record<string, unknown[]> = {
  support: [RULE("atrg_2", "support", 20, "DMs to BOSS", { field: "type", op: "in", value: ["com.slack.message.im", T] }),
            RULE("atrg_1", "support", 10, "Mentions in #support", { all: [{ field: "type", op: "in", value: ["com.slack.message.app_mention", T] },
              { field: "data.channel", op: "in", value: ["C0SUPPORT"] }, { not: { field: "data.text", op: "prefix", value: "ignore" } }] })],
  billing: [RULE("atrg_3", "billing", 30, "", { all: [{ field: "type", op: "in", value: ["com.slack.message.channel", T] },
              { field: "data.channel", op: "in", value: ["C0PAYMENTS"] }] }, "channel_all")],
};
const AGENTS = { agents: [{ key: "support", name: "Support agent", live: [{ environment: "prod" }], cost_7d_usd: 17.64 },
                          { key: "billing", name: "Billing agent", live: [{ environment: "prod" }], cost_7d_usd: 0 }] };
const PEOPLE = {
  connection_id: "icn_s", kind: "slack", endpoint_id: "che_1",
  attestation: { idp: "idp_okta", idp_name: "Okta", domains: ["northpeak.com"], attested_by: "Alice Chen", attested_at: "2026-11-03T10:00:00Z" },
  counts: { linked: 2, not_linked: 1, guests: 1, revoked: 0 },
  people: [
    { external_user_id: "U0PRIYA", display: "priya", state: "linked", link_id: "chl_p", method: "sso_email", person: { principal_id: "usr_p", name: "Priya Raman", email: "priya@northpeak.com" }, linked_at: iso(120_000) },
    { external_user_id: "U0MARCO", display: "marco.b", state: "linked", link_id: "chl_m", method: "code", person: { principal_id: "usr_m", name: "Marco Bianchi", email: "marco@northpeak.com" }, linked_at: iso(3_600_000) },
    { external_user_id: "U0DEV", display: "dev.k", state: "not_linked", prompted_at: iso(7_200_000) },
    { external_user_id: "U0ANA", display: "ana.vendor", state: "guest" },
  ],
};

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
type Call = { url: string; method: string; body: Record<string, unknown> };
function api(over: (url: string, method: string, body: Record<string, unknown>) => Response | undefined = () => undefined) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (u: string, init?: RequestInit) => {
    const url = String(u);
    const method = String(init?.method ?? "GET");
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ url, method, body });
    const o = over(url, method, body);
    if (o) return o;
    if (url.startsWith("/v1/intake/connections?")) return json({ connections: [SLACK, HOOK] });
    if (url.startsWith("/v1/agent-platform/agents")) return json(AGENTS);
    if (url.startsWith("/v1/intake/events?")) return json(EVENTS);
    if (url.startsWith("/v1/intake/events/iev_1")) return json(DETAIL_ROUTED);
    if (url.startsWith("/v1/intake/events/iev_2")) return json(DETAIL_REFUSED);
    if (url.startsWith("/v1/threads/")) return json({ status: "completed" });
    if (url.startsWith("/v1/intake/catalog")) return json({ sources: [{ key: "slack", label: "Slack", modes: ["push", "socket"], auth_fields: [], event_types: [] },
      { key: "webhook", label: "Generic webhook", modes: ["push"], description: "Any system. More.", auth_fields: [{ name: "signing_secret", label: "Signing secret", secret: true }], event_types: ["com.boss.webhook.received"] }] });
    const m = /\/v1\/agents\/(\w+)\/triggers\?/.exec(url);
    if (m && method === "GET") return json({ triggers: RULES[m[1]] ?? [] });
    if (url.includes("/preview-match")) return json({ ok: true, matched: 7, considered: 50, events: [
      { id: "e1", matched: true, text: "@BOSS why did the job fail?", actor: "priya" }, { id: "e2", matched: false, text: "lunch?", actor: "dev" }] });
    if (url.startsWith("/v1/budget-policies/scoped?")) return json({ policies: [{ id: "sbp_1", scope_type: "trigger", scope_id: "atrg_1", cap_usd_micros: 5_000_000, spent_usd_micros: 840_000, period: "day" }] });
    if (url.startsWith("/v1/intake/dead-letters")) return json({ dead_letters: [
      { ...EVENTS.events[1], id: "iev_d1", kind: "webhook", status: "dead", text: "INC0012299 updated · priority 1", actor: { kind: "service", display: "" },
        last_error: "RuntimeError: ServiceNow refused our login.", attempts: 6, routes: ["atrg_9"], first_failed_at: iso(3_600_000) },
      { ...EVENTS.events[1], id: "iev_d2", kind: "webhook", status: "dead", text: "INC0012305 created", actor: { kind: "service", display: "" },
        last_error: "TimeoutError: timed out", attempts: 5, routes: [], first_failed_at: iso(1_800_000) }],
      per_connection: [{ connection_id: "icn_s", kind: "slack", name: "northpeak", dead: 0 }, { connection_id: "icn_w", kind: "webhook", name: "Grafana alerts", dead: 2 }] });
    if (url.endsWith("/people")) return json(PEOPLE);
    if (url.startsWith("/v1/chat/endpoints?")) return json({ endpoints: [] });
    if (url.startsWith("/v1/identity/providers")) return json({ providers: [{ id: "idp_okta", name: "Okta", domains: ["northpeak.com"], status: "active" }] });
    if (url.endsWith("/test")) return json({ ok: true, checks: { "secret:signing_secret": "ok" }, last_hour: 3 });
    if (method === "POST" && url === "/v1/chat/endpoints") return json({ id: "che_new", kind: "slack", name: "Slack", status: "active", config: { channel: "C0SUPPORT" }, urls: {} }, 201);
    if (method === "POST" && url === "/v1/intake/connections") return json({ ...SLACK, id: "icn_new" }, 201);
    if (method === "POST" && /\/v1\/agents\/\w+\/triggers$/.test(url)) return json(RULE("atrg_new", "support", 40, "new", body.config && (body.config as { match?: unknown }).match), 201);
    if (method === "PATCH") return json(RULE(url.split("/").pop()!.split("?")[0], "support", 10, "Mentions in #support", null));
    return json({});
  }));
  return calls;
}
const type = async (sel: string, value: string) => {
  const el = $(sel) as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  await settle();
};
const place = (tab: string, extra: Partial<{ conn: string; ev: string; kind: string }> = {}) =>
  ({ tab: tab as never, conn: extra.conn ?? "", ev: extra.ev ?? "", kind: extra.kind ?? "" });
const at = (url: string) => window.history.replaceState(null, "", url);

beforeEach(() => { denied.clear(); at("/?area=setup"); });
afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("the rule sentence and the filter language", () => {
  it("a chat mention in a channel, with a negated condition, round-trips", () => {
    const d = { ...emptyDraft(true), channels: ["support"], conds: [{ field: "data.text", op: "prefix" as const, value: "ignore", not: true }] };
    const { match, addressing } = toMatch(d, true);
    expect(addressing).toBe("mention_or_dm");
    expect(match).toEqual({ all: [{ field: "type", op: "in", value: ["com.slack.message.app_mention", T] },
      { field: "data.channel", op: "in", value: ["support"] }, { not: { field: "data.text", op: "prefix", value: "ignore" } }] });
    const back = fromMatch(match, "mention_or_dm", true);
    expect(back).toMatchObject({ when: "mention", channels: ["support"], followups: true, custom: null });
    expect(ruleWords(back, true)).toBe("Mentions in #support · text doesn't start with “ignore”");
  });
  it("any message is channel_all; follow-ups only is thread_followups; a DM ignores channels", () => {
    expect(toMatch({ ...emptyDraft(true), when: "message", channels: ["C1"] }, true).addressing).toBe("channel_all");
    expect(toMatch({ ...emptyDraft(true), when: "followup" }, true).addressing).toBe("thread_followups");
    expect(toMatch({ ...emptyDraft(true), when: "dm", channels: ["C1"], followups: false }, true).match).toEqual({ field: "type", op: "in", value: ["com.slack.message.im"] });
  });
  it("a filter the sentence cannot say stays as it is (custom)", () => {
    const odd = { any: [{ field: "type", op: "eq", value: "x" }, { field: "subject", op: "eq", value: "y" }] };
    const d = fromMatch(odd, "mention_or_dm", true);
    expect(d.custom).toEqual(odd);
    expect(toMatch(d, true).match).toEqual(odd);
    expect(ruleWords(d, true)).toBe("Custom filter");
  });
  it("a record source picks an event type, wildcards become a prefix", () => {
    const { match } = toMatch({ ...emptyDraft(false), when: "com.github.issues.*" }, false);
    expect(match).toEqual({ field: "type", op: "prefix", value: "com.github.issues." });
    expect(fromMatch(match, "", false).when).toBe("com.github.issues.*");
  });
});

describe("an event as a flow (E5's detail)", () => {
  it("routed: source → checked → saved → rule → for Priya → run → reply", () => {
    const nodes = eventFlow(parseEventDetail(DETAIL_ROUTED), () => "#1 mentions", true);
    expect(nodes.map((n) => [n.key, n.state])).toEqual([["source", "done"], ["checked", "done"], ["saved", "done"], ["rule", "done"],
      ["whom", "done"], ["run", "done"], ["reply", "done"]]);
    expect(nodes.find((n) => n.key === "whom")).toMatchObject({ label: "For Priya", detail: "SSO email" });
    expect(flowSentence(nodes)).toContain("Run 4127");
  });
  it("refused: it stops at For whom? (not linked) and a link is sent", () => {
    const nodes = eventFlow(parseEventDetail(DETAIL_REFUSED), () => "#1");
    expect(nodes.slice(-2).map((n) => [n.label, n.state])).toEqual([["For whom?", "failed"], ["Link sent", "done"]]);
  });
});

describe("E1 Connections", () => {
  it("draws the tiles and one card per source, and Test says what it checked", async () => {
    const calls = api();
    await mount(<EventsHome projectId={PID} place={place("connections")} />);
    expect($('[data-testid="ev-tile-total"]')?.textContent).toContain("4,803");
    expect($('[data-testid="ev-tile-routed"]')?.textContent).toContain("316");
    expect($('[data-testid="ev-src-icn_s"]')?.textContent).toContain("northpeak");
    expect($('[data-testid="ev-src-state-icn_s"]')?.textContent).toBe("Live");
    expect($('[data-testid="ev-src-icn_s"]')?.textContent).toContain("14/min");
    expect($('[data-testid="ev-src-rules-icn_s"]')?.textContent).toContain("2 rules");
    expect($('[data-testid="ev-coming-teams"]')).not.toBeNull();
    await click($('[data-testid="ev-src-menu-icn_s"]'));
    await click($('[data-testid="ev-src-test-icn_s"]'));
    expect(calls.some((c) => c.method === "POST" && c.url === "/v1/intake/connections/icn_s/test")).toBe(true);
    expect($('[data-testid="ev-conn-said"]')?.textContent).toContain("Slack works");
  });
  it("a viewer sees Connect a source pre-greyed with the reason, and it does nothing", async () => {
    denied.add("integration:manage");
    api();
    await mount(<EventsHome projectId={PID} place={place("connections")} />);
    const b = $('[data-testid="ev-connect"]')!;
    expect(b.getAttribute("aria-disabled")).toBe("true");
    expect(b.getAttribute("title")).toContain("integration:manage");
    await click(b);
    expect($('[data-testid="ev-connect-sheet"]')).toBeNull();
  });
  it("Connect a source → a webhook: the URL and the minted secret shown once", async () => {
    const calls = api((url, method) => (method === "POST" && url === "/v1/intake/connections"
      ? json({ ...HOOK, id: "icn_n", url: "https://boss.example/v1/intake/webhook/tok", secret: "whsec_ONCE" }, 201) : undefined));
    await mount(<EventsHome projectId={PID} place={place("connections")} />);
    await click($('[data-testid="ev-connect"]'));
    await click($('[data-testid="ev-pick-webhook"]'));
    await click($('[data-testid="ev-connect-save"]'));
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/intake/connections")!;
    expect(post.body).toMatchObject({ project_id: PID, kind: "webhook", mode: "push", secrets: {} });
    expect($('[data-testid="ev-connect-url"]')?.textContent).toBe("https://boss.example/v1/intake/webhook/tok");
    expect($('[data-testid="ev-connect-secret"]')?.textContent).toBe("whsec_ONCE");
  });
});

describe("E2 Connect Slack", () => {
  it("Save waits for both secrets and the channel, then makes the endpoint and the connection riding on it", async () => {
    at("/?area=setup&events=connect&kind=slack");
    const calls = api((url) => (url.startsWith("/v1/intake/connections?") ? json({ connections: [] }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("connect", { kind: "slack" })} />);
    const save = $('[data-testid="ev-slack-save"]') as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(($('[data-testid="ev-slack-signing"]') as HTMLInputElement).type).toBe("password");
    await type('[data-testid="ev-slack-signing"]', "sig-secret");
    await type('[data-testid="ev-slack-bot"]', "xoxb-1");
    await type('[data-testid="ev-slack-channel"]', "#C0SUPPORT");
    await click($('[data-testid="ev-slack-socket"]'));
    expect(save.disabled).toBe(true);                       // Socket Mode needs the app-level token
    await type('[data-testid="ev-slack-apptoken"]', "xapp-1");
    expect(save.disabled).toBe(false);
    await click(save);
    const ep = calls.find((c) => c.method === "POST" && c.url === "/v1/chat/endpoints")!;
    expect(ep.body).toMatchObject({ kind: "slack", config: { channel: "C0SUPPORT" }, secrets: { signing_secret: "sig-secret", bot_token: "xoxb-1" } });
    const conn = calls.find((c) => c.method === "POST" && c.url === "/v1/intake/connections")!;
    expect(conn.body).toMatchObject({ kind: "slack", mode: "socket", config: { chat_endpoint_id: "che_new" }, secrets: { app_token: "xapp-1" } });
    expect(calls.some((c) => c.url === "/v1/intake/connections/icn_new/test")).toBe(true);
    expect($('[data-testid="ev-slack-flow-signed"]')?.getAttribute("data-state")).toBe("done");
    expect(($('[data-testid="ev-slack-signing"]') as HTMLInputElement).value).toBe("");   // the secrets are gone from the page
  });
  it("the SSO attestation is shown to an identity-provider admin only, and confirmed after save", async () => {
    at("/?area=setup&events=connect&kind=slack");
    denied.add("identity_provider:manage");
    api((url) => (url.startsWith("/v1/intake/connections?") ? json({ connections: [] }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("connect", { kind: "slack" })} />);
    expect($('[data-testid="ev-slack-sso"]')).toBeNull();
    unmount();
    denied.clear();
    const calls = api((url) => (url.startsWith("/v1/intake/connections?") ? json({ connections: [] }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("connect", { kind: "slack" })} />);
    expect($('[data-testid="ev-slack-sso"]')?.textContent).toContain("signs in with Okta");
    await click($('[data-testid="ev-slack-attest"]'));
    await type('[data-testid="ev-slack-signing"]', "s");
    await type('[data-testid="ev-slack-bot"]', "xoxb");
    await type('[data-testid="ev-slack-channel"]', "C1");
    await click($('[data-testid="ev-slack-save"]'));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/v1/intake/connections/icn_new/sso-attestation");
    expect(put.body).toEqual({ identity_provider_id: "idp_okta", domains: ["northpeak.com"] });
  });
});

describe("E4 Rules", () => {
  it("lists the connection's rules across agents in order, opens the first, and shows would-match", async () => {
    const calls = api();
    await mount(<EventsHome projectId={PID} place={place("rules", { conn: "icn_s" })} />);
    const ids = $$('[data-testid^="ev-rule-open-"]').map((b) => b.getAttribute("data-testid"));
    expect(ids).toEqual(["ev-rule-open-atrg_1", "ev-rule-open-atrg_2", "ev-rule-open-atrg_3"]);
    expect($('[data-testid="ev-rule-open-atrg_3"]')?.textContent).toContain("Every message in #C0PAYMENTS");
    await new Promise((r) => setTimeout(r, 450)); await settle();
    const pv = calls.find((c) => c.url.includes("/preview-match"))!;
    expect(pv.body).toMatchObject({ project_id: PID, connection_id: "icn_s", limit: 50 });
    expect($('[data-testid="ev-match-count"]')?.textContent).toBe("Would match 7 of the last 50 events");
    expect(($('[data-testid="ev-budget"]') as HTMLInputElement).value).toBe("5.00");
    expect($('[data-testid="ev-triage"]')?.getAttribute("aria-checked")).toBe("false");
  });
  it("a preview read without View people says live results may differ (BOSSAPP-1199)", async () => {
    expect(parsePreview({ ok: true, matched: 1, considered: 2, events: [], people_redacted: true }).people_redacted).toBe(true);
    expect(parsePreview({ ok: true, matched: 1, considered: 2, events: [] }).people_redacted).toBe(false);
    api((url) => (url.includes("/preview-match") ? json({ ok: true, matched: 3, considered: 50, events: [], people_redacted: true }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("rules", { conn: "icn_s" })} />);
    await new Promise((r) => setTimeout(r, 450)); await settle();
    const note = $('[data-testid="ev-match-people-hidden"]');
    expect(note?.textContent).toBe("People hidden · live results may differ");
    expect(note?.getAttribute("title")).toContain("may match live events the preview did not");
    expect(note?.className).toBe("ev-faint");
  });
  it("a preview with people shown carries no such note", async () => {
    api();
    await mount(<EventsHome projectId={PID} place={place("rules", { conn: "icn_s" })} />);
    await new Promise((r) => setTimeout(r, 450)); await settle();
    expect($('[data-testid="ev-match-count"]')?.textContent).toBe("Would match 7 of the last 50 events");
    expect($('[data-testid="ev-match-people-hidden"]')).toBeNull();
  });
  it("Save sends the sentence as the filter, the acting-for, limits, quiet hours and the daily budget", async () => {
    const calls = api();
    await mount(<EventsHome projectId={PID} place={place("rules", { conn: "icn_s" })} />);
    await click($('[data-testid="ev-mode-owner"]'));
    await type('[data-testid="ev-limit"]', "12");
    await click($('[data-testid="ev-quiet-ack_and_defer"]'));
    await type('[data-testid="ev-budget"]', "3");
    await click($('[data-testid="ev-rule-save"]'));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toBe("/v1/agent-triggers/atrg_1");
    expect(patch.body).toMatchObject({ identity_mode: "owner", limits: { per_entity: 12, per_entity_window_seconds: 3600 },
      quiet_hours: { start: "22:00", end: "08:00", mode: "ack_and_defer" } });
    expect((patch.body.config as { match: unknown; triage: unknown }).triage).toEqual({ mode: "off" });
    expect((patch.body.config as { match: { all: unknown[] } }).match.all).toHaveLength(3);
    const budget = calls.find((c) => c.method === "POST" && c.url === "/v1/budget-policies/scoped")!;
    expect(budget.body).toMatchObject({ scope_type: "trigger", scope_id: "atrg_1", cap_usd_micros: 3_000_000, period: "day", hard_stop: true });
  });
  it("a new rule goes on the chosen agent's live environment with the connection; triage on carries its caps", async () => {
    const calls = api();
    await mount(<EventsHome projectId={PID} place={place("rules", { conn: "icn_s" })} />);
    await click($('[data-testid="ev-rule-new"]'));
    await type('[data-testid="ev-when"]', "dm");
    await click($('[data-testid="ev-triage"]'));
    await click($('[data-testid="ev-rule-save"]'));
    const post = calls.find((c) => c.method === "POST" && /\/v1\/agents\/support\/triggers$/.test(c.url))!;
    expect(post.body).toMatchObject({ kind: "intake", environment: "prod", connection_id: "icn_s", priority: 40, identity_mode: "person" });
    expect((post.body.config as { triage: Record<string, unknown> }).triage).toMatchObject({ mode: "model", daily_usd_cap: 5, max_usd_per_event: 0.002 });
  });
  it("a failed budget, budget removal or triage step is said in red, never as a green saved", async () => {
    const calls = api((url, method) => (url.startsWith("/v1/budget-policies/scoped") && method !== "GET"
      ? json({ error: "scoped_budget_refused", detail: "No such trigger or route in this org." }, 422) : undefined));
    await mount(<EventsHome projectId={PID} actor="ceo" place={place("rules", { conn: "icn_s" })} />);
    await type('[data-testid="ev-budget"]', "3");
    await click($('[data-testid="ev-rule-save"]'));
    expect($('[data-testid="ev-rules-said"]')?.getAttribute("data-tone")).toBe("red");
    expect($('[data-testid="ev-rules-said"]')?.textContent).toContain("the daily budget was not set (No such trigger or route in this org.)");
    await type('[data-testid="ev-budget"]', "");
    await click($('[data-testid="ev-rule-save"]'));
    expect(calls.some((c) => c.method === "DELETE" && c.url.startsWith("/v1/budget-policies/scoped/sbp_1"))).toBe(true);
    expect($('[data-testid="ev-rules-said"]')?.textContent).toContain("the daily budget was not removed");
    expect(calls.filter((c) => c.method === "PATCH").every((c) => c.body.actor === "ceo")).toBe(true);
  });
  it("without agent:deploy, New rule and Save are pre-greyed; without agent:lifecycle the switch is", async () => {
    denied.add("agent:deploy"); denied.add("agent:lifecycle");
    api();
    await mount(<EventsHome projectId={PID} place={place("rules", { conn: "icn_s" })} />);
    expect($('[data-testid="ev-rule-new"]')?.getAttribute("aria-disabled")).toBe("true");
    expect($('[data-testid="ev-rule-save"]')?.getAttribute("aria-disabled")).toBe("true");
    expect($('[data-testid="ev-rule-switch-atrg_1"]')?.getAttribute("aria-disabled")).toBe("true");
  });
});

describe("E5 Stream", () => {
  it("chips carry the counts and filter; an event opens as a flow", async () => {
    const calls = api();
    await mount(<EventsHome projectId={PID} place={place("stream")} />);
    expect($('[data-testid="ev-chip-routed"]')?.textContent).toBe("Routed · 316");
    expect($('[data-testid="ev-chip-ignored"]')?.textContent).toBe("Ignored · 4,410");
    await click($('[data-testid="ev-chip-refused"]'));
    expect(calls.some((c) => c.url.includes("/v1/intake/events?") && c.url.includes("status=refused"))).toBe(true);
    expect($('[data-testid="ev-detail-flow"]')?.getAttribute("aria-label")).toContain("Rule Mentions in #support");
    expect($('[data-testid="ev-detail-flow-reply"]')?.getAttribute("data-state")).toBe("done");
    at("/?area=setup&events=stream&ev=iev_2");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  it("a redacted search that stopped at its cap says so; a full one says nothing (BOSSAPP-1199)", async () => {
    api((url) => (url.startsWith("/v1/intake/events?") ? json({ ...EVENTS, search_truncated: true, search_limit: 5000, scanned: 5000 }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("stream")} />);
    const note = $('[data-testid="ev-search-truncated"]');
    expect(note?.textContent).toBe("Searched the newest 5,000 events");
    expect(note?.getAttribute("title")).toContain("Narrow the period or source");
    expect(note?.className).toBe("ev-faint");
    unmount();
    api();
    await mount(<EventsHome projectId={PID} place={place("stream")} />);
    expect($('[data-testid="ev-search-truncated"]')).toBeNull();
  });
});

describe("E5 Stream · who the chat user is in BOSS (BOSSAPP-1196)", () => {
  it("with user:read the detail names the linked person", async () => {
    api();
    await mount(<EventsHome projectId={PID} place={place("stream", { ev: "iev_1" })} />);
    const actor = $('[data-testid="ev-detail-actor"]')?.textContent ?? "";
    expect(actor).toContain("U0PRIYA → Priya");
    expect($('[data-testid="ev-detail-person-hidden"]')).toBeNull();
  });
  it("without user:read the API withholds the person and the detail says name hidden", async () => {
    const redacted = { ...DETAIL_ROUTED, people_redacted: true, principal_id: null,
      outcome: DETAIL_ROUTED.outcome.map((o) => ({ ...o, subject: { ...o.subject, principal_id: null } })) };
    api((url) => (url.startsWith("/v1/intake/events/iev_1") ? json(redacted) : undefined));
    await mount(<EventsHome projectId={PID} place={place("stream", { ev: "iev_1" })} />);
    const hidden = $('[data-testid="ev-detail-person-hidden"]');
    expect(hidden?.textContent).toBe("A BOSS person · name hidden");
    expect(hidden?.getAttribute("title")).toContain("Owner, Admin or Auditor");
    expect($('[data-testid="ev-detail-actor"]')?.textContent).not.toContain("usr_p");
  });
  it("fails closed: a detail without the flag is treated as withheld", async () => {
    const { people_redacted: _drop, ...noFlag } = DETAIL_ROUTED;
    expect(parseEventDetail(noFlag).people_redacted).toBe(true);
    expect(parseEventDetail(DETAIL_ROUTED).people_redacted).toBe(false);
    api((url) => (url.startsWith("/v1/intake/events/iev_1") ? json(noFlag) : undefined));
    await mount(<EventsHome projectId={PID} place={place("stream", { ev: "iev_1" })} />);
    expect($('[data-testid="ev-detail-person-hidden"]')).not.toBeNull();
    expect($('[data-testid="ev-detail-actor"]')?.textContent).not.toContain("usr_p");
  });
});

describe("E6 Dead letters", () => {
  it("reprocesses the ticked events; force needs a typed reason", async () => {
    const calls = api((url, method) => (method === "POST" && url === "/v1/intake/events/replay" ? json({ replayed: ["iev_d1"], skipped: [] }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("dead")} />);
    expect($('[data-testid="ev-dl-iev_d1"]')?.textContent).toContain("ServiceNow refused our login.");
    expect($('[data-testid="ev-calm-icn_s"]')?.textContent).toContain("northpeak: nothing stuck");
    await click($('[data-testid="ev-dl-pick-iev_d1"]'));
    await click($('[data-testid="ev-dl-reprocess"]'));
    expect(calls.find((c) => c.url === "/v1/intake/events/replay")!.body).toMatchObject({ intake_event_ids: ["iev_d1"], mode: "reprocess", reason: "" });
    await click($('[data-testid="ev-dl-pick-iev_d2"]'));
    await click($('[data-testid="ev-dl-force"]'));
    expect(($('[data-testid="ev-force-go"]') as HTMLButtonElement).disabled).toBe(true);
    await type('[data-testid="ev-force-reason"]', "Password rotated and checked with Alice.");
    await click($('[data-testid="ev-force-go"]'));
    const force = calls.filter((c) => c.url === "/v1/intake/events/replay").pop()!;
    expect(force.body).toMatchObject({ intake_event_ids: ["iev_d2"], mode: "force", reason: "Password rotated and checked with Alice." });
  });
});

describe("E7 People", () => {
  it("lists linked and unlinked people with a method chip, revokes after confirming", async () => {
    const calls = api();
    await mount(<EventsHome projectId={PID} place={place("people", { conn: "icn_s" })} />);
    expect($('[data-testid="ev-attestation"]')?.textContent).toContain("SSO auto-link on · Okta · northpeak.com");
    expect($('[data-testid="ev-person-U0PRIYA"]')?.textContent).toContain("SSO email");
    expect($('[data-testid="ev-person-U0MARCO"]')?.textContent).toContain("Code");
    expect($('[data-testid="ev-person-U0DEV"]')?.textContent).toContain("Link sent");
    expect($('[data-testid="ev-person-U0ANA"]')?.textContent).toContain("Guest · never auto-linked");
    await click($('[data-testid="ev-people-filter-guest"]'));
    expect($$('[data-testid^="ev-person-"]').length).toBe(1);
    await click($('[data-testid="ev-people-filter-all"]'));
    await click($('[data-testid="ev-revoke-U0PRIYA"]'));
    expect(calls.some((c) => c.url.includes("/revoke"))).toBe(false);
    await click($('[data-testid="ev-revoke-yes-U0PRIYA"]'));
    expect(calls.find((c) => c.url.includes("/revoke"))!.url).toBe("/v1/intake/connections/icn_s/people/chl_p/revoke");
  });
  it("without user:read the API withholds who each person is, and the row says so", async () => {
    api((url) => (url.endsWith("/people") ? json({ ...PEOPLE, people_redacted: true,
      people: PEOPLE.people.map((p) => ({ ...p, person: null })) }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("people", { conn: "icn_s" })} />);
    expect($('[data-testid="ev-person-hidden-U0PRIYA"]')?.textContent).toBe("A BOSS person · name hidden");
    expect($('[data-testid="ev-people-list"]')?.textContent).not.toContain("@northpeak.com");
  });
  it("Revoked is a filter of its own", async () => {
    api((url) => (url.endsWith("/people") ? json({ ...PEOPLE, people: [...PEOPLE.people,
      { external_user_id: "U0OLD", display: "old", state: "revoked", link_id: "chl_o", method: "code", revoked_at: iso(60_000) }] }) : undefined));
    await mount(<EventsHome projectId={PID} place={place("people", { conn: "icn_s" })} />);
    await click($('[data-testid="ev-people-filter-revoked"]'));
    expect($$('[data-testid^="ev-person-U"]').map((x) => x.getAttribute("data-testid"))).toEqual(["ev-person-U0OLD"]);
    expect($('[data-testid="ev-person-U0OLD"]')?.textContent).toContain("Revoked");
  });
  it("Turn off the attestation is for an identity-provider admin only; Revoke is pre-greyed for a viewer", async () => {
    denied.add("identity_provider:manage"); denied.add("integration:manage");
    api();
    await mount(<EventsHome projectId={PID} place={place("people", { conn: "icn_s" })} />);
    expect($('[data-testid="ev-att-off"]')).toBeNull();
    expect($('[data-testid="ev-revoke-U0PRIYA"]')?.getAttribute("aria-disabled")).toBe("true");
  });
});
