// @vitest-environment happy-dom
/**
 * Setup › Try it starts a build that acts for the person, so the API may answer 409
 * consent_required. It ended on a bare "Not started". A setup write now opens the consent sheet
 * and, once granted, sends the same write again; the proxy keeps the API's body under `consent`.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { setupWrite } from "../lib/setup-client";
import { CONSENT_EVENT } from "../lib/identity/consent";

const CONSENT = { error: "consent_required", task: "First PR", task_run_id: "pending:x",
  agents: [{ id: "agt_1", name: "Helm", role_key: "builder" }], permissions: ["code_task:create"],
  projects: ["boss-dogfood"], github: null,
  durations: [{ kind: "task", label: "This build", days: null }], max_days: 30 };

afterEach(() => { vi.unstubAllGlobals(); });

describe("a setup write that needs consent", () => {
  it("asks, then sends the same write again", async () => {
    const bodies: string[] = [];
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async (_u: string, init: RequestInit) => {
      bodies.push(String(init.body));
      n += 1;
      return n === 1
        ? new Response(JSON.stringify({ error: "Needs consent", consent: CONSENT }), { status: 409 })
        : new Response(JSON.stringify({ ok: true }), { status: 200 });
    }));
    const asked: unknown[] = [];
    const onAsk = (e: Event) => {
      const d = (e as CustomEvent<{ task: string; onDecided: (g: string | null) => void }>).detail;
      asked.push(d.task);
      d.onDecided("grant_1");
    };
    window.addEventListener(CONSENT_EVENT, onAsk);
    try {
      const out = await setupWrite<{ ok: boolean }>("boss-dogfood", "try", { directive: "x" }, { label: "Open a PR" });
      expect(out.ok).toBe(true);
    } finally { window.removeEventListener(CONSENT_EVENT, onAsk); }
    expect(asked).toEqual(["First PR"]);
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toBe(bodies[0]);
  });

  it("declined: says so, sends nothing more", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ error: "x", consent: CONSENT }), { status: 409 }));
    vi.stubGlobal("fetch", fetchMock);
    const onAsk = (e: Event) => (e as CustomEvent<{ onDecided: (g: string | null) => void }>).detail.onDecided(null);
    window.addEventListener(CONSENT_EVENT, onAsk);
    try {
      await expect(setupWrite("boss-dogfood", "try", {}, { label: "Open a PR" })).rejects.toThrow(/consent/);
    } finally { window.removeEventListener(CONSENT_EVENT, onAsk); }
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
