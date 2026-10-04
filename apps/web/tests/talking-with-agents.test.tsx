// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";

/**
 * Talking with agents, in the browser (spec §0.2 A): the 1:1 talk, "Plan it" ending in a card
 * one press starts, and the room — against a fake API that streams Server-Sent Events chunk by
 * chunk, so each assertion happens WHILE a seat is thinking or speaking, the way the operator
 * sees it, not after the fact.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const converse = await import("../lib/agent-converse");
const { ConverseHost } = await import("../app/agents/converse-host");

// ── A fake API ───────────────────────────────────────────────────────────────────────────────

type Call = { method: string; path: string; body: Record<string, unknown> };
let calls: Call[] = [];
let conversations: Record<string, Record<string, unknown>> = {};
/** Each POST that streams gets the next of these; the test pushes its chunks. */
let streams: Array<{ push: (event: string, data: unknown) => void; end: () => void }> = [];
let startReply: (body: Record<string, unknown>) => Response;

const SEATS = {
  pm: { role_key: "pm", name: "Product Manager", callsign: "Juno", tagline: "What problem, for whom?", lifecycle: "active" },
  qa: { role_key: "qa", name: "QA Lead", callsign: null, tagline: "Show me the test.", lifecycle: "active" },
  security_reviewer: { role_key: "security_reviewer", name: "Security Reviewer", callsign: null, tagline: null, lifecycle: "active" },
};

function conversation(id: string, kind: string, seats: string[], over: Record<string, unknown> = {}) {
  return { conversation_id: id, project_id: "p1", kind, title: kind === "room" ? "Login launch" : `Talk with ${seats[0]}`,
           actor: "ceo", seats, lead_role_key: seats[0], status: "open", context: { from: "chip" },
           turn_count: 0, updated_at: new Date().toISOString(), turns: [],
           seat_info: seats.map((k) => SEATS[k as keyof typeof SEATS]), ...over };
}

function sseResponse(signal?: AbortSignal | null): Response {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
  // Like a real fetch: aborting the request errors the body being read.
  signal?.addEventListener("abort", () => controller.error(new DOMException("The operation was aborted.", "AbortError")));
  streams.push({
    push: (event, data) => {
      // Split each block in two, so the reader has to join chunks the way a real proxy splits them.
      const text = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
      const cut = Math.floor(text.length / 2);
      controller.enqueue(encoder.encode(text.slice(0, cut)));
      controller.enqueue(encoder.encode(text.slice(cut)));
    },
    end: () => controller.close(),
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

beforeEach(() => {
  calls = [];
  streams = [];
  conversations = {};
  startReply = () => Response.json({ run_id: "wfr_new", conversation: {} });
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://console.test");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    const path = url.pathname.replace("/api/conversations", "");
    calls.push({ method, path, body });
    if (method === "GET" && path === "/presets") {
      return Response.json({
        items: [{ key: "product_eng_security", label: "Product + Eng + Security",
                  seats: ["pm", "engineering_captain", "security_reviewer"], missing: ["engineering_captain"] },
                { key: "incident", label: "Incident", seats: ["qa", "pm"], missing: [] }],
        agents: Object.values(SEATS) });
    }
    if (method === "GET" && path === "") return Response.json({ items: [] });
    if (method === "GET") return Response.json(conversations[path.slice(1)] ?? {}, { status: conversations[path.slice(1)] ? 200 : 404 });
    if (path === "") {
      const id = `cnv_${Object.keys(conversations).length + 1}`;
      conversations[id] = conversation(id, String(body.kind), body.seats as string[]);
      return Response.json(conversations[id], { status: 201 });
    }
    if (/\/(turns|summary|propose)$/.test(path)) return sseResponse(init?.signal);
    if (path.endsWith("/start")) return startReply(body);
    if (path.endsWith("/decisions")) return Response.json({ decision_id: "dec_1", conversation: conversations[path.split("/")[1]] });
    return Response.json({});
  }));
});

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null; host = null;
  vi.unstubAllGlobals();
});

async function settle() {
  for (let i = 0; i < 25; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

async function mountHost() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<ConverseHost projectId="p1" actor="ceo" />));
  await settle();
}

const q = <T extends Element = HTMLElement>(sel: string) => document.querySelector<T>(sel);
const faceState = (roleKey: string, within = document.body) =>
  within.querySelector(`[data-testid="ag-face-${roleKey}"]`)?.getAttribute("data-state");

async function push(i: number, event: string, data: unknown) {
  await act(async () => { streams[i].push(event, data); });
  await settle();
}

// ── The wire ─────────────────────────────────────────────────────────────────────────────────

describe("the stream reader", () => {
  it("joins split chunks and maps every event to the web's shape", async () => {
    const response = sseResponse();
    const s = streams[0];
    s.push("turn_start", { turn_id: "t1", seq: 2, speaker_kind: "agent", role_key: "qa", act_id: "act_1",
                           reply_to_turn_id: "t0" });
    s.push("token", { turn_id: "t1", text: "Show me " });
    s.push("proposal", { turn_id: "t1", proposal: { proposal_id: "prp_1", workflow_key: "wf", title: "T",
      directive: "D", inputs: { a: 1 }, acceptance_criteria: ["x"], agents: [{ step_id: "s", step_label: "S",
      role_key: null }], why: "w", status: "proposed", readiness: { ok: false, problems: ["Needs a model"] } } });
    s.push("turn_end", { turn_id: "t1", status: "complete", tokens_in: 3, tokens_out: 4, cost_usd: 0.01,
                         receipt_id: "rcpt_1", cited_acts: ["act_9"] });
    s.push("error", { error: { kind: "budget_refused", headline: "No", action: "Raise it", href: "/?area=budget", retry: false } });
    s.push("done", { conversation: { conversation_id: "c", kind: "talk", seats: ["qa"], status: "open", turn_count: 3 } });
    s.end();
    const out = [];
    for await (const e of converse.readEvents(response.body!)) out.push(e);
    expect(out.map((e) => e.type)).toEqual(["turn_start", "token", "proposal", "turn_end", "error", "done"]);
    expect(out[0]).toMatchObject({ turnId: "t1", roleKey: "qa", actId: "act_1", replyToTurnId: "t0" });
    expect(out[2]).toMatchObject({ proposal: { proposalId: "prp_1", inputs: { a: "1" },
      agents: [{ stepId: "s", roleKey: null }], readiness: { ok: false, problems: ["Needs a model"] } } });
    expect(out[3]).toMatchObject({ receiptId: "rcpt_1", citedActs: ["act_9"], tokensOut: 4 });
    expect(out[4]).toMatchObject({ error: { kind: "budget_refused", href: "/?area=budget" } });
  });

  it("an @mention names a seat only when it is one", () => {
    expect(converse.mentionsIn("@qa and @security_reviewer, also @nobody", ["qa", "security_reviewer"]))
      .toEqual(["qa", "security_reviewer"]);
    expect(converse.splitCitations("I ran it [act:act_1] and [act:act_2].", ["act_1"]))
      .toEqual({ text: "I ran it and.", acts: ["act_1"] });
  });
});

// ── 1:1 ──────────────────────────────────────────────────────────────────────────────────────

describe("talking to one agent", () => {
  it("opens from a door, greets through a real turn, and the face is truthful while it does", async () => {
    await mountHost();
    await act(async () => { converse.openTalk("qa", { from: "passport" }); });
    await settle();
    const created = calls.find((c) => c.method === "POST" && c.path === "");
    expect(created?.body).toMatchObject({ kind: "talk", seats: ["qa"], actor: "ceo",
                                          context: { from: "passport" } });
    // The greeting is a real model turn (an empty message), never canned.
    const greet = calls.find((c) => c.path.endsWith("/turns"));
    expect(greet?.body).toMatchObject({ message: "", actor: "ceo", project_id: "p1" });

    const sheet = q('[data-testid="ag-talk"]')!;
    expect(sheet.textContent).toContain("QA Lead");
    expect(q('[data-testid="ag-ai-badge"]')!.getAttribute("title")).toBe("An AI agent. Not a real person.");
    expect(faceState("qa", sheet.querySelector(".ag-talk-head") as HTMLElement)).toBe("idle");

    await push(0, "turn_start", { turn_id: "t1", seq: 1, speaker_kind: "agent", role_key: "qa", act_id: "act_1" });
    expect(faceState("qa", sheet.querySelector(".ag-talk-head") as HTMLElement)).toBe("thinking");
    expect(sheet.querySelector(".ag-cv-badge")!.textContent).toContain("Thinking");

    await push(0, "token", { turn_id: "t1", text: "Hi, I'm the QA Lead " });
    expect(faceState("qa", sheet.querySelector(".ag-talk-head") as HTMLElement)).toBe("speaking");
    expect(sheet.querySelector(".ag-cv-badge")!.textContent).toContain("Speaking");
    await push(0, "token", { turn_id: "t1", text: "— an AI agent." });
    expect(sheet.querySelector('[data-testid="ag-turn-agent"]')!.textContent)
      .toContain("Hi, I'm the QA Lead — an AI agent.");

    conversations.cnv_1.turns = [{ turn_id: "t1", seq: 1, speaker_kind: "agent", role_key: "qa", kind: "message",
      content: "Hi, I'm the QA Lead — an AI agent.", status: "complete", cited_acts: [] }];
    await push(0, "turn_end", { turn_id: "t1", status: "complete", tokens_in: 1, tokens_out: 1, cost_usd: 0, cited_acts: [] });
    await push(0, "done", { conversation: { conversation_id: "cnv_1", kind: "talk", seats: ["qa"], status: "open", turn_count: 1 } });
    await act(async () => { streams[0].end(); });
    await settle();
    expect(faceState("qa", sheet.querySelector(".ag-talk-head") as HTMLElement)).toBe("idle");
    expect(overBudget(sheet)).toEqual([]);
  });

  it("while it answers, Send becomes Stop; Stop cuts the answer off and keeps what was said", async () => {
    await mountHost();
    await act(async () => { converse.openTalk("qa"); });
    await settle();
    const sheet = q('[data-testid="ag-talk"]')!;
    await push(0, "turn_start", { turn_id: "t1", seq: 1, speaker_kind: "agent", role_key: "qa" });
    await push(0, "token", { turn_id: "t1", text: "Hello, I am" });
    expect(sheet.querySelector('[data-testid="ag-send"]')).toBeNull();
    const stop = sheet.querySelector<HTMLButtonElement>('[data-testid="ag-stop"]')!;
    expect(stop.textContent).toBe("Stop");
    expect(stop.title).toBe("Stop answering now");
    // The server has not settled the cut-off answer yet when the console reloads.
    conversations.cnv_1.turns = [{ turn_id: "t1", seq: 1, speaker_kind: "agent", role_key: "qa", kind: "message",
      content: "", status: "streaming" }];
    await act(async () => { stop.click(); });
    await settle();
    expect(sheet.querySelector('[data-testid="ag-stop"]')).toBeNull();
    expect(sheet.querySelector('[data-testid="ag-send"]')).not.toBeNull();
    expect(sheet.textContent).toContain("Stopped.");
    expect(faceState("qa", sheet.querySelector(".ag-talk-head") as HTMLElement)).toBe("idle");
    expect(calls.filter((c) => c.method === "GET" && c.path === "/cnv_1").length).toBeGreaterThan(1);
  });

  it("a refusal says what to do, and nothing is lost from the box's history", async () => {
    await mountHost();
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = new URL(String(input), "http://console.test");
      const path = url.pathname.replace("/api/conversations", "");
      if ((init?.method ?? "GET") === "GET" && path === "/presets") return Response.json({ items: [], agents: [] });
      if ((init?.method ?? "GET") === "GET" && path === "") return Response.json({ items: [] });
      if ((init?.method ?? "GET") === "GET") return Response.json(conversation("cnv_9", "talk", ["qa"]));
      if (path === "") return Response.json(conversation("cnv_9", "talk", ["qa"]), { status: 201 });
      return Response.json({ error: "Needs a model. Set one in Settings.", code: "needs_model" }, { status: 409 });
    }));
    await act(async () => { converse.openTalk("qa"); });
    await settle();
    const problem = q('[data-testid="ag-problem"]')!;
    expect(problem.getAttribute("data-code")).toBe("needs_model");
    expect(problem.textContent).toContain("Needs a model");
    expect(problem.querySelector("a")!.getAttribute("href")).toBe("/?area=setup&step=model");
  });
});

// ── Plan → start ─────────────────────────────────────────────────────────────────────────────

describe("plan it with an agent", () => {
  async function planWithCard() {
    await mountHost();
    await act(async () => { converse.openPlan("pm", { from: "palette" }); });
    await settle();
    await act(async () => { streams[0].end(); });
    await settle();
    conversations.cnv_1.turns = [
      { turn_id: "t0", seq: 1, speaker_kind: "person", kind: "message", content: "A lead triage page", status: "complete" },
      { turn_id: "t1", seq: 2, speaker_kind: "agent", role_key: "pm", kind: "proposal", content: "Here is a plan.",
        status: "complete", payload: { proposal_id: "prp_1", workflow_key: "standard", workflow_name: "Standard delivery",
          title: "Lead triage", directive: "Build a lead triage page.", inputs: { instructions: "Build it" },
          acceptance_criteria: ["Sorts by score"], why: "A build.", status: "proposed",
          agents: [{ step_id: "build", step_label: "Build", role_key: "qa" }, { step_id: "review", step_label: "Review", role_key: null }],
          readiness: { ok: false, problems: ["A connected model"] } } }];
    await act(async () => { await (converse as unknown as { openConversation: (id: string) => void }).openConversation("cnv_1"); });
    await settle();
    return q('[data-testid="ag-proposal"]')!;
  }

  it("the card shows the plan, who runs each step, and what is missing", async () => {
    const card = await planWithCard();
    expect(card.textContent).toContain("Standard delivery");
    expect(card.textContent).toContain("Build a lead triage page.");
    expect(card.querySelector('[data-testid="ag-step-Review"]')!.textContent).toContain("Auto");
    expect(card.querySelector('[data-testid="ag-step-Build"]')!.textContent).toContain("QA Lead");
    expect(card.textContent).toContain("A connected model");
    expect(overBudget(card)).toEqual([]);
  });

  it("Start is one press; a readiness refusal offers Start anyway with exactly the keys shown", async () => {
    let first = true;
    startReply = () => {
      if (first) {
        first = false;
        return Response.json({ error: "This workflow cannot run here yet.", readiness: {
          missing: [{ key: "model", label: "A connected model", fix: "Connect a model in Setup." }] } }, { status: 409 });
      }
      return Response.json({ run_id: "wfr_77", conversation: conversations.cnv_1 });
    };
    const card = await planWithCard();
    const input = card.querySelector<HTMLInputElement>("input")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "Build it well");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const start = card.querySelector<HTMLButtonElement>('[data-testid="ag-start"]')!;
    await act(async () => { start.click(); start.click(); });
    await settle();
    const starts = calls.filter((c) => c.path.endsWith("/start"));
    expect(starts).toHaveLength(1);
    expect(starts[0].body).toMatchObject({ proposal_id: "prp_1", start_anyway: [],
                                           inputs: { instructions: "Build it well" } });
    expect(q('[data-testid="ag-refused"]')!.textContent).toContain("A connected model");
    await act(async () => { q<HTMLButtonElement>('[data-testid="ag-start-anyway"]')!.click(); });
    await settle();
    const again = calls.filter((c) => c.path.endsWith("/start"));
    expect(again).toHaveLength(2);
    expect(again[1].body.start_anyway).toEqual(["model"]);
    expect(q('[data-testid="ag-open-run"]')!.getAttribute("href")).toBe("?area=delivery_workflows&wfr=wfr_77");
  });
});

// ── The room ─────────────────────────────────────────────────────────────────────────────────

describe("the room", () => {
  async function openRoom() {
    await mountHost();
    await act(async () => { converse.openRoom({ seats: ["pm", "qa", "security_reviewer"] }); });
    await settle();
    return q('[data-testid="ag-room"]')!;
  }

  async function send(room: HTMLElement, text: string) {
    const box = room.querySelector<HTMLTextAreaElement>(".ag-composer textarea")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(box, text);
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => { room.querySelector<HTMLButtonElement>('[data-testid="ag-send"]')!.click(); });
    await settle();
  }

  it("seats take turns: thinking, then speaking, then done; the rest say Up next", async () => {
    const room = await openRoom();
    expect(room.querySelectorAll(".ag-seat")).toHaveLength(3);
    await send(room, "Ship the login flow this week?");
    const turn = calls.find((c) => c.path.endsWith("/turns"))!;
    expect(turn.body).toMatchObject({ message: "Ship the login flow this week?", to: [], rounds: 1 });
    expect(room.querySelector('[data-testid="ag-turn-person"]')!.textContent).toContain("Ship the login");

    await push(0, "turn_start", { turn_id: "a", seq: 2, speaker_kind: "agent", role_key: "pm" });
    const seat = (k: string) => room.querySelector(`[data-testid="ag-seat-${k}"]`) as HTMLElement;
    expect(seat("pm").getAttribute("data-state")).toBe("thinking");
    expect(seat("qa").textContent).toContain("Up next");
    await push(0, "token", { turn_id: "a", text: "Juno here: only behind a flag." });
    expect(seat("pm").getAttribute("data-state")).toBe("speaking");
    expect(seat("pm").querySelector(".ag-seat-line")!.textContent).toContain("only behind a flag");
    await push(0, "turn_end", { turn_id: "a", status: "complete", tokens_in: 1, tokens_out: 1, cost_usd: 0, cited_acts: [] });
    await push(0, "turn_start", { turn_id: "b", seq: 3, speaker_kind: "agent", role_key: "qa" });
    expect(seat("pm").getAttribute("data-state")).toBe("done");
    expect(seat("qa").getAttribute("data-state")).toBe("thinking");
    expect(seat("security_reviewer").textContent).toContain("Up next");
    // The face says it in words too, never colour alone.
    expect(seat("pm").querySelector(".ag-cv-badge")!.textContent).toContain("Done");
    expect(seat("qa").querySelector('[role="img"]')!.getAttribute("aria-label")).toBe("QA Lead, AI agent, Thinking");
    await act(async () => { streams[0].end(); });
    await settle();
    expect(overBudget(room)).toEqual([]);
    expect([...room.querySelectorAll("p")].filter((p) => !p.closest("[data-user-text]"))).toEqual([]);
  });

  it("an @mention or a picked seat asks only them; Let them reply asks for a second round", async () => {
    const room = await openRoom();
    await send(room, "@security_reviewer is session fixation covered?");
    expect(calls.filter((c) => c.path.endsWith("/turns")).at(-1)!.body)
      .toMatchObject({ to: ["security_reviewer"], rounds: 1 });
    await act(async () => { streams[0].end(); });
    await settle();
    await act(async () => { room.querySelector<HTMLButtonElement>('[data-testid="ag-to-qa"]')!.click(); });
    await act(async () => { room.querySelector<HTMLInputElement>('[data-testid="ag-debate"]')!.click(); });
    await send(room, "Final answers?");
    expect(calls.filter((c) => c.path.endsWith("/turns")).at(-1)!.body)
      .toMatchObject({ to: ["qa"], rounds: 2 });
  });

  it("the @ menu offers the seats", async () => {
    const room = await openRoom();
    const box = room.querySelector<HTMLTextAreaElement>(".ag-composer textarea")!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(box, "@se");
      box.selectionStart = box.selectionEnd = 3;
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const menu = room.querySelector('[role="listbox"][aria-label="Mention a seat"]');
    expect(menu!.textContent).toContain("Security Reviewer");
    expect(menu!.textContent).not.toContain("QA Lead");
  });

  it("Stop sits in the room's head while it answers; the question asked stays, the rest do not speak", async () => {
    const room = await openRoom();
    expect(room.querySelector('[data-testid="ag-stop"]')).toBeNull();
    await send(room, "Ship it?");
    await push(0, "turn_start", { turn_id: "a", seq: 2, speaker_kind: "agent", role_key: "pm" });
    const stop = room.querySelector<HTMLButtonElement>('.ag-room-head [data-testid="ag-stop"]')!;
    expect(stop).not.toBeNull();
    conversations.cnv_1.turns = [
      { turn_id: "p", seq: 1, speaker_kind: "person", actor: "ceo", kind: "question", content: "Ship it?", status: "complete" },
      { turn_id: "a", seq: 2, speaker_kind: "agent", role_key: "pm", kind: "error", content: "", status: "error",
        payload: { kind: "stopped", headline: "Stopped.", action: "Ask again when ready.", retry: true } }];
    await act(async () => { stop.click(); });
    await settle();
    expect(room.querySelector('[data-testid="ag-stop"]')).toBeNull();
    // The question was recorded before anyone answered: it stays in the transcript.
    expect(room.querySelector('[data-testid="ag-turn-person"]')!.textContent).toContain("Ship it?");
    expect(room.textContent).toContain("Stopped.");
    expect(room.textContent).not.toContain("Up next");
    for (const k of ["pm", "qa", "security_reviewer"]) {
      expect(room.querySelector(`[data-testid="ag-seat-${k}"]`)!.getAttribute("data-state")).toBe("idle");
    }
  });

  it("the summary says who was for and against, and a decision opens once", async () => {
    const room = await openRoom();
    conversations.cnv_1.turns = [
      { turn_id: "a", seq: 1, speaker_kind: "agent", role_key: "pm", kind: "message", content: "x", status: "complete" },
      { turn_id: "s0", seq: 2, speaker_kind: "agent", role_key: "pm", kind: "summary", content: "Wait.",
        status: "complete", payload: { text: "Wait.", by_role_key: "pm", decisions: [],
          next_actions: [{ text: "Pick an owner", owner_role_key: "pm", kind: "decision", decision_id: "dec_0" }] } },
      { turn_id: "s", seq: 3, speaker_kind: "agent", role_key: "pm", kind: "summary", content: "Ship behind a flag.",
        status: "complete", payload: { text: "Ship behind a flag.", by_role_key: "pm",
          decisions: [{ text: "Flag it", for: ["pm"], against: ["security_reviewer"] }],
          next_actions: [{ text: "Pick a date", owner_role_key: "pm", kind: "decision" }] } }];
    await act(async () => { converse.openConversation("cnv_1"); });
    await settle();
    const [older, summary] = [...document.querySelectorAll('[data-testid="ag-summary"]')];
    // An older card's action already opened says so after a reload.
    expect(older.querySelector("a[href*='decision=dec_0']")).not.toBeNull();
    expect(summary.textContent).toContain("For · Juno");
    expect(summary.textContent).toContain("Against · Security Reviewer");
    await act(async () => { summary.querySelector<HTMLButtonElement>('[data-testid="ag-open-decision"]')!.click(); });
    await settle();
    // Each card opens its own action: (its summary turn, index).
    expect(calls.filter((c) => c.path.endsWith("/decisions"))[0].body).toMatchObject({ turn_id: "s", index: 0 });
    expect(summary.querySelector("a[href*='area=decisions']")).not.toBeNull();
    expect(room).toBeTruthy();
  });
});

// ── Doors ────────────────────────────────────────────────────────────────────────────────────

describe("the doors", () => {
  it("a room without seats asks who to seat; a preset missing an agent says so", async () => {
    await mountHost();
    await act(async () => { converse.openRoom(); });
    await settle();
    const picker = q('[data-testid="ag-picker"]')!;
    const missing = picker.querySelector<HTMLButtonElement>('[data-testid="ag-preset-product_eng_security"]')!;
    expect(missing.disabled).toBe(true);
    expect(missing.getAttribute("title")).toBe("Needs engineering_captain");
    const open = picker.querySelector<HTMLButtonElement>('[data-testid="ag-open-room"]')!;
    expect(open.disabled, "one seat is not a room").toBe(true);
    await act(async () => { picker.querySelector<HTMLButtonElement>('[data-testid="ag-pick-qa"]')!.click(); });
    await act(async () => { picker.querySelector<HTMLButtonElement>('[data-testid="ag-pick-pm"]')!.click(); });
    expect(open.disabled).toBe(false);
    expect(overBudget(picker)).toEqual([]);
    await act(async () => { open.click(); });
    await settle();
    expect(calls.find((c) => c.method === "POST" && c.path === "")!.body)
      .toMatchObject({ kind: "room", seats: ["qa", "pm"] });
    expect(q('[data-testid="ag-room"]')).not.toBeNull();
  });

  it("the palette offers talk, plan and room", () => {
    expect(converse.CONVERSE_COMMANDS.map((c) => c.label))
      .toEqual(["Talk to an agent", "Plan it with an agent", "Open a room"]);
  });
});
