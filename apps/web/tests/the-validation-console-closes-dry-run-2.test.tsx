// @vitest-environment happy-dom
/**
 * DRY RUN 2's CONSOLE GAPS (Lumen Pay → Northgate, change #14), each pinned on the run's own
 * GET …/validation answer (tests/_helpers/dryrun2-validation.json, ids replaced):
 *   #2  A "Needs you" finding offers "Remove these lines": the block, a reason, the engine's delete
 *       (`remove`, or the empty edit). Accept shows only while the agent's proposal can be applied.
 *   #7  An open finding never reads "→ (removed)"; V-06's before/after is its own change, not
 *       "true → true"; an override names the person and says what they really did ("edited").
 *   #10 After "Confirm it's you", the action resumes on its first send: the new CSRF token is
 *       read before the replay, so the API never refuses it `403 csrf_failed`.
 *   #11 The partner's line is the number everywhere (Validation tab, fix cards, merge card); the
 *       branch's line is the tooltip.
 *   #12 The Work panel's funnel and the merge card's checks row print the same counts, guard
 *       findings included, with "2 need you"; a ruleset propose says which change it opened.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, settle, unmount } from "./_helpers/live-render";
import DRY2 from "./_helpers/dryrun2-validation.json";
import {
  countsWords, funnelOf, lineTitle, lineWords, parsePacket, parseRulesets, parseValidation, partnerNow, removalBlock, validationCounts,
} from "../lib/validation-review";
import { RULESET_NORTHGATE } from "../lib/validation-review-fixtures";
import { ValidationFlow } from "../app/validation/validation-tab";
import { ReviewPacketView } from "../app/validation/review-packet";
import { ValidationBuilt } from "../app/validation/built-summary";
import { RulesetEditor } from "../app/validation/ruleset-editor";
import { primePeople } from "../app/who";
import { idPost, setCsrfToken } from "../lib/identity/api";
import { installMirror } from "../lib/identity/mirror";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
beforeEach(() => { primePeople({ usr_ownerfixture0001: "Priya Raman" }); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); primePeople(null); installMirror(null); setCsrfToken(""); });

type Call = { method: string; path: string; body: Record<string, unknown> | null };
type Reply = { status: number; body: unknown } | undefined;
function engine(reply: (c: Call) => Reply = () => undefined) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    const call = { method: init?.method ?? "GET", path, body: init?.body ? JSON.parse(String(init.body)) : null };
    calls.push(call);
    const r = reply(call);
    if (r) return new Response(JSON.stringify(r.body), { status: r.status, headers: { "content-type": "application/json" } });
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
  }));
  return calls;
}
const press = async (el: Element | null) => { await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  await act(async () => {
    const input = el as HTMLInputElement | HTMLTextAreaElement;
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
};
const fix = (id: string) => $(`[data-testid="vr-fix"][data-finding="${id}"]`)!;
const posts = (calls: Call[]) => calls.filter((c) => c.method === "POST");

/** The run's answer, with the agent's refused proposal on V-07 (as the Validation tab showed it). */
function dry2(proposal: Record<string, unknown> | null = { before: "  - path: /v1/admin/accounts", after: "  - path: /v1/payments/refunds", applicable: true }) {
  const raw = JSON.parse(JSON.stringify(DRY2)) as { validation: { findings: Record<string, unknown>[] } };
  raw.validation.findings = raw.validation.findings.map((f) => (f.id === "V-07" ? { ...f, proposed: proposal } : f));
  return parseValidation(raw)!;
}
const flow = async (v = dry2()) => mount(<ValidationFlow v={v} projectId="northgate-gateway" change={14} onChanged={() => {}} />);

describe("#2 Remove these lines", () => {
  it("the admin route's block (partner lines 21–33) is shown; a reason is required; the engine's remove is sent", async () => {
    const calls = engine();
    await flow();
    const card = fix("V-07");
    expect(card.querySelector('[data-testid="vr-remove"]')).not.toBeNull();
    // Not offered on a finding an agent fixes (only "Needs you").
    expect(fix("V-09").querySelector('[data-testid="vr-remove"]')).toBeNull();
    await press(card.querySelector('[data-testid="vr-remove"]'));
    const lines = [...card.querySelectorAll('[data-testid="vr-remove-lines"] span')].map((e) => e.textContent);
    expect(lines[0]).toBe("-   - path: /v1/admin/accounts");
    expect(lines).toHaveLength(13);
    expect(lines.at(-1)).toBe("-       burst: 10");
    expect(card.querySelector('[data-testid="vr-remove-where"]')!.textContent).toContain("lines 21–33 of the partner's file");
    expect(card.querySelector('[data-testid="vr-remove-where"]')!.getAttribute("title")).toBe("On the branch: lines 32–44 of config/tenants/lumen-pay.yaml");
    const send = card.querySelector('[data-testid="vr-remove-send"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect(send.textContent).toBe("Remove 13 lines");
    await type(card.querySelector('[data-testid="vr-remove-reason"]'), "Internal admin surface; never for tenants.");
    await press(send);
    const sent = posts(calls);
    expect(sent).toHaveLength(1);
    expect(sent[0].path).toBe("/v1/changes/northgate-gateway/14/findings/V-07/override");
    expect(sent[0].body).toEqual({ action: "remove", reason: "Internal admin surface; never for tenants.", head_sha: "fa05ac2d6d71e40d29aa1160c5d2c3ade3656f5d" });
    expect(card.querySelector('[data-testid="vr-row-said"]')!.textContent).toBe("V-07 removed · re-checking now.");
  });

  it("an engine that took the empty edit instead: the same press falls back to {action: edit, text: ''}", async () => {
    const calls = engine((c) => (c.method === "POST" && c.body?.action === "remove"
      ? { status: 422, body: { detail: [{ loc: ["body", "action"], msg: "Input should be 'accept', 'reject', 'edit', 'waive' or 'reopen'" }] } } : undefined));
    await flow();
    const card = fix("V-07");
    await press(card.querySelector('[data-testid="vr-remove"]'));
    await type(card.querySelector('[data-testid="vr-remove-reason"]'), "Drop the admin route.");
    await press(card.querySelector('[data-testid="vr-remove-send"]'));
    expect(posts(calls).map((c) => c.body)).toEqual([
      { action: "remove", reason: "Drop the admin route.", head_sha: "fa05ac2d6d71e40d29aa1160c5d2c3ade3656f5d" },
      { action: "edit", text: "", reason: "Drop the admin route.", head_sha: "fa05ac2d6d71e40d29aa1160c5d2c3ade3656f5d" },
    ]);
    expect(card.querySelector('[data-testid="vr-row-said"]')!.textContent).toBe("V-07 removed · re-checking now.");
  });

  it("an engine with neither says it needs the latest BOSS, on the row", async () => {
    engine((c) => (c.method !== "POST" ? undefined : c.body?.action === "remove"
      ? { status: 422, body: { error: "bad_action", message: "Unknown action 'remove'." } }
      : { status: 422, body: { error: "text_required", message: "Write the replacement line." } }));
    await flow();
    const card = fix("V-07");
    await press(card.querySelector('[data-testid="vr-remove"]'));
    await type(card.querySelector('[data-testid="vr-remove-reason"]'), "x");
    await press(card.querySelector('[data-testid="vr-remove-send"]'));
    expect(card.querySelector('[data-testid="vr-row-said"]')!.textContent).toContain("Removing lines needs the latest BOSS");
  });

  it("removalBlock: a single key line is one line; a list item takes everything nested under it", () => {
    const text = (DRY2 as { validation: { input: { text: string } } }).validation.input.text;
    expect(removalBlock(text, { location: { file: "", line: 28, end_line: null, partner_line: 17 }, original: null })!.lines).toEqual(['      api_key: "[SECRET]"']);
    expect(removalBlock(text, { location: { file: "", line: 46, end_line: null, partner_line: 35 }, original: null })).toMatchObject({ from: 35, to: 37 });
    expect(removalBlock("", { location: { file: "", line: 9, end_line: null, partner_line: null }, original: "  x: 1" })).toMatchObject({ from: 9, to: 9, lines: ["  x: 1"] });
  });

  it("Accept on the agent's proposal is shown only while it can be applied; a 409 proposal_gone hides it and says so", async () => {
    const calls = engine((c) => (c.method === "POST" ? { status: 409, body: { error: "proposal_gone", message: "The lines this proposal changes are not on the branch." } } : undefined));
    await flow();
    const card = fix("V-07");
    expect(card.querySelector('[data-testid="vr-proposal-gone"]')).toBeNull();
    await press(card.querySelector('[data-testid="vr-accept"]'));
    expect(posts(calls)[0].body).toMatchObject({ action: "accept" });
    expect(card.querySelector('[data-testid="vr-accept"]')).toBeNull();
    expect(card.querySelector('[data-testid="vr-proposal-gone"]')!.textContent).toBe("The agent's proposal is no longer valid; edit or remove instead.");
    expect(card.querySelector('[data-testid="vr-row-said"]'), "said once, on the proposal").toBeNull();
    // Edit and Remove are still there.
    expect(card.querySelector('[data-testid="vr-edit"]')).not.toBeNull();
    expect(card.querySelector('[data-testid="vr-remove"]')).not.toBeNull();
  });

  it("a proposal the engine marks not applicable never offers Accept", async () => {
    engine();
    await flow(dry2({ before: "x", after: "y", applicable: false }));
    const card = fix("V-07");
    expect(card.querySelector('[data-testid="vr-accept"]')).toBeNull();
    expect(card.querySelector('[data-testid="vr-proposal-gone"]')!.textContent).toBe("The agent's proposal is no longer valid; edit or remove instead.");
    await flow(dry2({ before: "x", after: "y", applicable: true, gone: true }));
    expect($$('[data-testid="vr-fix"][data-finding="V-07"] [data-testid="vr-accept"]')).toHaveLength(0);
  });
});

describe("#7 the merge card and the fix cards say what is true", () => {
  const packet = () => parsePacket((DRY2 as { packet: unknown }).packet)!;

  it("an open finding never reads → (removed); V-06 is 'unchanged · fixed in h-04', not 'true → true'", async () => {
    engine();
    await mount(<ReviewPacketView packet={packet()} projectId="northgate-gateway" />);
    const now = (id: string) => $(`[data-testid="vr-packet-row"][data-finding="${id}"] [data-testid="vr-packet-now"]`)!.textContent;
    for (const id of ["V-05", "V-07", "V-03", "V-08"]) expect(now(id), id).toBe("→ unchanged");
    expect(now("V-06")).toBe("→ unchanged · fixed in h-04");
    expect($$('[data-testid="vr-packet-now"]').map((e) => e.textContent)).not.toContain("→ (removed)");
    expect(partnerNow({ original: "  - path: /x", fixed: null, status: "fixed", hunk_id: "" }).after).toBe("(removed)");
  });

  it("the override names the person and the real action: 'Priya Raman edited V-06', never 'reject+edit · a person'", async () => {
    engine();
    await mount(<ReviewPacketView packet={packet()} projectId="northgate-gateway" />);
    const row = $('[data-testid="vr-override-row"]')!.textContent!;
    expect(row).toContain("Priya Raman edited V-06");
    expect(row).not.toContain("reject+edit");
    expect(row).not.toContain("a person");
  });

  it("on the Validation tab: open V-05's line is context (no '-'), V-06 shows its hunk's change, O-1 'Priya Raman · edited the lines'", async () => {
    engine();
    await flow();
    const v05 = fix("V-05").querySelector('[data-testid="vr-diff-lines"]')!;
    expect(v05.getAttribute("data-kind")).toBe("unchanged");
    expect(v05.querySelector(".d")).toBeNull();
    expect(v05.textContent).toContain("still on the branch · not fixed yet");
    const v06 = fix("V-06").querySelector('[data-testid="vr-diff-lines"]')!;
    expect([...v06.querySelectorAll(".d")].map((e) => e.textContent)).toEqual(['-       allow_origins: ["*"]']);
    expect([...v06.querySelectorAll(".a")].map((e) => e.textContent)).toEqual(['+       allow_origins: ["https://dashboard.lumenpay.example"]']);
    const rec = fix("V-06").querySelector('[data-testid="vr-override-record"]')!;
    expect(rec.textContent).toContain("O-1 · Priya Raman");
    expect(rec.textContent).toContain("edited the lines");
  });
});

describe("#10 a step-up resumes the action on its first send", () => {
  it("the replay after 'Confirm it's you' carries the rotated session's CSRF token — no 403 csrf_failed", async () => {
    setCsrfToken("tok-before-step-up");
    let session = "before";
    installMirror({ check: () => ({ allowed: true, reason: "", needsStepUp: false }) as never, refresh: async () => {},
                    stepUp: async () => { session = "after"; return true; } });     // the step-up rotates the session
    const sent: { path: string; csrf: string; status: number }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/v1/identity/me")) return new Response(JSON.stringify({ csrf_token: `tok-${session}-step-up` }), { status: 200 });
      const csrf = new Headers(init?.headers).get("X-Boss-CSRF") ?? "";
      const status = session === "before" ? 401 : csrf === "tok-after-step-up" ? 200 : 403;
      sent.push({ path, csrf, status });
      const body = status === 401 ? { error: "step_up_required" } : status === 403 ? { error: "csrf_failed" } : { override: { ref: "O-2" } };
      return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    }));
    await expect(idPost("/v1/changes/northgate-gateway/14/findings/V-07/override", { action: "remove" })).resolves.toEqual({ override: { ref: "O-2" } });
    expect(sent.map((s) => s.status)).toEqual([401, 200]);
    expect(sent[1].csrf).toBe("tok-after-step-up");
  });
});

describe("#11 the partner's line is the number; the branch's is the tooltip", () => {
  it("lineWords/lineTitle", () => {
    const loc = { file: "", line: 28, end_line: null, partner_line: 17 };
    expect(lineWords({ location: loc })).toBe("L17");
    expect(lineTitle({ location: loc })).toBe("Line 17 of the partner's file · line 28 on the branch");
    expect(lineWords({ location: { file: "", line: 40, end_line: 42, partner_line: 30 } })).toBe("L30–32");
    expect(lineWords({ location: { file: "x.go", line: 69, end_line: null, partner_line: null } })).toBe("L69");
  });

  it("the Validation tab, the fix card and the merge card agree: secret L17, admin L21, guard L36", async () => {
    engine();
    await flow();
    const p0 = $$('[data-testid="vr-grp-P0"] [data-testid="vr-finding"]').map((e) => `${e.getAttribute("data-finding")} ${e.querySelector(".line")!.textContent}`);
    expect(p0).toEqual(["V-05 L17", "V-06 L20", "V-07 L21", "V-09 L35", "U-01 L36"]);
    expect(fix("V-05").querySelector(".vr-diff-h .vr-mono")!.textContent).toBe("V-05 no-inlined-secrets · L17");
    expect(fix("V-05").querySelector(".vr-diff-h .vr-mono")!.getAttribute("title")).toBe("Line 17 of the partner's file · line 28 on the branch");
    unmount();
    engine();
    await mount(<ReviewPacketView packet={parsePacket((DRY2 as { packet: unknown }).packet)!} projectId="northgate-gateway" />);
    const where = (id: string) => $(`[data-testid="vr-packet-row"][data-finding="${id}"] .vr-where`)!.textContent;
    expect([where("V-05"), where("V-07"), where("U-01")]).toEqual(["L17", "L21", "L36"]);
    expect($('[data-testid="vr-packet-row"][data-finding="V-05"] .vr-c-rule')!.getAttribute("title")).toContain("line 28 on the branch");
  });
});

describe("#12 one count on the Work panel and the merge card", () => {
  it("both read 9 found + 1 guard → 6 open · 2 need you", async () => {
    const v = dry2();
    const c = funnelOf(v);
    expect(c).toMatchObject({ found: 9, guard: 1, open: 6, needYou: 2 });
    expect(countsWords(c)).toBe("9 found + 1 guard → 6 open · 2 need you");
    engine();
    await mount(<ValidationBuilt v={v} tests={null} later={[]} fixesHref="" testid="wp" />);
    expect($('[data-testid="wp-open"]')!.textContent).toBe("6");
    expect($('[data-testid="wp-guard"]')!.textContent).toBe("+ 1 guard");
    expect($('[data-testid="wp-you"]')!.textContent).toBe("2 need you");
    unmount();
    engine();
    await mount(<ReviewPacketView packet={v.packet!} projectId="northgate-gateway" />);
    expect($('[data-testid="vr-check-validator"]')!.textContent).toContain("9 found + 1 guard → 6 open · 2 need you");
    // The engine's own after_open (5) left the guard out: never shown when the findings are here.
    expect($('[data-testid="vr-check-validator"]')!.textContent).not.toContain("9 → 5 open");
  });

  it("a person's edit counts as fixed by a person, not by the agent", () => {
    const p = parsePacket((DRY2 as { packet: unknown }).packet)!;
    expect(validationCounts(p.findings)).toMatchObject({ agent: 3, human: 1 });
  });

  it("a ruleset propose says 'Ruleset v4 opened as change #15' with a link, beside the button", async () => {
    const calls = engine((c) => (c.method === "POST" ? { status: 200, body: { change: 15, version: 4, url: "/changes/northgate-gateway/15" } } : undefined));
    await mount(<RulesetEditor rs={parseRulesets([RULESET_NORTHGATE])[0]} projectId="northgate-gateway" />);
    await type($('[data-rule="cors-no-wildcard-with-credentials"] [data-testid="vr-rule-guide"]'), "Default to allow_credentials: false.");
    await press($('[data-testid="vr-ruleset-propose"]'));
    expect(posts(calls)).toHaveLength(1);
    expect($('[data-testid="vr-ruleset-opened"]')!.textContent).toContain("Ruleset v4 opened as change #15");
    expect($('[data-testid="vr-ruleset-opened-link"]')!.getAttribute("href")).toBe("/changes/northgate-gateway/15");
    const btn = $('[data-testid="vr-ruleset-propose"]') as HTMLButtonElement;
    expect(btn.textContent).toBe("Opened as change #15");
    expect(btn.disabled).toBe(true);
  });
});
