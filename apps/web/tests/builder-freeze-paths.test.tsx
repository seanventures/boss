// @vitest-environment happy-dom
/**
 * A builder freezes a file or a data table. A write to a frozen path is refused
 * and the screen says the bytes stayed. An unfrozen path can change.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FreezePaths } from "../app/builder/freeze-paths";

const apiWrite = vi.hoisted(() => vi.fn());
vi.mock("../lib/api-write", () => ({ apiWrite }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Row = { kind: string; path: string; frozen: boolean; frozen_by: string };

let paths: Row[] = [];
let root: Root | null = null;
let host: HTMLElement | null = null;

async function settle(n = 12) {
  for (let i = 0; i < n; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

async function render() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(<FreezePaths projectId="p1" />); });
  await settle();
}

function $(sel: string) {
  const el = document.querySelector<HTMLElement>(sel);
  if (!el) throw new Error(`nothing matches ${sel}`);
  return el;
}

async function click(sel: string) {
  await act(async () => {
    $(sel).dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  await settle();
}

async function typeIn(sel: string, value: string) {
  const el = $(sel) as HTMLInputElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await settle(4);
}

beforeEach(() => {
  paths = [];
  apiWrite.mockImplementation(async (url: string, init: { method: string; body?: string }) => {
    const body = init.body ? JSON.parse(String(init.body)) : {};
    if (String(url).endsWith("/v1/freeze-paths") && init.method === "POST") {
      paths = [...paths, { kind: body.kind, path: body.path, frozen: true, frozen_by: "builder" }];
      return new Response(JSON.stringify({ ...body, frozen: true }), { status: 200 });
    }
    if (String(url).endsWith("/v1/freeze-paths/thaw")) {
      paths = paths.filter((row) => !(row.kind === body.kind && row.path === body.path));
      return new Response(JSON.stringify({ ...body, frozen: false }), { status: 200 });
    }
    if (String(url).endsWith("/v1/freeze-paths/write")) {
      const held = paths.some((row) => row.kind === body.kind && row.path === body.path);
      if (held) {
        return new Response(JSON.stringify({
          detail: `${body.path} is frozen. The write was refused and the bytes stayed.`,
        }), { status: 409 });
      }
      return new Response(JSON.stringify({
        changed: true, kind: body.kind, path: body.path, bytes: String(body.content).length,
      }), { status: 200 });
    }
    return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
  });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    expect(String(url)).toContain("/v1/freeze-paths");
    expect(String(url)).toContain("project_id=p1");
    return new Response(JSON.stringify({ project_id: "p1", paths }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  }));
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  apiWrite.mockReset();
});

describe("builder freeze paths", () => {
  it("starts empty and says what a freeze does", async () => {
    await render();
    expect($("h1").textContent).toBe("Hold these paths.");
    const empty = $("[data-testid='freeze-empty']");
    expect(empty.tagName).toBe("P");
    expect(empty.textContent || "").toMatch(/Nothing is held/);
    expect((empty.textContent || "").split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
  });

  it("freezes a file, refuses a write, then lets an unfrozen path change", async () => {
    await render();
    await typeIn("[data-testid='freeze-path']", "notes.txt");
    await click("[data-testid='freeze-submit']");

    const slide = $("[data-testid='freeze-slide']");
    expect(slide.getAttribute("data-path")).toBe("notes.txt");
    expect(slide.getAttribute("data-kind")).toBe("file");
    expect(slide.textContent).toMatch(/HOLD/);

    const freezeCall = apiWrite.mock.calls.find((call) => String(call[0]).endsWith("/v1/freeze-paths"));
    expect(freezeCall?.[1].method).toBe("POST");
    expect(JSON.parse(String(freezeCall?.[1].body))).toMatchObject({
      project_id: "p1", kind: "file", path: "notes.txt",
    });

    await typeIn("[data-testid='freeze-bytes']", "replaced");
    await click("[data-testid='freeze-try']");
    const result = $("[data-testid='freeze-result']");
    expect(result.tagName).toBe("P");
    expect(result.textContent).toMatch(/refused/);
    expect(result.textContent).toMatch(/bytes stayed/);
    expect(result.textContent).not.toMatch(/path changed/);

    await click("[data-testid='freeze-thaw']");
    expect(document.querySelector("[data-testid='freeze-slide']")).toBeNull();

    await typeIn("[data-testid='freeze-path']", "open.txt");
    ($("[data-testid='freeze-kind']") as HTMLSelectElement).value = "table";
    await act(async () => {
      $("[data-testid='freeze-kind']").dispatchEvent(new Event("change", { bubbles: true }));
    });
    await typeIn("[data-testid='open-bytes']", "v2");
    await click("[data-testid='open-try']");
    expect($("[data-testid='freeze-result']").textContent).toMatch(/path changed/);
    const write = apiWrite.mock.calls.filter((call) => String(call[0]).endsWith("/write")).at(-1);
    expect(JSON.parse(String(write?.[1].body))).toMatchObject({
      project_id: "p1", kind: "table", path: "open.txt", content: "v2",
    });
  });

  it("shows a failed freeze instead of dropping it", async () => {
    apiWrite.mockResolvedValueOnce(new Response(JSON.stringify({
      detail: "a path is a relative file or table name",
    }), { status: 422 }));
    await render();
    await click("[data-testid='freeze-submit']");
    const result = $("[data-testid='freeze-result']");
    expect(result.tagName).toBe("P");
    expect(result.textContent).toMatch(/relative file or table name/);
  });

  it("keeps tooltips at or under 14 words", async () => {
    await render();
    await typeIn("[data-testid='freeze-path']", "notes.txt");
    await click("[data-testid='freeze-submit']");
    const titles = [...document.querySelectorAll("[title]")].map((el) => el.getAttribute("title") || "");
    expect(titles.length).toBeGreaterThan(0);
    for (const title of titles) {
      expect(title.split(/\s+/).filter(Boolean).length).toBeLessThanOrEqual(14);
    }
  });
});
