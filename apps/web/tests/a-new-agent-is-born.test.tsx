// @vitest-environment happy-dom
/**
 * A new agent is born (Agent World spec §3.11 — S-agents acceptance "Create plays the birth and
 * lands on the Passport").
 *
 * Blank, From template and From archetype all make the agent through POST /v1/agents with WHERE
 * it came from (`born_from`) and the face it was born with (`persona.avatar.seed`). Create plays
 * the birth — a certificate with its agt_ id, stamped BORN — and then hands over: the Passport
 * opens on it (onCreated) and the agent is asked to introduce itself. The face's Reroll changes
 * only the seed; an archetype never claims to be a person.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installAgentWorldApi, type Call } from "./_helpers/agent-world-api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { AgentQuickCreate, bornFromFor, birthPersona } = await import("../app/agent-quick-create");

let root: Root | null = null;
let host: HTMLElement | null = null;
let toasts: string[] = [];
const onToast = (e: Event) => toasts.push((e as CustomEvent<{ message: string }>).detail.message);

beforeEach(() => {
  window.history.replaceState(null, "", "/?area=workforce");
  toasts = [];
  window.addEventListener("boss:toast", onToast);
});
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  document.body.innerHTML = "";
  window.removeEventListener("boss:toast", onToast);
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 10; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
const click = async (el: HTMLElement | null) => {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); });
  await settle();
};
function type(el: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}
const created = (r: string) => Response.json({ role_key: r, agent_id: "agt_newb0rn42x" }, { status: 201 });

async function open(onCreated = vi.fn()) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<AgentQuickCreate projectId="p1" actor="ceo" onClose={() => {}} onCreated={onCreated} />));
  await settle();
  return onCreated;
}
const posts = (calls: Call[]) => calls.filter((c) => c.method === "POST" && c.url === "/v1/agents");

describe("Blank: born with a face and a voice", () => {
  it("sends born_from blank and the face seed, plays the birth, then opens the Passport and says hello", async () => {
    const { calls } = installAgentWorldApi([(url, init) => (url === "/v1/agents" && init.method === "POST" ? created("release_auditor") : undefined)]);
    const onCreated = await open();
    await act(async () => { type($('[data-testid="aq-name"]') as HTMLInputElement, "Release Auditor"); });
    await act(async () => { type($('[data-testid="aq-callsign"]') as HTMLInputElement, "Tally"); });
    await act(async () => { type($('[data-testid="aq-tagline"]') as HTMLInputElement, "Every release, counted."); });
    await settle();
    await click($('[data-testid="aq-create"]'));

    const body = posts(calls)[0].body!;
    expect(body).toMatchObject({ actor: "ceo", name: "Release Auditor", born_from: { kind: "blank" } });
    const persona = body.persona as { callsign: string; tagline: string; avatar: { kit: string; seed: string } };
    expect(persona.callsign).toBe("Tally");
    expect(persona.tagline).toBe("Every release, counted.");
    expect(persona.avatar.kit).toBe("boss-v1");
    expect(persona.avatar.seed).toMatch(/^[0-9a-z]{10}$/);

    // The birth: a certificate with its id, stamped.
    const birth = $('[data-testid="ag-birth"]');
    expect(birth).not.toBeNull();
    expect(birth!.textContent).toContain("agt_newb0rn42x");
    expect(birth!.textContent).toContain("BORN");
    expect(birth!.textContent).toContain("AI agent");
    expect(onCreated).not.toHaveBeenCalled();

    await click(birth);
    expect(onCreated).toHaveBeenCalledWith("release_auditor");
    expect(toasts).toContain("Tally is born · agt_newb…");
    // The agent is asked to introduce itself (the side dock until S-converse's talk lands).
    expect(new URLSearchParams(window.location.search).get("dock_role")).toBe("Release Auditor");
  });

  it("Reroll changes the face seed and nothing else", async () => {
    const { calls } = installAgentWorldApi([(url, init) => (url === "/v1/agents" && init.method === "POST" ? created("x_agent") : undefined)]);
    const agentWorld = await import("../lib/agent-world");
    const seeds = ["aaaaaaaaaa", "bbbbbbbbbb"];
    const spy = vi.spyOn(agentWorld, "newSeed").mockImplementation(() => seeds.shift() ?? "cccccccccc");
    await open();
    await act(async () => { type($('[data-testid="aq-name"]') as HTMLInputElement, "X Agent"); });
    await click($('[data-testid="aq-reroll"]'));
    await click($('[data-testid="aq-create"]'));
    const b = posts(calls);
    expect(b).toHaveLength(1);
    expect(b[0].body).toMatchObject({ name: "X Agent", born_from: { kind: "blank" } });
    expect((b[0].body!.persona as { avatar: { seed: string } }).avatar.seed).toBe("bbbbbbbbbb");
    expect($('[data-testid="aq-reroll"]')).toBeNull(); // the birth has replaced the dialog
    spy.mockRestore();
  });

  it("a refused create shows the API's sentence and plays no birth", async () => {
    installAgentWorldApi([(url, init) => (url === "/v1/agents" && init.method === "POST"
      ? Response.json({ detail: "Persona can't change what the agent may do" }, { status: 422 }) : undefined)]);
    await open();
    await act(async () => { type($('[data-testid="aq-name"]') as HTMLInputElement, "Bad Agent"); });
    await click($('[data-testid="aq-create"]'));
    expect($('[data-testid="aq-failed"]')?.textContent).toBe("Persona can't change what the agent may do");
    expect($('[data-testid="ag-birth"]')).toBeNull();
  });
});

describe("From archetype: fourteen faces, none of them a person", () => {
  it("offers all fourteen, and a 'thinks like' archetype says it is a way of thinking", async () => {
    installAgentWorldApi();
    await open();
    await click($('[data-testid="aq-tab-archetype"]'));
    expect(document.querySelectorAll('[data-testid^="aq-arch-"][role="radio"]')).toHaveLength(14);
    expect($('[data-testid="aq-arch-visionary_founder"]')?.getAttribute("title")).toMatch(/^Inspired by a way of thinking/);
  });

  it("an archetype is born_from archetype; a role face is born_from its template", async () => {
    const { calls } = installAgentWorldApi([(url, init) => (url === "/v1/agents" && init.method === "POST" ? created("my_visionary_founder") : undefined)]);
    const onCreated = await open();
    await click($('[data-testid="aq-tab-archetype"]'));
    await click($('[data-testid="aq-arch-visionary_founder"]'));
    expect(($('[data-testid="aq-arch-name"]') as HTMLInputElement).value).toBe("My Visionary Founder");
    await click($('[data-testid="aq-arch-create"]'));
    const body = posts(calls)[0].body!;
    expect(body.born_from).toEqual({ kind: "archetype", archetype: "visionary_founder" });
    expect((body.persona as { archetype: string }).archetype).toBe("visionary_founder");
    await click($('[data-testid="ag-birth"]'));
    expect(onCreated).toHaveBeenCalledWith("my_visionary_founder");

    expect(bornFromFor("qa")).toEqual({ kind: "template", template_key: "qa" });
    expect(birthPersona({ seed: "abc", archetype: null }).archetype).toBeNull();
  });

  it("Draft about me is one model call in that voice, and says when there is no model", async () => {
    const { calls } = installAgentWorldApi([(url) => (url.includes("/persona/draft")
      ? Response.json({ detail: { code: "needs_model", message: "Needs a model." } }, { status: 409 }) : undefined)]);
    await open();
    await click($('[data-testid="aq-tab-archetype"]'));
    await click($('[data-testid="aq-arch-relentless_operator"]'));
    await click($('[data-testid="aq-draft-about"]'));
    const draft = calls.filter((c) => c.url.includes("/v1/agents/relentless_operator/persona/draft"));
    expect(draft).toHaveLength(1);
    expect(draft[0].body).toMatchObject({ field: "about_me", actor: "ceo" });
    expect(document.body.textContent).toContain("Needs a model. Set one in Settings.");
  });
});
