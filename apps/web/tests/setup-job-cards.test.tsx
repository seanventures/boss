// @vitest-environment happy-dom
/** Who does what as cards (BOSSAPP-1341) and the OpenRouter model field (BOSSAPP-1340). */
import { afterEach, describe, expect, it, vi } from "vitest";

import { board } from "./_helpers/setup-fixtures";
import { click, flush, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import type { SetupView } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.unstubAllGlobals(); });

function stub(view: SetupView) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(view),
    { status: 200, headers: { "Content-Type": "application/json" } })));
}

describe("job cards", () => {
  it("each card shows engine, model, how it is paid, scope and health", async () => {
    const view = board();
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    const card = q(document.body, "su-who-card-review")!;
    expect(card.textContent).toContain("Review");
    expect(card.textContent).toContain("OpenRouter");
    expect(card.textContent).toContain("openai/gpt-5");
    expect(q(card, "su-who-paid-review")!.textContent).toBe("API key");
    expect(card.textContent).toContain("All projects");
    expect(q(card, "su-who-health-review")!.textContent).toBe("Not checked");
    expect(q(document.body, "su-who-paid-build")!.textContent).toBe("Subscription");
  });

  it("never says Working without a server pill; shows the server's pill when sent", async () => {
    const view = board();
    view.jobs = view.jobs.map((j) => j.job === "build" && j.resolved
      ? { ...j, resolved: { ...j.resolved, pill: { state: "failed", tone: "red", label: "Refusing runs", tip: "x" } } } : j);
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    expect(q(document.body, "su-who-health-chat")!.textContent).not.toContain("Working");
    expect(q(document.body, "su-who-pill-build")!.textContent).toContain("Refusing runs");
  });

  it("Change edits in place under the summary and leaves Advanced closed", async () => {
    const view = board();
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    expect(q(document.body, "su-who-edit-build")).toBeNull();
    await click(q(document.body, "su-who-change-build"));
    const edit = q(document.body, "su-who-edit-build")!;
    expect(edit.querySelector("select")).not.toBeNull();
    expect((q(document.body, "su-advanced-all") as HTMLDetailsElement).open).toBe(false);
    await click(q(document.body, "su-who-change-build"));
    expect(q(document.body, "su-who-edit-build")).toBeNull();
  });

  it("a card shows the pay toggle for an engine with two ways", async () => {
    const view = board();
    view.jobs = view.jobs.map((j) => j.job === "build" ? { ...j, options: [
      { engine: "codex", model: "sub:gpt-6-sol", label: "Codex · subscription · gpt-6-sol", metered: false },
      { engine: "codex", model: "key:gpt-6-sol", label: "Codex · API key · gpt-6-sol", metered: true }],
      choice: { engine: "codex", model: "sub:gpt-6-sol" },
      resolved: { engine: "codex", model: "sub:gpt-6-sol", label: "Codex · gpt-6-sol", why: "chosen" } } : j);
    stub(view);
    mounted = await mountBoard(view, "?area=setup", true, "setup");
    expect(q(document.body, "su-who-paid-build")!.textContent).toBe("Subscription");
    await click(q(document.body, "su-who-change-build"));
    expect(q(document.body, "su-job-pick-build")).not.toBeNull();
  });
});

describe("OpenRouter in Add a model", () => {
  it("pre-fills no model and Connect needs only the key", async () => {
    const view = board();
    stub(view);
    mounted = await mountBoard(view, "?area=setup&add=openrouter", true, "setup");
    const field = q(document.body, "su-model-name") as HTMLInputElement | null;
    expect(field).not.toBeNull();
    expect(field!.value).toBe("");
    expect(field!.placeholder).toMatch(/provider picks/i);
  });

  it("shows OpenRouter model prices in picker options and beside the chosen model", async () => {
    const view = board();
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : {};
      if (body.op === "model_catalogue") {
        return new Response(JSON.stringify({
          models: ["openai/gpt-5", "deepseek/deepseek-chat"],
          prices: {
            "openai/gpt-5": { prompt_usd_per_mtok: 2.5, completion_usd_per_mtok: 10.0 },
            "deepseek/deepseek-chat": { prompt_usd_per_mtok: 0.14, completion_usd_per_mtok: 0.28 },
          },
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(JSON.stringify(view),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    mounted = await mountBoard(view, "?area=setup&add=openrouter", true, "setup");
    await flush();
    const opts = Array.from(document.querySelectorAll("#su-model-options option"));
    expect(opts.map((o) => o.getAttribute("label"))).toEqual([
      "$2.50 / $10.00 per 1M",
      "$0.14 / $0.28 per 1M",
    ]);
    expect(q(document.body, "su-model-price")).toBeNull();
    const field = q(document.body, "su-model-name") as HTMLInputElement;
    await type(field, "openai/gpt-5");
    expect(q(document.body, "su-model-price")!.textContent).toBe("$2.50 / $10.00 per 1M");
  });

  it("handles null, missing, negative, and non-finite prices gracefully without throwing", async () => {
    const view = board();
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : {};
      if (body.op === "model_catalogue") {
        const res = new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
        res.json = async () => ({
          models: ["null/prompt", "null/completion", "nan/model", "inf/model", "neg/model", "free/model", "paid/model"],
          prices: {
            "null/prompt": { prompt_usd_per_mtok: null, completion_usd_per_mtok: 0.0 },
            "null/completion": { prompt_usd_per_mtok: 0.0, completion_usd_per_mtok: null },
            "nan/model": { prompt_usd_per_mtok: Number.NaN, completion_usd_per_mtok: 0.0 },
            "inf/model": { prompt_usd_per_mtok: 0.0, completion_usd_per_mtok: Number.POSITIVE_INFINITY },
            "neg/model": { prompt_usd_per_mtok: -1, completion_usd_per_mtok: 0.0 },
            "free/model": { prompt_usd_per_mtok: 0, completion_usd_per_mtok: 0 },
            "paid/model": { prompt_usd_per_mtok: 0.5, completion_usd_per_mtok: 1.5 },
          },
        });
        return res;
      }
      return new Response(JSON.stringify(view),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    mounted = await mountBoard(view, "?area=setup&add=openrouter", true, "setup");
    await flush();
    const opts = Array.from(document.querySelectorAll("#su-model-options option"));
    expect(opts.map((o) => o.getAttribute("label"))).toEqual([
      null,
      null,
      null,
      null,
      null,
      "Free",
      "$0.50 / $1.50 per 1M",
    ]);
    const field = q(document.body, "su-model-name") as HTMLInputElement;
    for (const bad of ["null/prompt", "null/completion", "nan/model", "inf/model", "neg/model"]) {
      await type(field, bad);
      expect(q(document.body, "su-model-price")).toBeNull();
    }
    await type(field, "free/model");
    expect(q(document.body, "su-model-price")!.textContent).toBe("Free");
    await type(field, "paid/model");
    expect(q(document.body, "su-model-price")!.textContent).toBe("$0.50 / $1.50 per 1M");
  });

  it("ignores stale model_catalogue responses after rapidly switching providers", async () => {
    const view = board();
    let resolveOpenRouter!: (res: Response) => void;
    const openRouterPending = new Promise<Response>((resolve) => {
      resolveOpenRouter = resolve;
    });
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : {};
      if (body.op === "model_catalogue" && body.provider === "openrouter") {
        return openRouterPending;
      }
      return new Response(JSON.stringify(view),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    mounted = await mountBoard(view, "?area=setup&add=openrouter", true, "setup");
    // Switch rapidly to OpenAI before the OpenRouter catalogue request settles.
    await click(q(document.body, "su-provider-openai"));
    resolveOpenRouter(new Response(JSON.stringify({
      models: ["stale/openrouter-model"],
      prices: { "stale/openrouter-model": { prompt_usd_per_mtok: 1.0, completion_usd_per_mtok: 2.0 } },
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await flush();
    expect(Array.from(document.querySelectorAll("#su-model-options option"))).toHaveLength(0);
  });
});
