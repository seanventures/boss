// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs, on the run surfaces of Agent World (spec §2.7, §9, test 14).
 *
 * The Act card, the Chain, Credits, the signed step drawer and the dock head are rendered with
 * fixtures and walked with the Studio's own walker: no visible run over six words, no <p>, and
 * none of the words §9 bans — in the visible text or in a tooltip. And the motion is honest: every
 * keyframe the run section names exists, and reduced motion stills every one of them.
 */
import React, { act } from "react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { shipItChain } from "./_helpers/run-acts-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { toChain } = await import("../app/agents/act-wire");
const { ActCard } = await import("../app/agents/act-card");
const { RunChain } = await import("../app/agents/run-chain");
const { CreditsDialog } = await import("../app/agents/run-credits");
const { StepSignedBy } = await import("../app/agents/step-signed-by");
const { DockAgentHead } = await import("../app/agents/dock-agent");
const { PrMadeBy } = await import("../app/agents/build-faces");

const CHAIN = toChain(shipItChain());
const byId = (id: string) => CHAIN.acts.find((a) => a.actId === id)!;

let root: Root | null = null;
let host: HTMLElement | null = null;
beforeEach(() => {
  const plan = (shipItChain().acts as Record<string, unknown>[])[2];
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({
    ...plan, turns_excerpt: [{ content: "Two options. I rejected the queue." }],
  })));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
});
async function render(node: React.ReactNode): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host;
}

/** §9: words these surfaces never say. `Signed by` is the one sanctioned use of "signed". */
const BANNED = [/\bpersona\b/i, /\bfingerprint\b/i, /\bledger\b/i, /\bacts?\b/i, /\blineage\b/i, /\bsentient\b/i,
  /\balive\b/i, /\bconscious\b/i, /\bimpersonat/i, /decided to delegate/i, /chose to ask/i, /\btokens?\b/i,
  /\bsigned\b(?! by)/i];

function everything(root: Element): string {
  const tips = [...root.querySelectorAll("[data-tip], [aria-label], [title]")]
    .map((e) => [e.getAttribute("data-tip"), e.getAttribute("aria-label"), e.getAttribute("title")].join(" "));
  // The operator's or the agent's own words are theirs, not copy.
  const clone = root.cloneNode(true) as Element;
  clone.querySelectorAll("[data-user-text]").forEach((e) => e.remove());
  return `${clone.textContent ?? ""} ${tips.join(" ")}`;
}

const SURFACES: Array<[string, () => React.ReactNode]> = [
  ["Act card", () => <ActCard act={byId("act_build")} viewer="ceo" />],
  ["Act card, a person's", () => <ActCard act={byId("act_ok")} viewer="ceo" />],
  ["Chain", () => <RunChain chain={CHAIN} viewer="ceo" />],
  ["Credits", () => <CreditsDialog chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" onClose={() => {}} />],
  ["Signed step", () => <StepSignedBy act={byId("act_plan")} projectId="p1" viewer="ceo" question="Why?" />],
  ["Stuck step", () => <StepSignedBy act={{ ...byId("act_plan"), status: "error" }} projectId="p1" viewer="ceo"
                                     failure="pytest exited 1 after 14 failures in tests/test_api.py" question="Why?" />],
  ["Dock head", () => <DockAgentHead roleKey="qa" roleName="QA" state="thinking" projectId="p1" actId="act_plan" onClearAct={() => {}} />],
  ["PR footer", () => <PrMadeBy chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" />],
];

describe("the run surfaces stay concise", () => {
  it.each(SURFACES)("%s: no run of visible text over six words", async (_name, make) => {
    const el = await render(make());
    expect(el.children.length).toBeGreaterThan(0);
    expect(overBudget(document.body)).toEqual([]);
  });

  it.each(SURFACES)("%s: no paragraphs", async (_name, make) => {
    await render(make());
    expect(document.body.querySelectorAll("p").length).toBe(0);
  });

  it.each(SURFACES)("%s: none of the banned words", async (_name, make) => {
    await render(make());
    const said = everything(document.body);
    for (const word of BANNED) expect(`${word}: ${word.test(said)}`).toBe(`${word}: false`);
  });

  it("every face that stands alone is named with its state", async () => {
    const el = await render(<DockAgentHead roleKey="qa" roleName="QA" state="speaking" projectId="p1" />);
    expect(el.querySelector('.ag-face[role="img"]')?.getAttribute("aria-label")).toBe("QA Lead, AI agent, Speaking");
  });
});

describe("the run section's motion is honest", () => {
  const css = readFileSync(resolve(__dirname, "../app/agents/agent-world.css"), "utf8");
  const runs = css.slice(css.indexOf("[runs]"), css.indexOf("[converse]"));

  it("names only keyframes it defines, all agav-run-*", () => {
    const defined = [...runs.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]);
    const used = [...runs.matchAll(/animation:\s*([\w-]+)/g)].map((m) => m[1]).filter((n) => n !== "none");
    expect(defined.length).toBeGreaterThan(3);
    expect(defined.every((n) => n.startsWith("agav-run-"))).toBe(true);
    expect(used.filter((n) => !defined.includes(n))).toEqual([]);
    expect(defined.filter((n, i) => defined.indexOf(n) !== i)).toEqual([]);
  });

  it("animates only transform and opacity", () => {
    for (const m of runs.matchAll(/@keyframes\s+[\w-]+\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g)) {
      const props = [...m[1].matchAll(/([a-z-]+)\s*:/g)].map((p) => p[1]);
      expect(props.filter((p) => p !== "transform" && p !== "opacity")).toEqual([]);
    }
  });

  it("stills every animated element under reduced motion", () => {
    const reduced = runs.match(/@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\n\}/)?.[1] ?? "";
    expect(reduced).toContain("animation: none");
    const animated = [...runs.matchAll(/([^{}]+)\{[^{}]*animation:\s*agav-run/g)].map((m) => m[1].trim());
    for (const sel of animated) {
      const target = sel.split(",").pop()!.trim().split(/\s+/).pop()!.replace(/\[.*$/, "");
      const cls = target.match(/\.[\w-]+/g)?.pop() ?? target;
      expect(`${sel} → ${reduced.includes(cls)}`).toBe(`${sel} → true`);
    }
  });
});
