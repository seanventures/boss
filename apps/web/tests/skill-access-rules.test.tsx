// @vitest-environment happy-dom
/**
 * SKILLS & MCP › RULES › SKILL ACCESS (BOSSAPP-890): the rules in the order the API reads them,
 * disable/revoke send one write each, Add rule sends the chosen actions, and the simulator shows
 * the engine's explanation.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SkillAccessRules } from "../app/skills/skill-access-rules";
import { parseSkillRules, ruleWords } from "../lib/skill-policies";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const RULES = { rules: [
  { id: "skp_a", scope: "project", position: 1, effect: "allow", actions: ["import"], skill_pattern: "*",
    role_pattern: "*", actor_kind: "any", source_pattern: "github:https://github.com/acme/*", min_risk_tier: null,
    status: "active", hit_count: 3 },
  { id: "skp_b", scope: "org", position: 2, effect: "deny", actions: ["import"], skill_pattern: "*",
    role_pattern: "*", actor_kind: "any", source_pattern: "*", min_risk_tier: null, status: "active", hit_count: 1,
    note: "only our GitHub org" },
] };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("skill access rules", () => {
  it("parses and words a rule", () => {
    const [a, b] = parseSkillRules(RULES);
    expect(ruleWords(a)).toBe("from github:https://github.com/acme/*");
    expect(ruleWords(b)).toBe("anyone");
    expect(ruleWords({ ...b, actor_kind: "agent", role_pattern: "forge", min_risk_tier: 2 })).toBe("agents · role forge · tier 2+");
    expect(parseSkillRules({ rules: [{ effect: "odd", actor_kind: "x" }] })[0]).toMatchObject({ effect: "deny", actor_kind: "any", scope: "project" });
  });

  it("lists in order, disables, revokes, adds and simulates", async () => {
    const calls: { url: string; method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = String(init?.method ?? "GET");
      calls.push({ url: String(url), method, body: init?.body ? JSON.parse(String(init.body)) : {} });
      if (method === "GET") return json(RULES);
      if (String(url).includes("/simulate")) return json({ decision: "deny", rule_id: "skp_b", explanation: "DENY: rule skp_b matched first." });
      return json({ rule: {} });
    }));
    await mount(<SkillAccessRules projectId="p1" />);
    await settle();
    expect($$('[data-testid^="skill-rule-skp_"]').map((r) => r.getAttribute("data-testid"))).toEqual(["skill-rule-skp_a", "skill-rule-skp_b"]);
    ($('[data-testid="skill-rule-toggle-skp_b"]') as HTMLButtonElement).click();
    await settle();
    const patch = calls.find((c) => c.method === "PATCH");
    expect(patch?.url).toContain("/v1/skill-policies/skp_b");
    expect(patch?.body).toEqual({ project_id: "p1", status: "disabled" });
    ($('[data-testid="skill-rule-revoke-skp_a"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.method === "DELETE")?.url).toContain("/v1/skill-policies/skp_a?project_id=p1");

    ($('[data-testid="skill-access-add"]') as HTMLButtonElement).click();
    await settle();
    ($('[data-testid="skill-access-action-hold"]') as HTMLInputElement).click();
    await settle();
    ($('[data-testid="skill-access-save"]') as HTMLButtonElement).click();
    await settle();
    const post = calls.find((c) => c.method === "POST" && c.url === "/v1/skill-policies");
    expect(post?.body).toMatchObject({ project_id: "p1", effect: "deny", scope: "project", actions: ["import", "hold"],
                                       skill_pattern: "*", actor_kind: "any", min_risk_tier: null });

    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    const box = $('[aria-label="Simulate skill key"]') as HTMLInputElement;
    setter.call(box, "house_tone");
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await settle();
    ($('[data-testid="skill-access-simulate-run"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url.includes("/simulate"))?.body).toMatchObject({ project_id: "p1", action: "import", skill_key: "house_tone" });
    expect($('[data-testid="skill-access-simulate-out"]')?.textContent).toContain("skp_b matched first");
  });
});
