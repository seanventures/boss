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

    const opened: { kind: string; roleKey?: string }[] = [];
    const onOpen = (e: Event) => opened.push((e as CustomEvent<{ kind: string; roleKey?: string }>).detail);
    window.addEventListener("boss:converse", onOpen);
    await click(birth);
    window.removeEventListener("boss:converse", onOpen);
    expect(onCreated).toHaveBeenCalledWith("release_auditor");
    expect(toasts).toContain("Tally is born · agt_newb…");
    // The agent introduces itself: its 1:1 talk opens, in its own voice.
    expect(opened).toEqual([expect.objectContaining({ kind: "talk", roleKey: "release_auditor" })]);
  });

  it("the face is the kit's picker: Reroll re-seeds and keeps the parts picked by hand", async () => {
    const { calls } = installAgentWorldApi([(url, init) => (url === "/v1/agents" && init.method === "POST" ? created("x_agent") : undefined)]);
    const agentWorld = await import("../lib/agent-world");
    const spy = vi.spyOn(agentWorld, "newSeed").mockImplementation(() => "aaaaaaaaaa");
    await open();
    await act(async () => { type($('[data-testid="aq-name"]') as HTMLInputElement, "X Agent"); });
    const picker = $('[data-testid="aq-face-voice"] [data-testid="ag-face-picker"]');
    expect(picker).not.toBeNull();
    // A cast hat is never offered to a new agent: it would dress it as a cast member.
    const hats = [...picker!.querySelectorAll<HTMLOptionElement>('[data-testid="ag-face-hat"] option')].map((o) => o.value);
    expect(hats).not.toContain("magnifier");
    const emblem = picker!.querySelector<HTMLSelectElement>('[data-testid="ag-face-emblem"]')!;
    await act(async () => {
      emblem.value = "gear";
      emblem.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click($('[data-testid="ag-face-reroll"]'));
    await click($('[data-testid="aq-create"]'));
    const b = posts(calls);
    expect(b).toHaveLength(1);
    expect(b[0].body).toMatchObject({ name: "X Agent", born_from: { kind: "blank" } });
    const avatar = (b[0].body!.persona as { avatar: { seed: string; emblem?: string } }).avatar;
    expect(avatar.seed).toMatch(/^aaaaaaaaaa~/);
    expect(avatar.emblem).toBe("gear");
    expect($('[data-testid="ag-face-reroll"]')).toBeNull(); // the birth has replaced the dialog
    spy.mockRestore();
  });

  it("a callsign another agent answers to is refused before it is sent", async () => {
    const { calls } = installAgentWorldApi();
    await open();
    await act(async () => { type($('[data-testid="aq-name"]') as HTMLInputElement, "Second QA"); });
    await act(async () => { type($('[data-testid="aq-callsign"]') as HTMLInputElement, "Vera"); });
    await settle();
    expect($('[data-testid="aq-face-voice"]')?.textContent).toContain("Taken");
    expect(($('[data-testid="aq-create"]') as HTMLButtonElement).disabled).toBe(true);
    expect(posts(calls)).toHaveLength(0);
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

describe("From archetype: the kit's cast, none of them a person", () => {
  it("offers the operator's crew first and the 'thinks like' archetypes last, from the kit's cast", async () => {
    const { CAST } = await import("../lib/avatar-kit/cast");
    installAgentWorldApi();
    await open();
    await click($('[data-testid="aq-tab-archetype"]'));
    const keys = [...document.querySelectorAll('[data-testid^="aq-arch-"][role="radio"]')]
      .map((b) => b.getAttribute("data-testid")!.replace("aq-arch-", ""));
    expect(keys).toEqual(CAST.filter((c) => c.designed === "hand").map((c) => c.roleKey));
    expect(keys.slice(0, 5)).toEqual(["engineering_captain", "backend_engineer", "frontend_engineer",
                                      "operations_engineer", "sre"]);
    expect(keys.slice(-4)).toEqual(["first_principles_researcher", "visionary_founder", "relentless_operator",
                                    "teacher_who_simplifies"]);
    expect($('[data-testid="aq-arch-visionary_founder"]')?.getAttribute("title")).toMatch(/^Inspired by a way of thinking/);
  });

  it("a new agent born from qa is not a second Vera: no callsign, no tagline, no cast parts", async () => {
    const { CAST_BY_KEY } = await import("../lib/avatar-kit/cast");
    const { calls } = installAgentWorldApi([(url, init) => (url === "/v1/agents" && init.method === "POST" ? created("my_qa_lead") : undefined)]);
    await open();
    await click($('[data-testid="aq-tab-archetype"]'));
    await click($('[data-testid="aq-arch-qa"]'));
    expect(($('[data-testid="aq-arch-name"]') as HTMLInputElement).value).toBe("My QA Lead");
    await click($('[data-testid="aq-arch-create"]'));
    const body = posts(calls)[0].body!;
    expect(body.born_from).toEqual({ kind: "template", template_key: "qa" });
    const persona = body.persona as { callsign?: string; tagline?: string; avatar: Record<string, string> };
    expect(persona.callsign).toBeUndefined();
    expect(persona.tagline).toBeUndefined();
    expect(JSON.stringify(body)).not.toContain("Vera");
    const vera = CAST_BY_KEY.qa.avatar;
    expect(persona.avatar.hat).toBeUndefined();
    expect(persona.avatar.emblem).toBeUndefined();
    expect(persona.avatar.seed).not.toBe(vera.seed);
    expect(persona.avatar.seed).toMatch(/^[0-9a-z]{10}$/);
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
    expect(birthPersona({ archetype: null }).archetype).toBeNull();
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

describe("From template: a shipped role keeps its face", () => {
  it("installs qa with the cast face untouched — no new seed, no persona face", async () => {
    const { calls } = installAgentWorldApi([
      (url) => (url.includes("/v1/agent-studio/templates") ? Response.json({ items: [
        { id: "tpl_qa", name: "QA Lead", description: "Tests", role_key: "qa", category: "Quality", installed: true, active: true },
      ] }) : undefined),
      (url, init) => (url === "/v1/agents" && init.method === "POST" ? created("qa") : undefined),
    ]);
    await open();
    await click($('[data-testid="aq-tab-template"]'));
    await click($('[data-testid="aq-template-tpl_qa"] button'));
    const body = posts(calls)[0].body!;
    expect(body.born_from).toEqual({ kind: "template", template_key: "tpl_qa" });
    expect((body.persona as { avatar?: unknown } | undefined)?.avatar).toBeUndefined();
  });
});
