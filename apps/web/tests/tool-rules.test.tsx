// @vitest-environment happy-dom
/**
 * SKILLS & MCP › RULES AND "ALWAYS ALLOW THIS" (BOSSAPP-815/817): the rule table lists what the
 * API answers in the engine's order, enable/disable/revoke send the right writes, the simulate
 * box shows the engine's explanation, and an Ask card about a tier 0-1 tool call approves with
 * `always_allow` (never pinning a redacted value).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AlwaysAllowOption, alwaysAllowOffer, pinnable } from "../app/always-allow-ui";
import { ToolRules } from "../app/skills/tool-rules";
import { conditionWords, ordered, parseRules, parseSuggestions, rateWords, readConditions, scopeWords } from "../lib/tool-policies";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const RULES = { policies: [
  { id: "tpol_b", scope: "project", priority: 100, effect: "allow", selector: { server: "*", tool: "read_*" },
    conditions: [], source: "operator", status: "active", hit_count: 2 },
  { id: "tpol_a", scope: "agent:forge", priority: 10, effect: "deny", selector: { server: "docs", tool: "delete_*" },
    conditions: [{ path: "$.path", op: "prefix", value: "/prod" }], source: "operator", status: "active", hit_count: 0 },
  { id: "tpol_c", scope: "agent:forge", priority: 50, effect: "allow", selector: { server: "mcp_1", tool: "note" },
    conditions: [], source: "decision", source_decision_id: "dec_9", status: "active", hit_count: 5 },
] };

afterEach(() => { unmount(); vi.restoreAllMocks(); });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("the rule contract", () => {
  it("parses, orders and says rules in words", () => {
    const rules = parseRules(RULES);
    expect(ordered(rules).map((r) => r.id)).toEqual(["tpol_a", "tpol_c", "tpol_b"]);
    expect(scopeWords("agent:forge")).toBe("Agent forge");
    expect(scopeWords("org")).toBe("Org");
    expect(conditionWords(rules[1].conditions)).toBe('$.path prefix "/prod"');
    expect(readConditions('$.path prefix /srv\n$.n equals 3')).toEqual({ error: null, conditions: [
      { path: "$.path", op: "prefix", value: "/srv" }, { path: "$.n", op: "equals", value: 3 }] });
    expect(readConditions("nonsense").error).toMatch(/Line 1/);
  });

  it("reads the always-allow offer only from an eligible tool-call card", () => {
    const rec = { action: "gateway_tool_call", tool: "note", server: "docs", role_key: "forge",
                  args_preview: { path: "/a", token: "[redacted]", opts: { x: 1 } },
                  always_allow_offer: { eligible: true, expiry_choices: ["1h", "7d", "never"] } };
    const offer = alwaysAllowOffer(rec)!;
    expect(offer.tool).toBe("note");
    expect(pinnable(offer.preview)).toEqual(["path"]);
    expect(alwaysAllowOffer({ ...rec, always_allow_offer: { eligible: false } })).toBeNull();
    expect(alwaysAllowOffer({ ...rec, action: "gateway_ask_person" })).toBeNull();
  });
});

describe("the Rules tab", () => {
  it("lists rules in order, filters Always allow, toggles, revokes and simulates", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
      if (method === "GET") return json(RULES);
      if (String(url).includes("/simulate")) {
        return json({ decision: "deny", matched_policy_ids: ["tpol_a"], explanation: "DENY: rule tpol_a matched first.",
                      matched_rules: [] });
      }
      return json({ policy: {} });
    }));
    await mount(<ToolRules projectId="p1" actor="ceo" />);
    await settle();
    const rows = $$('[data-testid^="tool-rule-tpol_"]');
    expect(rows.map((r) => r.getAttribute("data-testid"))).toEqual(["tool-rule-tpol_a", "tool-rule-tpol_c", "tool-rule-tpol_b"]);
    expect($('[data-testid="tool-rule-tpol_a"]')?.textContent).toContain('$.path prefix "/prod"');
    ($('[data-testid="tool-rules-filter-decision"]') as HTMLButtonElement).click();
    await settle();
    expect($$('[data-testid^="tool-rule-tpol_"]').map((r) => r.getAttribute("data-testid"))).toEqual(["tool-rule-tpol_c"]);
    ($('[data-testid="tool-rule-toggle-tpol_c"]') as HTMLButtonElement).click();
    await settle();
    const patch = calls.find((c) => c.method === "PATCH");
    expect(patch?.url).toContain("/v1/tool-policies/tpol_c");
    expect(patch?.body).toEqual({ project_id: "p1", status: "disabled" });
    ($('[data-testid="tool-rule-revoke-tpol_c"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.method === "DELETE")?.url).toContain("/v1/tool-policies/tpol_c?project_id=p1");
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    for (const [label, value] of [["Simulate server", "docs"], ["Simulate tool", "delete_x"]]) {
      const box = $(`[aria-label="${label}"]`) as HTMLInputElement;
      setter.call(box, value);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await settle();
    ($('[data-testid="tool-rules-simulate-run"]') as HTMLButtonElement).click();
    await settle();
    const sim = calls.find((c) => c.url.includes("/simulate"));
    expect(sim?.body).toMatchObject({ project_id: "p1", server: "docs", tool: "delete_x", arguments: {} });
    expect($('[data-testid="tool-rules-simulate-out"]')?.textContent).toContain("tpol_a matched first");
  });
});

describe("Always allow on the card", () => {
  it("approves with always_allow, the ticked arguments and the chosen expiry", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
      return json({ id: "dec_1", status: "approved" });
    }));
    const offer = alwaysAllowOffer({ action: "gateway_tool_call", tool: "note", server: "docs", role_key: "forge",
      args_preview: { path: "/a", token: "[redacted]" },
      always_allow_offer: { eligible: true, expiry_choices: ["1h", "7d", "never"] } })!;
    await mount(<AlwaysAllowOption decisionId="dec_1" offer={offer} projectId="p1" actorId="ceo" />);
    ($('[data-testid="always-allow-toggle"]') as HTMLButtonElement).click();
    await settle();
    expect($('[data-testid="always-allow-pin-token"]')).toBeNull();
    ($('[data-testid="always-allow-pin-path"]') as HTMLInputElement).click();
    await settle();
    ($('[data-testid="always-allow-approve"]') as HTMLButtonElement).click();
    await settle();
    const sent = calls.find((c) => c.url.includes("/v1/decisions/dec_1/approve"));
    expect(sent?.body).toEqual({ actor: "ceo", always_allow: { expiry: "7d", match_args: ["path"] } });
    expect($('[data-testid="always-allow-done"]')?.textContent).toContain("note now runs without asking for forge");
  });
});

describe("rate limits: buckets and suggested limits (BOSSAPP-868)", () => {
  it("reads a bucket, says it in words, and defaults to the project", () => {
    const [run, plain] = parseRules({ policies: [
      { id: "r1", effect: "rate_limit", rate_limit: { max_calls: 30, window_seconds: 3600, bucket: "run" } },
      { id: "r2", effect: "rate_limit", rate_limit: { max_calls: 5, window_seconds: 60 } },
    ] });
    expect(run.rate_limit).toEqual({ max_calls: 30, window_seconds: 3600, bucket: "run" });
    expect(rateWords(run.rate_limit!)).toBe("30/3600s per run");
    expect(rateWords(plain.rate_limit!)).toBe("5/60s");
  });

  it("offers the catalogue's suggestions and adds one as offered", async () => {
    const rule = { scope: "project", priority: 100, effect: "rate_limit", conditions: [],
                   selector: { server: "github", tool: "create_*" },
                   rate_limit: { max_calls: 30, window_seconds: 3600, bucket: "run" } };
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
      if (String(url).includes("/suggestions")) {
        return json({ suggestions: [{ server: "github", catalogue: "github", why: "loops", rule }] });
      }
      if (method === "GET") return json({ policies: [] });
      return json({ policy: {} }, 201);
    }));
    expect(parseSuggestions({ suggestions: [{ server: "github", rule }] })[0].summary.rate_limit?.bucket).toBe("run");
    await mount(<ToolRules projectId="p1" actor="ceo" />);
    await settle();
    expect($('[data-testid="tool-rules-suggestion-0"]')?.textContent).toContain("30/3600s per run");
    ($('[data-testid="tool-rules-suggestion-add-0"]') as HTMLButtonElement).click();
    await settle();
    const post = calls.find((c) => c.method === "POST");
    expect(post?.url).toContain("/v1/tool-policies");
    expect(post?.body).toEqual({ project_id: "p1", ...rule });
  });
});

describe("who a rule applies to (BOSSAPP-1041)", () => {
  function set(el: HTMLInputElement | HTMLSelectElement, value: string) {
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  }

  it("picks a project, an agent or a workflow and refuses an agent with no target", async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      const path = String(url);
      if (method === "POST") {
        posts.push(JSON.parse(String(init?.body ?? "{}")));
        return json({ policy: {} }, 201);
      }
      if (path.includes("/v1/roles")) return json({ items: [{ role_key: "backend_engineer", display_name: "Backend" }] });
      if (path.includes("/v1/workflows")) return json({ items: [{ id: "wf_nightly", name: "Nightly" }] });
      if (path.includes("/suggestions")) return json({ suggestions: [] });
      return json({ policies: [] });
    }));
    await mount(<ToolRules projectId="p1" actor="ceo" />);
    await settle();
    ($('[data-testid="tool-rules-add"]') as HTMLButtonElement).click();
    await settle();
    const applies = $('[aria-label="Scope"]');
    expect(applies?.tagName, "Applies to is a picker, like the rate-limit bucket").toBe("SELECT");
    set(applies as HTMLSelectElement, "agent");
    await settle();
    expect(($('[data-testid="tool-rules-save"]') as HTMLButtonElement).disabled).toBe(true);
    ($('[data-testid="tool-rules-save"]') as HTMLButtonElement).click();
    await settle();
    expect(posts).toHaveLength(0);
    const agent = $('[aria-label="Agent"]') as HTMLSelectElement;
    expect(agent?.tagName).toBe("SELECT");
    set(agent, "backend_engineer");
    await settle();
    ($('[data-testid="tool-rules-save"]') as HTMLButtonElement).click();
    await settle();
    expect(posts.at(-1)?.scope).toBe("agent:backend_engineer");
    ($('[data-testid="tool-rules-add"]') as HTMLButtonElement).click();
    await settle();
    set($('[aria-label="Scope"]') as HTMLSelectElement, "workflow");
    await settle();
    set($('[aria-label="Workflow"]') as HTMLSelectElement, "wf_nightly");
    await settle();
    ($('[data-testid="tool-rules-save"]') as HTMLButtonElement).click();
    await settle();
    expect(posts.at(-1)?.scope).toBe("workflow:wf_nightly");
    ($('[data-testid="tool-rules-add"]') as HTMLButtonElement).click();
    await settle();
    set($('[aria-label="Scope"]') as HTMLSelectElement, "project");
    await settle();
    ($('[data-testid="tool-rules-save"]') as HTMLButtonElement).click();
    await settle();
    expect(posts.at(-1)?.scope).toBe("project");
  });
});
