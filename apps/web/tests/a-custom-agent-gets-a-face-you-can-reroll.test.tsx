// @vitest-environment happy-dom
/**
 * Custom agents get a procedural robot, and Reroll (spec §3.4 Face, §6 Seeding, §0.2 B).
 * Reroll changes only the seed, so a part the operator picked stays picked; `Auto` hands a
 * part back to the seed; a cast hat is offered only to the role that already wears it.
 */
import React, { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";

import { FacePicker } from "../app/agents/face-picker";
import { avatarSvg, seededSpec } from "../lib/agent-avatar";
import type { AvatarSpec } from "../lib/agent-world-types";
import { CAST_BY_KEY } from "../lib/avatar-kit/cast";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

async function mount(initial: AvatarSpec) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const seen: AvatarSpec[] = [];
  function Harness() {
    const [spec, setSpec] = useState(initial);
    return <FacePicker value={spec} onChange={(s) => { seen.push(s); setSpec(s); }} name="Shift Auditor" />;
  }
  const root = createRoot(host);
  await act(async () => root.render(<Harness />));
  return { host, seen, last: () => seen[seen.length - 1], done: () => act(async () => root.unmount()) };
}

const pick = async (el: HTMLSelectElement, value: string) => act(async () => {
  el.value = value;
  el.dispatchEvent(new Event("change", { bubbles: true }));
});

describe("a custom agent's face", () => {
  it("previews at 24, 48 and 96 in idle, working and needs-you", async () => {
    const t = await mount(seededSpec("agt_custom1"));
    const faces = [...t.host.querySelectorAll<SVGElement>(".ag-face-previews svg.ag-av")];
    expect(faces.map((f) => [f.getAttribute("width"), f.getAttribute("data-state")]))
      .toEqual([["24", "idle"], ["48", "working"], ["96", "needs_you"]]);
    await t.done();
  });

  it("rerolls to a new face, keeping what was picked", async () => {
    const t = await mount(seededSpec("agt_custom1"));
    await pick(t.host.querySelector<HTMLSelectElement>("[data-testid=ag-face-hat]")!, "propeller");
    const before = t.host.querySelector(".ag-face-previews")!.innerHTML;
    await act(async () => t.host.querySelector<HTMLButtonElement>("[data-testid=ag-face-reroll]")!.click());
    const after = t.last();
    expect(after.hat).toBe("propeller");
    expect(after.seed.startsWith("agt_custom1~")).toBe(true);
    expect(t.host.querySelector(".ag-face-previews")!.innerHTML).not.toBe(before);
    await t.done();
  });

  it("Auto hands a part back to the seed; a palette swatch sets the palette", async () => {
    const t = await mount({ ...seededSpec("agt_custom2"), emblem: "gear" });
    await pick(t.host.querySelector<HTMLSelectElement>("[data-testid=ag-face-emblem]")!, "");
    expect("emblem" in t.last()).toBe(false);
    await act(async () => t.host.querySelector<HTMLInputElement>('input[value="purple"]')!.click());
    expect(t.last().palette).toBe("purple");
    expect(avatarSvg(t.last(), { size: 48 })).toContain("#a78bfa");
    await t.done();
  });

  it("offers a cast hat only to the role that wears it", async () => {
    const custom = await mount(seededSpec("agt_custom3"));
    const hats = [...custom.host.querySelectorAll<HTMLOptionElement>("[data-testid=ag-face-hat] option")].map((o) => o.value);
    expect(hats).not.toContain("magnifier");
    await custom.done();
    const vera = await mount(CAST_BY_KEY.qa.avatar);
    const veraHats = [...vera.host.querySelectorAll<HTMLOptionElement>("[data-testid=ag-face-hat] option")].map((o) => o.value);
    expect(veraHats).toContain("magnifier");
    expect(veraHats.filter((h) => h === "headphones")).toEqual([]);
    await vera.done();
  });
});
