// @vitest-environment happy-dom
/**
 * Setup › Embedding model: its own slot, like Decisions (operator decision 2026-10-22).
 *
 * With no model the row says meaning-based search is off and why, in the server's words, and
 * offers the three providers. Save & check sends the provider's fields (the key in the body, then
 * cleared from the form) as op `embedding_model`; the answer's check is shown as the model's
 * real reply, or its error. A configured slot is checked again or removed with one press, and
 * the key is never on the page.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EmbeddingModel } from "../lib/setup-types";
import { click, fakeApi, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

const PROVIDERS = [
  { provider: "azure", label: "Azure OpenAI", needs_key: true,
    fields: ["endpoint", "deployment", "api_version", "api_key", "dimension"],
    example_model: "text-embedding-3-small", example_dimension: 1536 },
  { provider: "openai", label: "OpenAI or compatible", needs_key: true,
    fields: ["base_url", "model", "api_key", "dimension"], example_model: "text-embedding-3-small",
    example_dimension: 1536 },
  { provider: "ollama", label: "Ollama (local)", needs_key: false, fields: ["base_url", "model", "dimension"],
    example_model: "nomic-embed-text", example_dimension: 768, example_base_url: "http://localhost:11434" },
];
const NONE: EmbeddingModel = {
  key: "knowledge.embedding_model", configured: false, permission: "model_provider:manage",
  budget_category: "knowledge", search: false, providers: PROVIDERS,
  reason: "meaning-based search is off: no embedding model is set (Setup › Embedding model)",
};
const WORKING: EmbeddingModel = {
  ...NONE, configured: true, search: true, reason: "", provider: "ollama", label: "Ollama (local)",
  model: "nomic-embed-text", dimension: 768, base_url: "http://localhost:11434", has_key: false,
  model_id: "ollama:nomic-embed-text:768", reembed_pending: 12,
  check: { state: "ok", reply: "768 numbers from nomic-embed-text" },
};
const FAILED: EmbeddingModel = {
  ...WORKING, search: false, provider: "openai", label: "OpenAI or compatible", model: "gpt-4o", dimension: 1536,
  has_key: true, check: { state: "failed", error: "NotFoundError: 404 model 'gpt-4o' does not support embeddings" },
  reason: "meaning-based search is off: the embedding model check failed: NotFoundError: 404 model 'gpt-4o' does not support embeddings",
};
const open = "?area=setup&focus=embedding_model";
const KEY = "sk-proj-WEBTESTEMBEDDINGKEY0123456789XYZ";

describe("Setup › Embedding model", () => {
  it("with no model, says search is off and why, and offers the three providers", async () => {
    const view = { ...board(), embedding_model: NONE };
    fakeApi(view);
    mounted = await mountBoard(view, open);
    expect(q(document, "su-embedding-off")!.textContent).toContain("text and graph only");
    expect(q(document, "su-embedding-off")!.getAttribute("title")).toContain("no embedding model is set");
    for (const p of ["azure", "openai", "ollama"]) expect(q(document, `su-embedding-provider-${p}`)).not.toBeNull();
  });

  it("Save & check sends the provider's fields with the key, then clears the key", async () => {
    const view = { ...board(), embedding_model: NONE };
    const api = fakeApi(view);
    api.answer = (call) => call.op === "embedding_model"
      ? { status: 200, body: { ...view, embedding_model: { ...FAILED, check: { state: "ok", reply: "1536 numbers from text-embedding-3-small" },
                                                           model: "text-embedding-3-small", search: true, reason: "" } } }
      : null;
    mounted = await mountBoard(view, open);
    await click(q(document, "su-embedding-provider-azure"));
    await type(q(document, "su-embedding-endpoint"), "https://example.openai.azure.com");
    await type(q(document, "su-embedding-deployment"), "embed-small");
    await type(q(document, "su-embedding-api_key"), KEY);
    await click(q(document, "su-embedding-save"));
    const sent = api.calls.find((c) => c.op === "embedding_model")!;
    expect(sent.body).toMatchObject({ provider: "azure", endpoint: "https://example.openai.azure.com",
                                      deployment: "embed-small", api_key: KEY, dimension: "1536" });
    expect(q(document, "su-embedding-check")!.textContent).toContain("Working");
    expect(q(document, "su-embedding-reply")!.textContent).toBe("1536 numbers from text-embedding-3-small");
    expect(document.body.innerHTML).not.toContain(KEY);
  });

  it("shows the model's real reply, the background re-embed, and checks again or removes", async () => {
    const view = { ...board(), embedding_model: WORKING };
    const api = fakeApi(view);
    mounted = await mountBoard(view, open);
    expect(q(document, "su-embedding-check")!.getAttribute("data-state")).toBe("ok");
    expect(q(document, "su-embedding-reply")!.textContent).toBe("768 numbers from nomic-embed-text");
    expect(q(document, "su-embedding-pending")!.textContent).toBe("Re-embedding: 12 left");
    expect(q(document, "su-embedding-form")).toBeNull();
    await click(q(document, "su-embedding-recheck"));
    expect(api.calls.some((c) => c.op === "embedding_model_check")).toBe(true);
    await click(q(document, "su-embedding-remove"));
    expect(api.calls.some((c) => c.op === "embedding_model_remove")).toBe(true);
  });

  it("a failed check shows the provider's error and why search is off", async () => {
    const view = { ...board(), embedding_model: FAILED };
    fakeApi(view);
    mounted = await mountBoard(view, open);
    expect(q(document, "su-embedding-check")!.getAttribute("data-state")).toBe("failed");
    expect(q(document, "su-embedding-reply")!.textContent).toContain("does not support embeddings");
    expect(q(document, "su-embedding-off")!.getAttribute("title")).toContain("check failed");
  });

  it("an older API without the slot shows no row", async () => {
    fakeApi(board());
    mounted = await mountBoard(board(), open);
    expect(q(document, "su-row-embedding-model")).toBeNull();
  });
});
