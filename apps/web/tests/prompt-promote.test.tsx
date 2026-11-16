// @vitest-environment happy-dom
/**
 * BOSSAPP-1263: saving a prompt does not create an agent. Promote is refused
 * until a decision is approved, and the screen never calls POST /v1/agents.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, type } from "./_helpers/kit-render";

const { PromptPromote } = await import("../app/prompts/promote");

type Call = { url: string; method: string; body: Record<string, unknown> | null };

const calls: Call[] = [];
let approved = false;

function json(status: number, data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  calls.length = 0;
  approved = false;
  vi.unstubAllGlobals();
});

function installFetch() {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : null;
    calls.push({ url, method, body });
    if (method === "GET") return json(200, { prompts: [] });
    if (url.endsWith("/decision")) {
      return json(202, {
        prompt_id: "prompt_1", decision_id: "dec_1", decision_status: "pending",
        agent_created: false,
      });
    }
    if (url.endsWith("/promote")) {
      if (!approved) {
        return json(409, {
          detail: "This decision is not approved, so the prompt stays a prompt.",
        });
      }
      return json(201, {
        agent_created: true, already: false, role_key: "release_notes",
        agent: { name: "Release notes", role_key: "release_notes" },
      });
    }
    return json(201, {
      id: "prompt_1", project_id: body?.project_id, title: body?.title, body: body?.body,
      decision_id: null, decision_status: null, role_key: null, agent_created: false,
    });
  }));
}

function longSentencesOutsideParagraphs(): string[] {
  const bad: string[] = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    const parent = node.parentElement;
    if (parent && !parent.closest("style, script")) {
      for (const sentence of (node.textContent || "").split(/(?<=[.!?])\s+/)) {
        const words = sentence.trim().split(/\s+/).filter(Boolean);
        if (words.length > 6 && !parent.closest("p")) bad.push(sentence.trim());
      }
    }
    node = walker.nextNode();
  }
  return bad;
}

async function saveOne() {
  installFetch();
  await render(<PromptPromote projectId="p1" actor="ceo" />);
  await type("#prompt-title", "Release notes");
  await type("#prompt-body", "Compare the notes to the diff.");
  await click("[data-testid=save-prompt]");
}

describe("prompt promote", () => {
  it("saves a prompt and does not create an agent", async () => {
    await saveOne();
    const posts = calls.filter((call) => call.method === "POST");
    expect(posts.map((call) => call.url)).toEqual(["/v1/prompts"]);
    expect(posts[0].body).toMatchObject({
      project_id: "p1", actor: "ceo", title: "Release notes",
      body: "Compare the notes to the diff.",
    });
    expect(calls.some((call) => call.url.includes("/v1/agents"))).toBe(false);
    expect($("[data-testid=agent-born]")).toBeNull();
    expect(document.body.textContent).toContain("Saving does not create an agent.");
    expect($("[data-testid=prompt-card]")?.textContent).toContain("Release notes");
    expect(longSentencesOutsideParagraphs()).toEqual([]);
  });

  it("refuses to promote until the decision is approved", async () => {
    await saveOne();
    await click("[data-testid=ask-decision]");
    await click("[data-testid=promote-prompt]");
    const error = $("[data-testid=promote-error]");
    expect(error?.tagName).toBe("P");
    expect(error?.textContent).toContain("not approved");
    expect($("[data-testid=agent-born]")).toBeNull();
    expect(calls.some((call) => call.url.includes("/v1/agents"))).toBe(false);
    expect(calls.some((call) => call.url === "/v1/prompts/prompt_1/promote")).toBe(true);
    const titles = $$("[title]").map((element) => element.getAttribute("title") || "");
    expect(titles.length).toBeGreaterThan(0);
    for (const title of titles) expect(title.split(/\s+/).filter(Boolean).length).toBeLessThanOrEqual(14);
    expect(longSentencesOutsideParagraphs()).toEqual([]);
  });

  it("promotes only after the decision is approved, still not via POST /v1/agents", async () => {
    await saveOne();
    await click("[data-testid=ask-decision]");
    approved = true;
    await click("[data-testid=promote-prompt]");
    expect($("[data-testid=agent-born]")?.textContent).toContain("Release notes");
    expect($("[data-testid=promote-error]")).toBeNull();
    expect(calls.some((call) => call.url.includes("/v1/agents"))).toBe(false);
    expect(calls.filter((call) => call.url.endsWith("/promote"))).toHaveLength(1);
    expect(longSentencesOutsideParagraphs()).toEqual([]);
  });
});
