// @vitest-environment happy-dom
/**
 * Run Credits (spec §3.6, §11.1): who made this run, as a card you can keep.
 *
 *  - `Download PNG` fetches the image route for this run and saves it as a file.
 *  - `Copy text` copies the same credits as words.
 *  - It opens by itself once, when a run you are watching finishes — and only once.
 *  - The PR footer says how many agents made it and opens the same Credits.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { shipItChain } from "./_helpers/run-acts-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { toChain } = await import("../app/agents/act-wire");
const { CreditsButton, CreditsDialog, creditsText } = await import("../app/agents/run-credits");
const { PrMadeBy } = await import("../app/agents/build-faces");
const { creditsCard } = await import("../app/agents/credits-card");

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);
let root: Root | null = null;
let host: HTMLElement | null = null;
let fetched: string[] = [];

beforeEach(() => {
  fetched = [];
  try { window.sessionStorage.clear(); } catch { /* none */ }
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    fetched.push(String(input));
    return new Response(PNG, { headers: { "Content-Type": "image/png" } });
  }));
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
function render(node: React.ReactNode): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(node));
  return host;
}
async function settle() {
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const CHAIN = toChain(shipItChain());

describe("Run Credits", () => {
  it("lists who made it in chain order, with the totals and the person accountable", () => {
    const el = render(<CreditsDialog chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" onClose={() => {}} />);
    const cast = [...el.querySelectorAll('[data-testid="ag-credits-cast"] li')].map((li) => li.getAttribute("data-role-key"));
    expect(cast).toEqual(["pm", "chief_architect", "engineering_captain", "backend_engineer", "adversarial_reviewer", "qa", "", "release_manager"]);
    expect(el.querySelector('[data-testid="ag-credits-agents"]')?.textContent).toBe("7");
    expect(el.querySelector('[data-testid="ag-credits-accountable"]')?.textContent?.trim()).toBe("You");
  });

  it("credits a person as You only when it was the viewer, on the dialog, the text and the image alike", () => {
    // `cto` started and approved this run; `ceo` is looking at it.
    const theirs = toChain({ ...shipItChain(), accountable: "cto" });
    const el = render(<CreditsDialog chain={theirs} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" onClose={() => {}} />);
    const person = el.querySelector('[data-testid="ag-credits-cast"] li[data-role-key=""]')!;
    expect(person.querySelector(".ag-credits-name strong")?.textContent).toBe("A person");
    expect(person.querySelector(".ag-face")?.getAttribute("data-variant")).toBe("person");
    expect(creditsText(theirs, "ceo", "Ship it")).toContain("• A person, Approved deploy");
    expect(creditsText(theirs, "ceo", "Ship it")).not.toMatch(/• You/);
    expect(creditsCard(theirs, "Ship it").faces.find((f) => !f.roleKey)?.name).toBe("A person");
    // The credit can name its person outright, and then that decides.
    const named = toChain({ ...shipItChain(), credits: [{ role_key: null, person_id: "ceo", line: "Approved deploy" }] });
    expect(creditsText({ ...named, accountable: "cto" }, "ceo", "Ship it")).toContain("• You, Approved deploy");
    // The viewer's own run still says You.
    expect(creditsText(CHAIN, "ceo", "Ship it")).toContain("• You, Approved deploy");
  });

  it("downloads a PNG of this run's credits", async () => {
    const created = vi.fn(() => "blob:credits");
    (URL as unknown as { createObjectURL: unknown }).createObjectURL = created;
    (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
    const clicked: Array<{ href: string; download: string }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.getAttribute("href") ?? "", download: this.download });
    });
    const el = render(<CreditsDialog chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" onClose={() => {}} />);
    await act(async () => (el.querySelector('[data-testid="ag-credits-png"]') as HTMLButtonElement).click());
    await settle();
    expect(fetched).toEqual(["/api/run-credits/wfr_1?project_id=p1&title=Ship+it"]);
    expect(created).toHaveBeenCalledTimes(1);
    expect(clicked).toEqual([{ href: "blob:credits", download: "boss-credits-wfr_1.png" }]);
    expect(el.querySelector('[data-testid="ag-credits-said"]')?.textContent).toBe("Downloaded");
  });

  it("copies the credits as text", async () => {
    const wrote: string[] = [];
    Object.defineProperty(navigator, "clipboard", { configurable: true,
      value: { writeText: vi.fn(async (t: string) => { wrote.push(t); }) } });
    const el = render(<CreditsDialog chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" onClose={() => {}} />);
    await act(async () => (el.querySelector('[data-testid="ag-credits-copy-text"]') as HTMLButtonElement).click());
    await settle();
    expect(wrote[0]).toBe(creditsText(CHAIN, "ceo", "Ship it"));
    expect(wrote[0].split("\n")[0]).toBe("Ship it, made by 7 agents");
    expect(wrote[0]).toContain("Built with BOSS, open source");
  });

  it("opens by itself once, when a run you are watching finishes", () => {
    const el = render(<CreditsButton chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" live />);
    expect(document.querySelector('[data-testid="ag-credits"]')).toBeNull();
    act(() => root!.render(<CreditsButton chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" live={false} />));
    expect(document.querySelector('[data-testid="ag-credits"]')).not.toBeNull();
    act(() => (document.querySelector(".ag-credits-close") as HTMLButtonElement).click());
    // Re-run and finish again in the same tab: it has been shown for this run already.
    act(() => root!.render(<CreditsButton chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" live />));
    act(() => root!.render(<CreditsButton chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" live={false} />));
    expect(document.querySelector('[data-testid="ag-credits"]')).toBeNull();
    expect(el.querySelector('[data-testid="ag-credits-open"]')).not.toBeNull();
  });

  it("does not open for a run that was already finished when you arrived", () => {
    render(<CreditsButton chain={CHAIN} runId="wfr_2" projectId="p1" viewer="ceo" title="Ship it" live={false} />);
    expect(document.querySelector('[data-testid="ag-credits"]')).toBeNull();
  });

  it("is absent for a run no agent worked on", () => {
    const el = render(<CreditsButton chain={toChain({ acts: [], totals: {}, credits: [] })} runId="wfr_3" projectId="p1"
                                     viewer="ceo" title="x" live={false} />);
    expect(el.querySelector('[data-testid="ag-credits-open"]')).toBeNull();
  });

  it("puts `Made by n agents` under the PR and opens the same credits", () => {
    const el = render(<PrMadeBy chain={CHAIN} runId="wfr_1" projectId="p1" viewer="ceo" title="Ship it" />);
    expect(el.querySelector('[data-testid="ag-pr-made-by"]')?.textContent).toContain("Made by 7 agents, accountable: you");
    act(() => (el.querySelector('[data-testid="ag-pr-credits"]') as HTMLButtonElement).click());
    expect(document.querySelector('[data-testid="ag-credits"]')).not.toBeNull();
  });

  it("draws the image card from the same credits, never more than 8 faces and no internal ids", () => {
    const card = creditsCard(CHAIN, "Ship it");
    expect(card.agents).toBe(7);
    expect(card.faces.map((f) => f.roleKey)).toEqual(["pm", "chief_architect", "engineering_captain", "backend_engineer",
      "adversarial_reviewer", "qa", null, "release_manager"]);
    expect(JSON.stringify(card)).not.toMatch(/act_|wfr_|crun_|github/);
    expect(card.time).toBe("42m");
    expect(card.cost).toBe("$1.37");
  });
});
