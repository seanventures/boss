// @vitest-environment happy-dom
/**
 * THE REQUEST CONTRACT (activity-govern spec §8.2). The redesigned Govern sends exactly the
 * requests the old Govern components sent — same method, same API path, same body — plus the
 * two tightenings the operator decided (§10 Q3/Q4: the signed-in actor on Pace, FinOps and
 * token-guide writes), the version it showed as `If-Match`, and `via: govern` on the monthly cap
 * (spec §4.4.3). Governance semantics do not change because the screen did.
 *
 * Two layers, both pinned: what the screen posts to `/api/govern`, and what that route sends
 * to the API. The goldens below were recorded from the old components on base d29954dd:
 *   SettingsKnobs → /api/settings → PUT /v1/settings/{key}  {key, project_id, actor, value}
 *   setBudgetIntensityAction → POST /v1/budget/set_intensity  {scope_id, intensity, actor: "executive_console"}
 *   setStageTokenCapAction → PUT /v1/token-efficiency/policies  {project_id, policies: [{stage, max_total_tokens_per_call}]}
 *   Setup's Budget row → /api/setup → PUT /v1/setup/budget  {project_id, monthly_cap_usd}
 *   reconcileCommitmentsAction → POST /v1/finops/reconcile-commitments  {project_id, actor: "system"}
 *   requestInitiativeBudgetAction → POST /v1/finops/initiative-budget  {project_id, title, tier, amount_usd_micros, actor: "system"}
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { fakeGovernApi, governView, newGovern, type FakeGovern } from "./_helpers/govern-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  PROJECT_ID: "p1",
  authHeaders: (extra?: Record<string, string>) => ({ Authorization: "Bearer k", ...(extra ?? {}) }),
  resolveProjectId: (p?: string) => p || "p1",
}));

const { GovernHome } = await import("../app/govern/govern-home");
const { POST } = await import("../app/api/govern/route");

const OLD = {
  knob: { method: "PUT", path: "/v1/settings/code_agent_max_turns",
          body: { key: "code_agent_max_turns", project_id: "p1", actor: "ceo", value: "37" } },
  knobReset: { method: "PUT", path: "/v1/settings/code_review_inrun_max_rounds",
               body: { key: "code_review_inrun_max_rounds", project_id: "p1", actor: "ceo", value: null } },
  intensity: { method: "POST", path: "/v1/budget/set_intensity",
               body: { scope_id: "p1", intensity: 50, actor: "executive_console" } },
  token: { method: "PUT", path: "/v1/token-efficiency/policies",
           body: { project_id: "p1", policies: [{ stage: "implement", max_total_tokens_per_call: 48000 }] } },
  monthly: { method: "PUT", path: "/v1/setup/budget", body: { project_id: "p1", monthly_cap_usd: 150 } },
  reconcile: { method: "POST", path: "/v1/finops/reconcile-commitments", body: { project_id: "p1", actor: "system" } },
  initiative: { method: "POST", path: "/v1/finops/initiative-budget",
                body: { project_id: "p1", title: "New GPU cluster", tier: 2, amount_usd_micros: 60_000_000, actor: "system" } },
};
/** The ONLY differences from the old requests, each decided or specified. */
const TIGHTENED = {
  intensity: { ...OLD.intensity.body, actor: "ceo" },                      // §10 Q4
  token: { ...OLD.token.body, actor: "ceo" },                              // §10 Q3
  monthly: { ...OLD.monthly.body, via: "govern" },                         // §4.4.3
  reconcile: { ...OLD.reconcile.body, actor: "ceo" },                      // §10 Q4
  initiative: { ...OLD.initiative.body, actor: "ceo" },                    // §10 Q4
};

let g: FakeGovern;
beforeEach(() => {
  g = newGovern();
  vi.stubGlobal("fetch", fakeGovernApi(g));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget");
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const posts = (op: string) => g.calls.filter((c) => c.method === "POST" && c.op === op);

async function setValue(el: Element | null, value: string) {
  await act(async () => {
    const input = el as HTMLInputElement;
    const proto = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
  await settle();
}

async function routeSends(op: string, body: Record<string, unknown>, ifMatch?: string) {
  const sent: { url: string; method: string; body: unknown; ifMatch: string | null }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    sent.push({ url: String(input), method: String(init?.method), body: JSON.parse(String(init?.body ?? "null")),
                ifMatch: new Headers(init?.headers as HeadersInit).get("If-Match") });
    return Response.json({ ok: true });
  }));
  await POST(new Request("http://console.test/api/govern", {
    method: "POST", headers: { "Content-Type": "application/json", ...(ifMatch ? { "If-Match": ifMatch } : {}) },
    body: JSON.stringify({ op, ...body }),
  }));
  return sent[0];
}

describe("the screen posts the old requests (plus the decided tightenings)", () => {
  it("a knob: the same body SettingsKnobs sent, with the version it showed", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-code_agent_max_turns]"));
    await click($("[data-testid=gv-edit-code_agent_max_turns]"));
    await setValue($("[data-testid=gv-input]"), "37");
    await click($("[data-testid=gv-save]"));
    const [post] = posts("setting");
    expect(post.body).toEqual({ op: "setting", ...OLD.knob.body });
    expect(post.ifMatch).toBe("unset");
    const api = await routeSends("setting", OLD.knob.body, "unset");
    expect([api.method, api.url, api.body, api.ifMatch]).toEqual(
      ["PUT", `https://boss-api.test${OLD.knob.path}`, OLD.knob.body, "unset"]);
  });

  it("Use default: the old reset (value null)", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-reset-code_review_inrun_max_rounds]"));
    expect(posts("setting")[0].body).toEqual({ op: "setting", ...OLD.knobReset.body });
    const api = await routeSends("setting", OLD.knobReset.body);
    expect(api.body).toEqual(OLD.knobReset.body);
    expect(api.url).toBe(`https://boss-api.test${OLD.knobReset.path}`);
  });

  it("Pace: the old intensity write, as the signed-in actor", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-pace]"));
    await click($("[data-testid=gv-edit-pace]"));
    await click($("[data-testid=gv-pace-apply]"));
    expect(posts("pace")[0].body).toEqual({ op: "pace", project_id: "p1", actor: "ceo", intensity: 50 });
    expect(posts("pace")[0].ifMatch).toBe("7");
    const api = await routeSends("pace", { project_id: "p1", actor: "ceo", intensity: 50 }, "7");
    expect([api.method, api.url, api.body, api.ifMatch]).toEqual(
      ["POST", `https://boss-api.test${OLD.intensity.path}`, TIGHTENED.intensity, "7"]);
  });

  it("the monthly cap: Setup's write, saying it came from Govern", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-more-monthly_cap]"));
    await click($("[data-testid=gv-edit-monthly_cap]"));
    await setValue($("[data-testid=gv-input]"), "150");
    await click($("[data-testid=gv-save]"));
    const api = await routeSends("monthly_cap", posts("monthly_cap")[0].body!);
    expect([api.method, api.url, api.body]).toEqual(["PUT", `https://boss-api.test${OLD.monthly.path}`, TIGHTENED.monthly]);
  });

  it("a token guide: the old cap form's body, as the signed-in actor", async () => {
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-token-edit]"));
    await setValue($("[data-testid=gv-token-stage]"), "implement");
    await setValue($("[data-testid=gv-token-cap]"), "48000");
    await click($("[data-testid=gv-token-save]"));
    const [post] = posts("token_cap");
    expect(post.ifMatch).toBe("unset");
    const api = await routeSends("token_cap", post.body!);
    expect([api.method, api.url, api.body]).toEqual(["PUT", `https://boss-api.test${OLD.token.path}`, TIGHTENED.token]);
  });

  it("FinOps: the old posts, as the signed-in actor instead of `system`", async () => {
    g.envelopes = { allocations: [], finops: { state: "no_events", summary: { anomalies_open_count: 0 } },
                    reconciliations: [], initiative_rule: { tier_min: 2, amount_usd_micros: 50_000_000 } };
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    await click($("[data-testid=gv-tab-envelopes]"));
    await click($("[data-testid=gv-reconcile]"));
    await setValue($("[data-testid=gv-init-title]"), "New GPU cluster");
    await setValue($("[data-testid=gv-init-amount]"), "60");
    await act(async () => { ($("[data-testid=gv-initiative-form]") as HTMLFormElement).requestSubmit(); });
    await settle();
    const reconcile = await routeSends("reconcile", posts("reconcile")[0].body!);
    expect([reconcile.url, reconcile.body]).toEqual([`https://boss-api.test${OLD.reconcile.path}`, TIGHTENED.reconcile]);
    const initiative = await routeSends("initiative", posts("initiative")[0].body!);
    expect([initiative.url, initiative.body]).toEqual([`https://boss-api.test${OLD.initiative.path}`, TIGHTENED.initiative]);
  });

  it("opening Govern is one read and no writes", async () => {
    g.view = governView();
    await mount(<GovernHome projectId="p1" actor="ceo" />);
    expect(g.calls).toHaveLength(1);
    expect(g.calls[0]).toMatchObject({ method: "GET", url: "/api/govern?project_id=p1&view=limits" });
  });
});
