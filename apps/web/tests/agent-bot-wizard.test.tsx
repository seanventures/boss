// @vitest-environment happy-dom
/**
 * Agent or bot, from the roster: an agent is the existing create; a bot is that create,
 * then a deployment, then a trigger. A miss is the error, never a key.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AgentBotWizard } = await import("../app/agents/create-wizard");

type Call = { url: string; method: string; body: Record<string, unknown> | null };

const GRANTABLE = {
  skills: [{
    skill_id: "qa.run_tests", label: "Run tests", summary: "Runs the suite", source: "pack",
    behaviour_kind: "tool", risk_tier: 0, enabled: true,
  }],
  mcp_servers: [{
    mcp_server_id: "mcp_1", server: "GitHub", url: "http://mcp.example/github", enabled: true, risk_tier: 0,
  }],
  agents: [],
};

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 12; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

function install(reply: (url: string, method: string, body: Record<string, unknown> | null) => Response): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? "GET").toUpperCase();
    let body: Record<string, unknown> | null = null;
    if (typeof init.body === "string") {
      try { body = JSON.parse(init.body) as Record<string, unknown>; } catch { body = null; }
    }
    calls.push({ url, method, body });
    if (method === "GET" && url.includes("/v1/agents/grantable")) return Response.json(GRANTABLE);
    return reply(url, method, body);
  }));
  return calls;
}

async function open() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<AgentBotWizard projectId="p1" actor="ceo" onClose={() => {}} />);
  });
  await settle();
}

const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
async function click(sel: string) {
  const el = $(sel);
  expect(el, sel).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
}
async function type(sel: string, value: string) {
  const el = document.querySelector<HTMLInputElement | HTMLTextAreaElement>(sel);
  expect(el, sel).not.toBeNull();
  await act(async () => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el!.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}

const posts = (calls: Call[]) => calls.filter((c) => c.method === "POST");
const step = () => $('[data-testid="agent-bot-wizard"]')?.getAttribute("data-step");

describe("agent or bot wizard", () => {
  it("refuses an empty name", async () => {
    const calls = install(() => Response.json({}));
    await open();
    await click('[data-testid="abw-kind-agent"]');
    await click('[data-testid="abw-next"]');
    expect(step()).toBe("name");
    await type('[data-testid="abw-purpose"]', "Checks a release");
    await click('[data-testid="abw-next"]');
    expect(step()).toBe("name");
    expect($('[data-testid="abw-error"]')?.textContent).toMatch(/name/i);
    await type('[data-testid="abw-name"]', "   ");
    await click('[data-testid="abw-next"]');
    expect(step()).toBe("name");
    expect(posts(calls)).toEqual([]);
  });

  it("posts the existing create body for an agent and shows the key", async () => {
    const calls = install((url, method) => {
      if (method === "POST" && url === "/v1/agents") {
        return Response.json({ role_key: "release_auditor", agent_id: "agt_1" }, { status: 201 });
      }
      return Response.json({ detail: "unexpected" }, { status: 500 });
    });
    await open();
    await click('[data-testid="abw-kind-agent"]');
    await click('[data-testid="abw-next"]');
    await type('[data-testid="abw-name"]', "Release Auditor");
    await type('[data-testid="abw-purpose"]', "Checks a release");
    await click('[data-testid="abw-next"]');
    expect(step()).toBe("grants");
    await click('[data-testid="abw-skill-qa.run_tests"]');
    await click('[data-testid="abw-mcp-mcp_1"]');
    await click('[data-testid="abw-create"]');

    const wrote = posts(calls);
    expect(wrote.map((c) => c.url)).toEqual(["/v1/agents"]);
    expect(wrote[0].body).toEqual({
      project_id: "p1",
      actor: "ceo",
      name: "Release Auditor",
      purpose: "Checks a release",
      skills: ["qa.run_tests"],
      mcp_servers: [{ server_id: "mcp_1", tools: [] }],
      reports_to: null,
    });
    expect($('[data-testid="abw-created"]')?.textContent).toContain("release_auditor");
    expect($('[data-testid="abw-error"]')).toBeNull();
  });

  it("deploys a bot and then posts its trigger", async () => {
    const calls = install((url, method) => {
      if (method === "POST" && url === "/v1/agents") {
        return Response.json({ role_key: "night_watch", version: 1 }, { status: 201 });
      }
      if (method === "POST" && url === "/v1/agents/night_watch/deployments") {
        return Response.json({ id: "adep_1", environment: "dev", version: 1 }, { status: 201 });
      }
      if (method === "POST" && url === "/v1/agents/night_watch/triggers") {
        return Response.json({ id: "atrg_1", kind: "cron" }, { status: 201 });
      }
      return Response.json({ detail: `unexpected ${method} ${url}` }, { status: 500 });
    });
    await open();
    await click('[data-testid="abw-kind-bot"]');
    await click('[data-testid="abw-next"]');
    await type('[data-testid="abw-name"]', "Night Watch");
    await type('[data-testid="abw-purpose"]', "Watches pulls");
    await click('[data-testid="abw-next"]');
    await click('[data-testid="abw-skill-qa.run_tests"]');
    await click('[data-testid="abw-next"]');
    expect(step()).toBe("wake");
    await click('[data-testid="abw-wake-cron"]');
    await type('[data-testid="abw-cron"]', "15 * * * *");
    await click('[data-testid="abw-next"]');
    expect(step()).toBe("review");
    await click('[data-testid="abw-create"]');

    const wrote = posts(calls);
    expect(wrote.map((c) => c.url)).toEqual([
      "/v1/agents",
      "/v1/agents/night_watch/deployments",
      "/v1/agents/night_watch/triggers",
    ]);
    expect(wrote[0].body).toMatchObject({
      project_id: "p1",
      actor: "ceo",
      name: "Night Watch",
      purpose: "Watches pulls",
      skills: ["qa.run_tests"],
      mcp_servers: [],
      reports_to: null,
    });
    expect(wrote[1].body).toEqual({
      project_id: "p1",
      actor: "ceo",
      environment: "dev",
      version: 1,
    });
    expect(wrote[2].body).toMatchObject({
      project_id: "p1",
      actor: "ceo",
      environment: "dev",
      kind: "cron",
      config: { cron: "15 * * * *", timezone: "UTC" },
    });
    expect($('[data-testid="abw-created"]')?.textContent).toContain("night_watch");
  });

  it("shows a refused trigger and does not say the bot was created", async () => {
    const calls = install((url, method) => {
      if (method === "POST" && url === "/v1/agents") {
        return Response.json({ role_key: "night_watch", version: 1 }, { status: 201 });
      }
      if (method === "POST" && url === "/v1/agents/night_watch/deployments") {
        return Response.json({ id: "adep_1" }, { status: 201 });
      }
      if (method === "POST" && url === "/v1/agents/night_watch/triggers") {
        return Response.json({ detail: "night_watch is not deployed to dev." }, { status: 404 });
      }
      return Response.json({ detail: "unexpected" }, { status: 500 });
    });
    await open();
    await click('[data-testid="abw-kind-bot"]');
    await click('[data-testid="abw-next"]');
    await type('[data-testid="abw-name"]', "Night Watch");
    await click('[data-testid="abw-next"]');
    await click('[data-testid="abw-next"]');
    await click('[data-testid="abw-wake-cron"]');
    await type('[data-testid="abw-cron"]', "15 * * * *");
    await click('[data-testid="abw-next"]');
    await click('[data-testid="abw-create"]');

    expect(posts(calls).map((c) => c.url)).toEqual([
      "/v1/agents",
      "/v1/agents/night_watch/deployments",
      "/v1/agents/night_watch/triggers",
    ]);
    expect($('[data-testid="abw-error"]')?.textContent).toContain("night_watch is not deployed to dev.");
    expect($('[data-testid="abw-created"]')).toBeNull();
    expect($('[data-testid="agent-bot-wizard"]')?.textContent).not.toMatch(/\bexists\b/i);
  });
});
