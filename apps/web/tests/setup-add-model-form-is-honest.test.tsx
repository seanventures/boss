// @vitest-environment happy-dom
/**
 * Add a model offers only what the backend accepts (model-connection audit P1-5, P2-9):
 * "Other" asks for its key before Connect (the backend refuses an API-key connection without
 * one), and Local › Ollama's URL placeholder is where this BOSS actually looks — the address
 * the server reports, never the container's own 127.0.0.1.
 */
import { afterEach, describe, expect, it } from "vitest";

import { click, fakeApi, mountBoard, q, type, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; document.body.innerHTML = ""; });

const connect = () => q(document.body, "su-model-connect") as HTMLButtonElement;

describe("Other needs its key", () => {
  it("a URL and a model without a key cannot Connect, and says why", async () => {
    const api = fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "su-add-model-open"));
    await click(q(document.body, "su-tile-api_key"));
    await click(q(document.body, "su-provider-custom"));
    await type(q(document.body, "su-model-base-url"), "https://llm.example.com/v1");
    await type(q(document.body, "su-model-name"), "my-model");
    expect(connect().disabled).toBe(true);
    expect(connect().getAttribute("data-tip")).toBe("Paste a key first");
    await click(connect());
    expect(api.calls.some((c) => c.op === "models")).toBe(false);

    await type(q(document.body, "su-model-key"), "sk-a-real-looking-key-0123");
    expect(connect().disabled).toBe(false);
  });

  it("a key without the base URL still asks for the URL first", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "su-add-model-open"));
    await click(q(document.body, "su-tile-api_key"));
    await click(q(document.body, "su-provider-custom"));
    await type(q(document.body, "su-model-key"), "sk-a-real-looking-key-0123");
    await type(q(document.body, "su-model-name"), "my-model");
    expect(connect().disabled).toBe(true);
    expect(connect().getAttribute("data-tip")).toBe("Add the base URL first");
  });
});

describe("Local › Ollama points where BOSS looks", () => {
  const openOllama = async (exampleBaseUrl?: string) => {
    const view = board();
    view.detected = view.detected.filter((d) => d.provider !== "ollama");
    view.catalogue = view.catalogue.map((c) => c.provider === "ollama"
      ? { ...c, example_base_url: exampleBaseUrl } : c);
    fakeApi(view);
    mounted = await mountBoard(view);
    await click(q(document.body, "su-add-model-open"));
    await click(q(document.body, "su-tile-local"));
    await click(q(document.body, "su-provider-ollama"));
    return q(document.body, "su-model-base-url") as HTMLInputElement;
  };

  it("uses the address the server reports", async () => {
    const url = await openOllama("http://gpu-box.lan:11434/v1");
    expect(url.placeholder).toBe("gpu-box.lan:11434");
  });

  it("never falls back to the container's own loopback", async () => {
    const url = await openOllama(undefined);
    expect(url.placeholder).not.toContain("127.0.0.1");
    expect(url.placeholder).toBe("host.docker.internal:11434");
  });
});
