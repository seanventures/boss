// @vitest-environment happy-dom
/**
 * One action sets the model connection and the default model.
 * A failed row is listed. Nothing here renames an agent or changes what they may do.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { cleanup, render } from "./_helpers/kit-render";

const { RosterBulk } = await import("../app/agents/roster-bulk");

afterEach(() => cleanup());

const AGENTS = [
  { roleKey: "ada", name: "Ada", connectionId: null, connectionLabel: "Local credentials", defaultModel: null, lifecycle: "active" },
  { roleKey: "bea", name: "Bea", connectionId: "setup-anthropic", connectionLabel: "Anthropic", defaultModel: "claude-sonnet-5", lifecycle: "active" },
  { roleKey: "cy", name: "Cy", connectionId: null, connectionLabel: "Local credentials", defaultModel: null, lifecycle: "retired" },
];

const CONNECTIONS = [
  { id: "setup-anthropic", label: "Anthropic", models: ["claude-sonnet-5", "claude-haiku-5"] },
  { id: "setup-openai", label: "OpenAI", models: ["gpt-5.4"] },
];

function tick(roleKey: string) {
  const box = document.querySelector<HTMLInputElement>(`[data-testid="rb-agent-${roleKey}"]`);
  if (!box) throw new Error(`no checkbox for ${roleKey}`);
  box.click();
}

function pick(testId: string, value: string) {
  const el = document.querySelector<HTMLSelectElement>(`[data-testid="${testId}"]`);
  if (!el) throw new Error(`no ${testId}`);
  el.value = value;
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("roster bulk model change", () => {
  it("sets the connection and the default model for the ticked agents in one action", async () => {
    const onApply = vi.fn(async () => ({
      receiptId: "rcpt_one", dedupeKey: "once", replayed: false, applied: [], failed: [],
    }));
    await render(<RosterBulk agents={AGENTS} connections={CONNECTIONS} onApply={onApply} />);
    tick("ada");
    tick("bea");
    document.querySelector<HTMLInputElement>('[data-testid="rb-change-connection"]')!.click();
    pick("rb-connection", "setup-openai");
    document.querySelector<HTMLInputElement>('[data-testid="rb-change-model"]')!.click();
    pick("rb-model", "gpt-5.4");
    document.querySelector<HTMLButtonElement>('[data-testid="rb-apply"]')!.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(onApply).toHaveBeenCalledTimes(1);
    const body = onApply.mock.calls[0][0];
    expect(body.connectionId).toBe("setup-openai");
    expect(body.defaultModel).toBe("gpt-5.4");
    expect(body.agents.map((agent: { roleKey: string }) => agent.roleKey)).toEqual(["ada", "bea"]);
    expect(body.agents[1].expected).toEqual({
      connectionId: "setup-anthropic", defaultModel: "claude-sonnet-5",
    });
    expect(typeof body.dedupeKey).toBe("string");
    expect(body.dedupeKey.length).toBeGreaterThan(8);
    const intro = document.querySelector('[data-testid="rb-intro"]');
    expect(intro?.tagName).toBe("P");
    expect(intro!.textContent!.trim().split(/\s+/).length).toBeGreaterThan(6);
    for (const el of document.querySelectorAll("[title]")) {
      const words = (el.getAttribute("title") || "").trim().split(/\s+/).filter(Boolean);
      expect(words.length).toBeLessThanOrEqual(14);
    }
  });

  it("lists a failed row and retries only that row", async () => {
    const onApply = vi.fn()
      .mockResolvedValueOnce({
        receiptId: "rcpt_part",
        dedupeKey: "part",
        replayed: false,
        applied: [{ roleKey: "ada", connectionId: "setup-openai", defaultModel: "gpt-5.4" }],
        failed: [{ roleKey: "bea", error: "This agent's connection or model changed." }],
      })
      .mockResolvedValueOnce({
        receiptId: "rcpt_retry", dedupeKey: "retry", replayed: false,
        applied: [{ roleKey: "bea", connectionId: "setup-openai", defaultModel: "gpt-5.4" }],
        failed: [],
      });
    await render(<RosterBulk agents={AGENTS} connections={CONNECTIONS} onApply={onApply} />);
    tick("ada");
    tick("bea");
    document.querySelector<HTMLInputElement>('[data-testid="rb-change-connection"]')!.click();
    pick("rb-connection", "setup-openai");
    document.querySelector<HTMLInputElement>('[data-testid="rb-change-model"]')!.click();
    pick("rb-model", "gpt-5.4");
    document.querySelector<HTMLButtonElement>('[data-testid="rb-apply"]')!.click();
    await new Promise((r) => setTimeout(r, 20));
    const failure = document.querySelector('[data-testid="rb-failure-bea"]');
    expect(failure?.textContent).toContain("This agent's connection or model changed.");
    expect(document.querySelector('[data-testid="rb-row-ada"]')?.textContent).toContain("gpt-5.4");
    expect(document.querySelector('[data-testid="rb-row-bea"]')?.textContent).toContain("claude-sonnet-5");
    document.querySelector<HTMLButtonElement>('[data-testid="rb-retry"]')!.click();
    await new Promise((r) => setTimeout(r, 20));
    expect(onApply).toHaveBeenCalledTimes(2);
    const retry = onApply.mock.calls[1][0];
    expect(retry.agents.map((agent: { roleKey: string }) => agent.roleKey)).toEqual(["bea"]);
    expect(retry.dedupeKey).not.toBe(onApply.mock.calls[0][0].dedupeKey);
    expect(retry.connectionId).toBe("setup-openai");
    expect(retry.defaultModel).toBe("gpt-5.4");
  });

  it("does not offer a control for who the agent is or what they may do", async () => {
    await render(<RosterBulk agents={AGENTS} connections={CONNECTIONS} onApply={vi.fn()} />);
    const retired = document.querySelector<HTMLInputElement>('[data-testid="rb-agent-cy"]');
    expect(retired?.disabled).toBe(true);
    const text = document.body.innerHTML.toLowerCase();
    expect(text).not.toMatch(/autonomy|reports-to|reports to|authority/);
    expect(document.querySelector('input[name="display_name"]')).toBeNull();
    expect(document.querySelector('input[name="skills"]')).toBeNull();
    tick("bea");
    document.querySelector<HTMLInputElement>('[data-testid="rb-change-connection"]')!.click();
    pick("rb-connection", "setup-openai");
    const blocked = document.querySelector('[data-testid="rb-blocked"]');
    expect(blocked?.tagName).toBe("P");
    expect(blocked?.textContent).toContain("Bea");
    expect(blocked?.textContent).toContain("claude-sonnet-5");
    expect(document.querySelector<HTMLButtonElement>('[data-testid="rb-apply"]')!.disabled).toBe(true);
  });
});
