// @vitest-environment happy-dom
/**
 * useWorkflowEditor: load a workflow, keep an unsaved draft in this browser, offer it back.
 *
 * Mounted against a fake /api/delivery-workflows. The draft is a convenience only — a blocked
 * storage must not break the editor — and a draft from before a newer version was published
 * says so ("v4 was published since").
 */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkflowEditor } from "../lib/workflow-editor-store";
import { LINEAR, PALETTE, clone } from "./_helpers/workflow-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Editor = ReturnType<typeof useWorkflowEditor>;
let editor: Editor;
let root: Root | null = null;
let activeVersion = 3;
let reads: string[] = [];
let specMissing = false;
let failing = new Set<string>();

function Probe({ k, version }: { k: string; version?: number }) {
  editor = useWorkflowEditor(k, "p1", { version });
  return null;
}

async function mount(k: string, version?: number) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(createElement(Probe, { k, version })); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

/** The same editor, re-rendered with another key or version (the URL changed). */
async function rerender(k: string, version?: number) {
  await act(async () => { root!.render(createElement(Probe, { k, version })); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const draftOf = (k: string) => {
  const raw = window.localStorage.getItem(`boss:wf-draft:p1:${k}`);
  return raw ? JSON.parse(raw) : null;
};

const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 450)); });

beforeEach(() => {
  reads = [];
  activeVersion = 3;
  specMissing = false;
  failing = new Set();
  window.localStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://console.test");
    const view = url.searchParams.get("view") ?? "";
    reads.push(view + (url.searchParams.get("version") ? `@${url.searchParams.get("version")}` : ""));
    if (view === "capabilities") {
      return Response.json({ capabilities: PALETTE.capabilities, blocks: [], agents: [],
                             human_role: "human" });
    }
    if (view === "custom_steps") return Response.json({ steps: [], bindable: {} });
    if (view === "spec" && failing.has(url.searchParams.get("key") ?? "")) {
      return Response.json({ detail: "upstream down" }, { status: 502 });
    }
    if (view === "spec" && specMissing) {
      return Response.json({ detail: "no workflow 'ghost'" }, { status: 404 });
    }
    if (view === "spec") {
      return Response.json({ definition_id: "wfd_1", workflow_key: "ship", name: "Ship",
                             is_catalogue: false, version: Number(url.searchParams.get("version")
                               ?? activeVersion), spec: clone(LINEAR), copy: null,
                             versions: [{ version: activeVersion, published_at: "x", is_active: true }] });
    }
    return Response.json({}, { status: 404 });
  }));
});
afterEach(() => {
  act(() => root?.unmount());
  root = null;
  vi.unstubAllGlobals();
});

describe("useWorkflowEditor", () => {
  it("loads the spec and the palette, clean", async () => {
    await mount("ship");
    expect(editor.loading).toBe(false);
    expect(editor.error).toBeNull();
    expect(editor.state.spec).toEqual(LINEAR);
    expect(editor.state.meta).toMatchObject({ workflow_key: "ship", version: 3 });
    expect(editor.palette?.humanRole).toBe("human");
    expect(editor.dirty).toBe(false);
    expect(editor.restore).toBeNull();
  });

  it("a new workflow loads nothing and starts untitled", async () => {
    await mount("new");
    expect(reads).not.toContain("spec");
    expect(editor.state.meta.name).toBe("Untitled workflow");
    expect(editor.state.loaded).toBeNull();
  });

  it("wires a placed step through the palette without the caller passing it", async () => {
    await mount("ship");
    act(() => editor.dispatch({ type: "INSERT", at: -1, step: { id: "smoke", kind: "capability",
      binding: { capability: "smoke_test", inputs: {} }, next: [] } }));
    expect(editor.state.selection).toBe("smoke");
    expect(editor.state.spec.steps.at(-1)?.binding).toEqual(
      { capability: "smoke_test", inputs: { url: { from_step: "deploy", field: "url" } } });
    expect(editor.state.wired).toEqual({ smoke: ["url"] });
    expect(editor.dirty).toBe(true);
  });

  it("keeps an unsaved draft and offers it back on the next visit", async () => {
    await mount("ship");
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Make it" } }));
    await settle();
    act(() => root?.unmount());

    await mount("ship");
    expect(editor.state.spec).toEqual(LINEAR);
    expect(editor.restore).toMatchObject({ publishedSince: null });
    act(() => editor.restore!.apply());
    expect(editor.state.spec.steps[1].title).toBe("Make it");
    expect(editor.dirty).toBe(true);
    expect(editor.restore).toBeNull();
  });

  it("says when a newer version was published since the draft", async () => {
    await mount("ship");
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Make it" } }));
    await settle();
    act(() => root?.unmount());
    activeVersion = 4;
    await mount("ship");
    expect(editor.restore?.publishedSince).toBe(4);
    act(() => editor.restore!.discard());
    expect(window.localStorage.length).toBe(0);
  });

  it("forgets the draft once the edit is undone", async () => {
    await mount("ship");
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Make it" } }));
    await settle();
    expect(window.localStorage.length).toBe(1);
    act(() => editor.dispatch({ type: "UNDO" }));
    await settle();
    expect(window.localStorage.length).toBe(0);
  });

  it("a save spends the draft kept under the key it was edited as", async () => {
    await mount("new");
    act(() => editor.dispatch({ type: "INSERT", at: -1, step: { id: "only", kind: "timer",
                                                                wait_seconds: 5, next: [] } }));
    await settle();
    expect(window.localStorage.getItem("boss:wf-draft:p1:new")).not.toBeNull();
    act(() => editor.dispatch({ type: "SAVED", spec: editor.state.spec,
      meta: { ...editor.state.meta, definition_id: "wfd_9", workflow_key: "mine", version: 1 } }));
    expect(window.localStorage.getItem("boss:wf-draft:p1:new")).toBeNull();
    expect(editor.dirty).toBe(false);
  });

  it("works with storage blocked", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    await mount("ship");
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Make it" } }));
    await settle();
    expect(editor.state.spec.steps[1].title).toBe("Make it");
    vi.restoreAllMocks();
  });

  it("opens an older version read-only and never stores a draft of it", async () => {
    await mount("ship", 2);
    expect(reads).toContain("spec@2");
    expect(editor.state.meta.readOnlyVersion).toBe(2);
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "x" } }));
    await settle();
    expect(window.localStorage.length).toBe(0);
  });

  it("guards leaving with unsaved changes", async () => {
    await mount("ship");
    const before = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(before);
    expect(before.defaultPrevented).toBe(false);
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "x" } }));
    const after = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(after);
    expect(after.defaultPrevented).toBe(true);
  });

  it("going from an older version back to the running one loads the running one", async () => {
    await mount("ship", 2);
    expect(editor.state.meta).toMatchObject({ version: 2, readOnlyVersion: 2 });
    await rerender("ship");
    expect(reads.filter((r) => r.startsWith("spec"))).toEqual(["spec@2", "spec"]);
    expect(editor.state.meta).toMatchObject({ version: 3, readOnlyVersion: null });
  });

  it("after saving a new workflow the URL's move to its key does not reload it", async () => {
    await mount("new");
    act(() => editor.dispatch({ type: "INSERT", at: -1, step: { id: "only", kind: "timer",
                                                                wait_seconds: 5, next: [] } }));
    act(() => editor.dispatch({ type: "SAVED", spec: editor.state.spec,
      meta: { ...editor.state.meta, definition_id: "wfd_9", workflow_key: "mine", version: 1 } }));
    await rerender("mine");
    expect(reads.filter((r) => r.startsWith("spec"))).toEqual([]);
    expect(editor.state.spec.steps.map((s) => s.id)).toEqual(["only"]);
    // ...and its drafts are kept under its own key from here on.
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "only", patch: { title: "Pause" } }));
    await settle();
    expect(draftOf("mine")?.spec.steps[0].title).toBe("Pause");
  });

  it("a load that fails keeps the draft stored for that workflow", async () => {
    await mount("ship");
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Make it" } }));
    await settle();
    act(() => root?.unmount());
    failing.add("ship");
    await mount("ship");
    await settle();
    expect(editor.error).toBe("upstream down");
    expect(draftOf("ship")?.spec.steps[1].title).toBe("Make it");
  });

  it("a workflow that fails to load never gets another workflow's draft", async () => {
    await mount("ship");
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Make it" } }));
    await settle();
    failing.add("other");
    await rerender("other");
    await settle();
    expect(editor.error).toBe("upstream down");
    expect(editor.state.spec.steps).toEqual([]);
    expect(draftOf("other")).toBeNull();
    expect(draftOf("ship")?.spec.steps[1].title).toBe("Make it");
  });

  it("broken JSON keeps the draft of the edits made before it", async () => {
    await mount("ship");
    act(() => editor.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Make it" } }));
    await settle();
    act(() => editor.dispatch({ type: "SET_JSON_TEXT", text: "{ \"steps\": [" }));
    expect(editor.state.jsonError).not.toBeNull();
    await settle();
    expect(draftOf("ship")?.spec.steps[1].title).toBe("Make it");
  });

  it("reports a workflow that cannot be loaded", async () => {
    specMissing = true;
    await mount("ghost");
    expect(editor.error).toBe("no workflow 'ghost'");
    expect(editor.loading).toBe(false);
  });
});
