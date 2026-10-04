/**
 * The Discovery list passes list rows to the page; the dossier is fetched when one is opened.
 *
 * GET /v1/discoveries returned each run's full research state — 447 KB for 22 runs on boss-1,
 * 425 KB of it `state` — and lib/discoveries.ts passed it straight into DiscoveriesHome, so
 * every console refresh that rendered Discovery serialised every dossier for a list that reads
 * four numbers. Each row the page passes is now held to LIST_ITEM_BUDGET_BYTES, even from an
 * API that still sends the dossier.
 */
import { describe, expect, it, vi } from "vitest";

import { LIST_ITEM_BUDGET_BYTES, payloadBytes } from "../lib/list-budget";
import { codeOnly } from "./_helpers/source";

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: { getDiscoveries: vi.fn() },
}));

const filler = (n: number) => "a cited market signal about veterinary scribes ".repeat(n);

/** Shaped like engine discovery_service's dossier; ~20 KB, what boss-1's runs weigh. */
function fullRun(i: number) {
  return {
    id: `drun_${i}`, project_id: "boss-dogfood", status: i === 0 ? "running" : "completed",
    phase: i === 0 ? "vet" : "done", title: `Run ${i}`,
    seed: i === 0 ? filler(60) : filler(1), context: filler(40),
    state: {
      job_id: `job_${i}`, deep: i % 2 === 0,
      research_brief: { hypothesis: filler(4), questions: [filler(2), filler(2)] },
      signal_findings: Array.from({ length: 7 }, (_, k) => ({
        angle: "demand", finding: filler(5), evidence: filler(14),
        citation_url: `https://example.com/${i}/${k}`,
      })),
      market_analysis: { tam: filler(6), dynamics: { growth: filler(6) } },
      critique: { overall_confidence_0_1: 0.5, biggest_risk: filler(4),
        claims: [{ claim: filler(3), verdict: "supported", reason: filler(3) }],
        disconfirming_evidence: [filler(4)] },
      candidate_outcomes: Array.from({ length: 3 }, () => ({ outcome: filler(3), wedge: filler(4),
        target_pilot: filler(3), confidence_0_1: 0.6 })),
      gate: { verdict: "promising", confidence: 0.62, checks: { has_outcome: true } },
      _diagnostics: { sweep: { reply_head: "x".repeat(1200) }, vet: { reply_head: "x".repeat(1200) } },
    },
    usage: { cost_usd_micros: 950_000, model: "claude" }, error: null,
    created_at: "2026-10-01T12:00:00Z", updated_at: "2026-10-01T12:30:00Z",
    attention: i === 0 ? { kind: "no_worker", state: "waiting", headline: "Queued",
      action: "Check the worker", detail: filler(30) } : null,
  };
}

describe("what the page passes per discovery", async () => {
  const { client } = await import("../lib/client");
  const { fetchDiscoveriesFeed, toDiscoveryListItem } = await import("../lib/discoveries");
  const runs = Array.from({ length: 22 }, (_, i) => fullRun(i));

  it("is a bounded summary, never the dossier, even from an API that still sends it", async () => {
    expect(payloadBytes(runs)).toBeGreaterThan(22 * 15_000);  // the fixture is boss-1's weight
    vi.mocked(client.getDiscoveries).mockResolvedValueOnce({ discoveries: runs } as never);
    const feed = await fetchDiscoveriesFeed("boss-dogfood");
    expect(feed.items).toHaveLength(22);
    for (const item of feed.items) {
      expect(item).not.toHaveProperty("state");
      expect(item).not.toHaveProperty("context");
      expect(payloadBytes(item)).toBeLessThanOrEqual(LIST_ITEM_BUDGET_BYTES);
    }
    expect(payloadBytes(feed.items)).toBeLessThanOrEqual(22 * LIST_ITEM_BUDGET_BYTES);
    // Everything the list row shows survives.
    expect(feed.items[1].summary).toEqual({ verdict: "promising", confidence: 0.62,
      findings: 7, outcomes: 3, deep: false });
    expect(feed.items[0].attention?.headline).toBe("Queued");
    expect(feed.items[1].usage?.cost_usd_micros).toBe(950_000);
  });

  it("keeps a summary the API already made", () => {
    const { state: _s, context: _c, ...row } = fullRun(3);
    const summary = { verdict: "pursue", confidence: 0.8, findings: 9, outcomes: 2, deep: true };
    expect(toDiscoveryListItem({ ...row, summary }).summary).toEqual(summary);
  });
});

describe("the Discovery screen", () => {
  it("the list reads the summary, and the dossier is fetched when one is opened", () => {
    const ui = codeOnly("app/discoveries-ui.tsx");
    const list = ui.slice(ui.indexOf("export function DiscoveriesHome"), ui.indexOf("function DiscoveryDetail"));
    expect(list).not.toMatch(/\bd\.state\b|\bst\.(gate|signal_findings|candidate_outcomes)\b/);
    expect(list).toContain("d.summary");
    const detail = ui.slice(ui.indexOf("function DiscoveryDetail"));
    expect(detail).toContain("fetchDiscoveryDetail(row.id)");
  });

  it("the detail is read through the same-origin proxy", async () => {
    const { fetchDiscoveryDetail } = await import("../lib/discovery-detail");
    const seen: string[] = [];
    const ok = await fetchDiscoveryDetail("drun_1", (async (url: string) => {
      seen.push(url);
      return new Response(JSON.stringify(fullRun(1)), { status: 200 });
    }) as typeof fetch);
    expect(seen).toEqual(["/v1/discoveries/drun_1"]);
    expect(ok.ok && ok.run.state.signal_findings?.length).toBe(7);
    const missing = await fetchDiscoveryDetail("drun_x", (async () =>
      new Response(JSON.stringify({ detail: "discovery run not found" }), { status: 404 })) as typeof fetch);
    expect(missing).toEqual({ ok: false, error: "discovery run not found" });
  });
});
