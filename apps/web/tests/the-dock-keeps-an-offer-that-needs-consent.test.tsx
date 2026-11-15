// @vitest-environment happy-dom
/**
 * THE DOCK KEEPS AN OFFER THAT STILL NEEDS CONSENT (BOSSAPP-1163), rendered, not read from source.
 *
 * The run dock's "Revise decision" offer on a local-first project's merge decision answered 409
 * consent_required. The dock opened the sheet, and when the person closed it without granting it
 * dropped the offer and started a repair turn — so the button was gone, and on the next visit the
 * thread held a repair instead. Here the real <AgentChat> restores a thread whose newest reply
 * carries the offer (as GET /v1/agent/threads/{id}/messages serves it), and:
 *   - declined: the button stays, nothing else is sent, no repair turn;
 *   - granted: the same press is sent once more, and the offer is spent.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ActionResult } from "../lib/action-result";
import { CONSENT_EVENT, type ConsentRequest } from "../lib/identity/consent";
import { mountAs, settle, stubFetch, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as Record<string, unknown>).React = React;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const answers: ActionResult[] = [];
const pressed: unknown[][] = [];
const turns: unknown[][] = [];
vi.mock("../app/actions", () => ({
  runAgentActionAction: vi.fn(async (...args: unknown[]) => { pressed.push(args); return answers.shift() ?? { ok: true, data: { ok: true } }; }),
  askBossAction: vi.fn(async (...args: unknown[]) => { turns.push(args); return { ok: true, data: {} }; }),
}));

import { AgentChat } from "../app/agent-ui";

const LOCAL = "local_prj_desk/desk";
const LOCAL_409 = {
  error: "consent_required", task: "Fix change #1: Add an SLA timer", task_run_id: "pending:Zk9Q",
  agents: [{ id: "agt_forge", name: "Software Engineer", role_key: "forge" }],
  permissions: ["code_task:read"], projects: ["prj_desk"], github: null,
  local: { repo: LOCAL, repos: [LOCAL], summary: "on this machine" },
  durations: [{ kind: "standing", label: "For 30 days", days: 30 }], max_days: 90,
};
const OFFER = { id: "pa_revise", tool: "decide", label: "Revise decision",
                args: { decision_id: "dec_1", action: "revise", note: "Handle weekends." } };

function serveThread() {
  stubFetch((c: Call) => {
    if (c.path !== "/api/agent") return { body: {} };
    const view = new URLSearchParams(c.search).get("view");
    if (view === "workspace") return { body: { tasks: [], threads: [{ thread_id: "th_1", title: "Merge", message_count: 2 }] } };
    if (view === "messages") {
      return { body: { thread_id: "th_1", messages: [
        { role: "user", content: "Send the SLA change back: handle weekends." },
        { role: "assistant", content: "I can send it back with your note." },
      ], proposed_actions: [OFFER], next_steps: [], working: false } };
    }
    return { body: {} };
  });
}
const offer = () => document.querySelector("[data-testid='agent-proposal-decide']") as HTMLButtonElement | null;
async function press() {
  expect(offer(), "the restored offer is on screen").not.toBeNull();
  await React.act(async () => { offer()!.click(); });
  for (let i = 0; i < 4; i += 1) await settle();
}
function answerConsent(grant: string | null) {
  const asked: ConsentRequest[] = [];
  window.addEventListener(CONSENT_EVENT, (e) => {
    const d = (e as CustomEvent<ConsentRequest>).detail;
    asked.push(d);
    d.onDecided?.(grant);
  }, { once: true });
  return asked;
}

afterEach(() => { unmount(); answers.length = 0; pressed.length = 0; turns.length = 0; });

describe("the dock and an offer that needs consent", () => {
  it("declined: the offer stays standing, nothing more is sent, no repair turn", async () => {
    serveThread();
    answers.push({ ok: false, error: "Needs your consent", data: { consent: LOCAL_409 } });
    const asked = answerConsent(null);
    await mountAs(<AgentChat projectId="prj_desk" actor="ceo" />, "owner");
    for (let i = 0; i < 4; i += 1) await settle();
    await press();
    expect(asked).toHaveLength(1);
    expect(asked[0].local?.repos).toEqual([LOCAL]);
    expect(pressed).toHaveLength(1);
    expect(turns, "no repair turn").toHaveLength(0);
    expect(offer(), "the offer is not spent").not.toBeNull();
    expect(document.body.textContent).toContain("needs your consent first");
  });

  it("granted: the same press is sent once more and the offer is spent", async () => {
    serveThread();
    answers.push({ ok: false, error: "Needs your consent", data: { consent: LOCAL_409 } },
                 { ok: true, data: { ok: true, action: "revise" } });
    answerConsent("dlg_1");
    await mountAs(<AgentChat projectId="prj_desk" actor="ceo" />, "owner");
    for (let i = 0; i < 4; i += 1) await settle();
    await press();
    expect(pressed).toHaveLength(2);
    expect(pressed[1].slice(2, 4)).toEqual(["decide", OFFER.args]);
    expect(turns).toHaveLength(0);
    expect(offer()).toBeNull();
  });
});
