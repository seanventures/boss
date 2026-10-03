// @vitest-environment happy-dom
/**
 * What the lead found walking the Workflow Studio in the real console (1512x801), each pinned as
 * the behaviour an operator sees:
 *
 *  6. A long step title was cut off by the type chip ("Build, review ar | Built-in"). The title
 *     now has the row (it wraps, two lines at most) and the chip rides the id line.
 *  7. The agent picker: names were squeezed to "A…"; EVERY agent claimed "✓ has
 *     github.create_pr_real" on a built-in step (it read the capability's own tools as something
 *     an agent must hold); and the list opened upward over the top bar.
 *  8. Save's tooltip stayed open over the "Save as my copy" popover's name field.
 * 11. "Ready" stayed green after an edit, vouching for a draft nobody had checked.
 */
import React, { act, useReducer, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import type { EditorAction, EditorState, Palette, Spec } from "../lib/workflow-editor-types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { Inspector } = await import("../app/workflows/editor/inspector/inspector");
const { placePopover } = await import("../app/workflows/editor/inspector/popover");
const { SaveCopyPopover } = await import("../app/workflows/editor/save/save-copy-popover");
const { SummaryChips } = await import("../app/workflows/editor/summary-chips");
const { useTips } = await import("../app/workflows/flow/tip-layer");
const { PALETTE, reducer, makeState, setValue, q } = await import("./_helpers/workflow-editor-fakes");

/** A built-in step whose capability uses BOSS's own GitHub tools, beside an agent task. */
const PAL: Palette = {
  ...PALETTE,
  capabilities: [...PALETTE.capabilities,
    { key: "build_review", label: "Build, review and open a pull request", summary: "", risk_tier: 1,
      required_inputs: [], optional_inputs: [], produces: ["pr_url"],
      skills: ["github.create_pr_real", "github.review"] }],
  agents: [
    { role_key: "qa", name: "QA Lead", skills: [], mcp: [] },
    { role_key: "dev", name: "Developer With A Rather Long Display Name", skills: ["web.search"], mcp: [] },
  ],
};

const SPEC: Spec = {
  entry_step: "build",
  steps: [
    { id: "build", kind: "capability", next: ["write"], title: "Build, review and open a pull request for it",
      binding: { capability: "build_review", inputs: {} } },
    { id: "write", kind: "capability", next: [], captain: "dev",
      binding: { agent_work: true, inputs: {}, skills: ["web.search", "github.create_pr"] } },
  ],
};

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null;
});

async function render(node: React.ReactNode) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  return host;
}

const box: { state: EditorState | null; dispatch: (a: EditorAction) => void } = { state: null, dispatch: () => {} };

function Drawer({ initial }: { initial: EditorState }) {
  const [state, dispatch] = useReducer(reducer, initial);
  box.state = state;
  box.dispatch = dispatch;
  return <Inspector state={state} palette={PAL} dispatch={dispatch} projectId="p1" actor="ceo"
                    refreshPalette={async () => {}} onClose={() => {}} />;
}
const drawer = (selection: string) =>
  render(<Drawer initial={{ ...makeState(JSON.parse(JSON.stringify(SPEC))), selection }} />);

const optionRows = () => [...document.querySelectorAll('[data-testid="wf-agent-pop"] [role="option"]')];

describe("6. the inspector header", () => {
  it("gives the title the row and puts the type chip on the id line", async () => {
    const el = await drawer("build");
    const chip = q(el, "wf-step-type")!;
    expect(chip.textContent).toBe("Built-in");
    expect(chip.closest(".wf-insp-id")).not.toBeNull();
    // The title is a wrapping box, never sharing its line with the chip.
    const title = q<HTMLTextAreaElement>(el, "wf-step-title")!;
    expect(title.tagName).toBe("TEXTAREA");
    expect(title.parentElement!.contains(chip)).toBe(false);
    expect(title.value).toBe("Build, review and open a pull request for it");
  });

  it("is still one line of text: Enter leaves the box and a pasted break becomes a space", async () => {
    const el = await drawer("build");
    const title = q<HTMLTextAreaElement>(el, "wf-step-title")!;
    await act(async () => { setValue(title, "Build\nand ship"); });
    expect(box.state!.spec.steps[0].title).toBe("Build and ship");
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    await act(async () => { title.dispatchEvent(enter); });
    expect(enter.defaultPrevented).toBe(true);
  });
});

describe("7. the agent picker", () => {
  it("on a built-in step claims nothing about holdings: its tools are BOSS's own", async () => {
    const el = await drawer("build");
    await act(async () => { q<HTMLButtonElement>(el, "wf-agent-trigger")!.click(); });
    const rows = optionRows();
    expect(rows).toHaveLength(3);
    for (const row of rows) {
      expect(row.textContent).not.toContain("✓");
      expect(row.textContent).not.toContain("✗");
      expect(row.textContent).not.toContain("github.create_pr_real");
    }
  });

  it("on an agent task says what each agent really holds, and what it misses", async () => {
    const el = await drawer("write");
    await act(async () => { q<HTMLButtonElement>(el, "wf-agent-trigger")!.click(); });
    const [, qa, dev] = optionRows();
    expect(qa.querySelector('[data-testid="wf-agent-option-status"]')!.textContent).toBe("✗ missing web.search +1");
    expect(qa.textContent).toContain("0 skills");
    expect(dev.querySelector('[data-testid="wf-agent-option-status"]')!.textContent).toBe("✗ missing github.create_pr");
  });

  it("an agent holding everything the step scopes is the only one that says ✓ has", async () => {
    const spec: Spec = { ...SPEC, steps: [SPEC.steps[0], { ...SPEC.steps[1], binding: {
      agent_work: true, inputs: {}, skills: ["web.search"] } }] };
    const el = await render(<Drawer initial={{ ...makeState(spec), selection: "write" }} />);
    await act(async () => { q<HTMLButtonElement>(el, "wf-agent-trigger")!.click(); });
    const [, qa, dev] = optionRows();
    expect(qa.textContent).toContain("✗ missing web.search");
    expect(dev.textContent).toContain("✓ has web.search");
  });

  it("gives the name its own line, whole, with the holdings beneath it", async () => {
    const el = await drawer("write");
    await act(async () => { q<HTMLButtonElement>(el, "wf-agent-trigger")!.click(); });
    const dev = optionRows()[2];
    const name = dev.querySelector('[data-testid="wf-agent-option-name"]')!;
    expect(name.textContent).toBe("Developer With A Rather Long Display Name");
    // The status is not squeezed into the name's line.
    expect(name.parentElement!.querySelector(".wf-combo-meta")!.textContent).toContain("1 skill");
    expect(name.contains(dev.querySelector('[data-testid="wf-agent-option-status"]'))).toBe(false);
  });

  it("opens as a float at least as wide as the field, capped to the room it has", async () => {
    const el = await drawer("write");
    const trigger = q<HTMLButtonElement>(el, "wf-agent-trigger")!;
    trigger.getBoundingClientRect = () => ({ top: 300, bottom: 334, left: 1120, right: 1500, width: 380,
      height: 34, x: 1120, y: 300, toJSON: () => ({}) }) as DOMRect;
    await act(async () => { trigger.click(); });
    const pop = document.querySelector<HTMLElement>('[data-testid="wf-agent-pop"]')!;
    expect(pop.classList.contains("wf-pop-float")).toBe(true);
    expect(pop.getAttribute("data-side")).toBe("below");
    expect(parseFloat(pop.style.width)).toBeGreaterThanOrEqual(380);
    expect(pop.style.maxHeight).not.toBe("");
  });
});

describe("7c. where a popover goes", () => {
  const VIEW = { width: 1512, height: 801 };
  const field = (top: number, width = 340) => ({ top, bottom: top + 34, left: 1150, width });

  it("stays below a field halfway down the page, scrolling rather than jumping over the top bar", () => {
    // The old rule flipped this one up to y≈44, over the editor's top bar.
    const p = placePopover(field(450), 400, 360, true, VIEW);
    expect(p.above).toBe(false);
    expect(p.top).toBe(490);
    expect(p.top + Math.min(400, p.maxHeight)).toBeLessThanOrEqual(VIEW.height - 12);
  });

  it("flips above only when there is no real room below, and still fits the viewport", () => {
    const p = placePopover(field(660), 400, 360, true, VIEW);
    expect(p.above).toBe(true);
    expect(p.top).toBeGreaterThanOrEqual(12);
    expect(p.top + Math.min(400, p.maxHeight)).toBeLessThanOrEqual(660 - 6);
  });

  it("is at least the field's width and at least the asked width, never wider than the viewport", () => {
    expect(placePopover(field(100, 300), 100, 360, true, VIEW).width).toBe(360);
    expect(placePopover(field(100, 420), 100, 360, true, VIEW).width).toBe(420);
    expect(placePopover(field(100, 300), 100, 360, false, { width: 320, height: 600 }).width).toBe(296);
    const right = placePopover({ top: 100, bottom: 134, left: 1400, width: 100 }, 100, 360, true, VIEW);
    expect(right.left + right.width).toBeLessThanOrEqual(VIEW.width - 12);
  });
});

describe("7. the float's own rules beat the Library card menu's", () => {
  it("is fixed and uncapped whatever order the stylesheets load in", () => {
    // library.css loads after inspector.css and styles `.wf-pop` absolute, `right: 0`, max 340px:
    // the picker was squeezed by it. The float's rules carry two classes, so they always win.
    const css = readFileSync(join(process.cwd(), "app/workflows/inspector.css"), "utf8");
    const rule = css.slice(css.indexOf(".wf-pop.wf-pop-float {"), css.indexOf("}", css.indexOf(".wf-pop.wf-pop-float {")));
    expect(rule).toContain("position: fixed");
    expect(rule).toContain("right: auto");
    expect(rule).toMatch(/max-width: calc\(100vw - 24px\)/);
    expect(rule).toContain("overflow: auto");
  });
});

describe("8. a tooltip never covers the popover its button opened", () => {
  const TIP = "Shipped with BOSS. Saving makes your own copy.";
  function SaveBar() {
    useTips();
    const ref = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    return (
      <>
        <button ref={ref} type="button" data-tip={TIP} data-testid="save" onClick={() => setOpen(true)}>Save</button>
        <SaveCopyPopover anchor={ref} open={open} onClose={() => setOpen(false)} standardName="Ship it"
                         copy={null} busy={false} nameError="" onSave={() => {}} />
      </>
    );
  }
  const bubble = () => document.getElementById("wf-tip");

  it("closes the trigger's tip when its popover opens, and keeps it closed while open", async () => {
    const el = await render(<SaveBar />);
    const save = q<HTMLButtonElement>(el, "save")!;
    // Keyboard focus shows the tip at once (the click's own focus does the same in a browser).
    await act(async () => { save.dispatchEvent(new FocusEvent("focusin", { bubbles: true })); });
    expect(bubble()!.hidden).toBe(false);
    expect(bubble()!.textContent).toBe(TIP);

    await act(async () => { save.click(); });
    expect(document.querySelector('[data-testid="wf-copy-name"]')).not.toBeNull();
    expect(bubble()!.hidden).toBe(true);

    // Back on the trigger while its popover is open: still no tip over the name field.
    await act(async () => { save.dispatchEvent(new FocusEvent("focusin", { bubbles: true })); });
    expect(bubble()!.hidden).toBe(true);
    expect(save.hasAttribute("data-pop-open")).toBe(true);
  });
});

describe("11. Ready only vouches for the draft on screen", () => {
  function Chips({ initial }: { initial: EditorState }) {
    const [state, dispatch] = useReducer(reducer, initial);
    box.state = state;
    box.dispatch = dispatch;
    return <SummaryChips state={state} views={new Map()} onSettings={() => {}} onIssues={() => {}} />;
  }
  const OK = { ok: true, problems: [], summary: { steps: 2, agents: [], approvals: [], typical_minutes: null, needs: [] } };

  it("turns from Ready to a neutral Not checked on any edit", async () => {
    const el = await render(<Chips initial={makeState(SPEC)} />);
    await act(async () => box.dispatch({ type: "SET_PROBLEMS", result: OK, spec: box.state!.spec }));
    expect(q(el, "wf-chip-ready")).not.toBeNull();

    await act(async () => box.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Reordered" } }));
    expect(q(el, "wf-chip-ready")).toBeNull();
    expect(q(el, "wf-chip-unchecked")!.textContent).toBe("Not checked");
    expect(q(el, "wf-chip-unchecked")!.getAttribute("data-tone")).toBeNull();

    // The re-check of the new draft brings it back.
    await act(async () => box.dispatch({ type: "SET_PROBLEMS", result: OK, spec: box.state!.spec }));
    expect(q(el, "wf-chip-ready")).not.toBeNull();
  });

  it("a check that answers for an older draft does not make the new one Ready", async () => {
    const el = await render(<Chips initial={makeState(SPEC)} />);
    const sent = box.state!.spec;
    await act(async () => box.dispatch({ type: "PATCH_STEP", id: "build", patch: { title: "Edited in flight" } }));
    await act(async () => box.dispatch({ type: "SET_PROBLEMS", result: OK, spec: sent }));
    expect(q(el, "wf-chip-ready")).toBeNull();
    expect(q(el, "wf-chip-unchecked")).not.toBeNull();
    // Undo back to exactly what was checked: that draft WAS checked.
    await act(async () => box.dispatch({ type: "UNDO" }));
    expect(q(el, "wf-chip-ready")).not.toBeNull();
  });
});
