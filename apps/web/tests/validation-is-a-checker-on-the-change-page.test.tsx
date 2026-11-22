// @vitest-environment happy-dom
/**
 * VALIDATION IS A CHECKER (docs/design/workflow-library/DESIGN.md decision 4, wireframe screen 2).
 *
 *   Checks         one row per checker — Tests, gwlint, a security scan, the reviewer — reading
 *                  "9 found → 5 open (4 tool · 4 agent · 1 you)"; a row opens to its findings,
 *                  P0 first, each linking to its line in Files changed.
 *   Files changed  every finding is a note pinned on its line: severity · rule · "fixed by tool",
 *                  "fixed by agent", "fixed by you" or "Needs you" with the proposed fix, and the
 *                  §3 actions Accept · Revert line · Edit · Remove lines · Waive (single-flight,
 *                  step-up safe). The target's header carries "Revert to original".
 *   The tab        there is no Validation tab: ?tab=validation opens Checks.
 *   Merge card     one verdict line and a link to Checks; invalid still lists its problems.
 *
 * Ported from the retired Validation tab's tests (gwlint, dry run 1 and dry run 2 gaps): the
 * override calls, reasons, approvers, the remove fallback, proposal_gone and one press per send.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, settle, unmount } from "./_helpers/live-render";
import DRY2 from "./_helpers/dryrun2-validation.json";
import { NotesFromEngine, NotesOn } from "./_helpers/line-notes-harness";
import { parsePacket, parseValidation } from "../lib/validation-review";
import { PACKET_INVALID, VALIDATION_OPEN } from "../lib/validation-review-fixtures";
import { checkerCount, checkerWords, isOriginalCommit, noteWords, originalCommit, placeNotes } from "../lib/line-notes";
import { parseUnifiedDiff } from "../lib/diff-view";
import { parseChangeSet } from "../lib/repo-links";
import { rangeLabel } from "../lib/change-review";
import { CheckRows } from "../app/validation/checks-rows";
import { ReviewPacketView } from "../app/validation/review-packet";
import { primePeople } from "../app/who";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
beforeEach(() => { primePeople({ usr_ownerfixture0001: "Priya Raman" }); });
afterEach(() => { unmount(); vi.unstubAllGlobals(); primePeople(null); });

type Call = { method: string; path: string; body: Record<string, unknown> | null };
type Reply = { status: number; body: unknown } | undefined;
function engine(reply: (c: Call) => Reply = () => undefined, validation: unknown = VALIDATION_OPEN) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    const call = { method: init?.method ?? "GET", path, body: init?.body ? JSON.parse(String(init.body)) : null };
    calls.push(call);
    const r = reply(call);
    if (r) return new Response(JSON.stringify(r.body), { status: r.status, headers: { "content-type": "application/json" } });
    if (path.endsWith("/validation") && call.method === "GET") return new Response(JSON.stringify(validation), { status: 200 });
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
const note = (id: string) => $(`[data-testid="vr-fix"][data-finding="${id}"]`)!;
const posts = (calls: Call[]) => calls.filter((c) => c.method === "POST");
const OPEN = () => parseValidation(VALIDATION_OPEN)!;
function dry2(proposal: Record<string, unknown> | null = { before: "  - path: /v1/admin/accounts", after: "  - path: /v1/payments/refunds", applicable: true }) {
  const raw = JSON.parse(JSON.stringify(DRY2)) as { validation: { findings: Record<string, unknown>[] } };
  raw.validation.findings = raw.validation.findings.map((f) => (f.id === "V-07" ? { ...f, proposed: proposal } : f));
  return parseValidation(raw)!;
}

describe("Checks: one row per checker", () => {
  const cs = parseChangeSet({ number: 9, state: "open", source: "local", base_branch: "main", checks: { tests_passed: "true", run_status: "completed", where: "BOSS's sandbox" } });

  it("Tests, gwlint 9 found → 5 open (4 tool · 4 agent · 1 you) opening to its summary, contract tests per case", async () => {
    const jumps: unknown[] = [];
    const v = parseValidation({ ...VALIDATION_OPEN, overrides: [{ ref: "O-1", action: "waive", finding_id: "V-02", actor: "usr_ownerfixture0001",
      reason: "no PII in query params", created_at: "2026-11-21T14:23:00Z", rerun_id: "E-4" }] })!;
    const packet = parsePacket({ verdict: { state: "blocked", reasons: [] }, findings: [], risks: [{ text: "admin route still routed", refs: ["V-07"] }] });
    await mount(<CheckRows cs={cs} v={v} packet={packet} projectId="northgate-gateway" onJump={(...a) => jumps.push(a)} />);
    expect($('[data-testid="ck-tests"]')!.textContent).toContain("passed");
    const gw = $('[data-testid="ck-checker"][data-checker="gwlint"]')!;
    expect(gw.querySelector('[data-testid="ck-words"]')!.textContent).toBe("9 found → 5 open (4 tool · 4 agent · 1 you)");
    expect(gw.hasAttribute("open")).toBe(true);
    // The summary: funnel + exit, severity bar, P0/P1 table (original → now), overrides, risks, Rules.
    expect(gw.querySelector('[data-testid="ck-funnel"]')!.textContent).toBe("9 found → 4 tool · 4 agent · 1 you → 5 open · exit 1Rules");
    expect(gw.querySelector('[data-testid="vr-sev-P0"]')!.textContent).toContain("3 open");
    // The run behind the count (wireframe B): the validator's command and exit.
    expect(gw.querySelector('[data-testid="ck-run-before"]')!.textContent).toContain("$ ");
    const rows = [...gw.querySelectorAll('[data-testid^="ck-table"] [data-testid="vr-packet-row"]')].map((e) => e.getAttribute("data-finding"));
    expect(rows.slice(0, 3)).toEqual(["V-03", "V-06", "V-07"]);                     // P0 first, open before fixed
    expect(gw.querySelector('[data-finding="V-09"] [data-testid="vr-packet-now"]')!.textContent).toContain("https://lumen-pay-core");
    expect(gw.querySelector('[data-testid="vr-p23"]')!.textContent).toContain("P2 / P3 · 1");
    expect(gw.querySelector('[data-testid="vr-override-row"]')!.textContent).toContain("O-1Priya Ramanwaived V-02");
    expect(gw.querySelector('[data-testid="vr-override-row"]')!.textContent).toContain("re-run E-4");
    expect(gw.querySelector('[data-testid="vr-risks"]')!.textContent).toContain("admin route still routed");
    expect(gw.querySelector('[data-testid="ck-rules"]')!.getAttribute("href")).toBe("/?project_id=northgate-gateway&area=project&tab=settings&section=validators");
    await press(gw.querySelector('[data-finding="V-03"] [data-testid="vr-row-line"]'));
    expect(jumps).toEqual([["config/tenants/lumen-pay.yaml", 17, "new"]]);
    const contract = $('[data-testid="ck-contract"]')!;
    expect(contract.textContent).toContain("3 / 3 pass");
    expect([...contract.querySelectorAll('[data-testid="ck-case"]')].at(-1)!.textContent).toBe("payments_rate_limited_11th429429V-04");
  });

  it("a reviewer and a security scan are rows of their own; a validator that found nothing reads 0 found", async () => {
    const packet = parsePacket({ verdict: { state: "blocked", reasons: [] }, validators: [{ id: "gwlint", before: 0, after_open: 0 }], findings: [
      { id: "R-1", source: { kind: "reviewer", name: "security seat" }, severity: "P1", status: "open", location: { file: "a.go", line: 3 }, title: "staging issuer" },
      { id: "S-1", source: { kind: "security", name: "npm audit" }, severity: "P1", status: "open", fix_kind: "human", location: { file: "package-lock.json", line: 9 }, title: "yaml 1.10.0" },
    ] })!;
    await mount(<CheckRows cs={cs} v={null} packet={packet} onJump={() => {}} />);
    const names = $$('[data-testid="ck-checker"]').map((e) => e.querySelector("b")!.textContent);
    expect(names).toEqual(["gwlint", "Reviewer", "Security scan"]);
    expect($$('[data-testid="ck-checker"]')[0].textContent).toContain("0 found");
    expect($('[data-checker="Security scan"]')!.textContent).toContain("1 found → 1 open (1 you)");
  });

  it("no validation on the engine (404): the other checks only, never a crash or an id", async () => {
    await mount(<CheckRows cs={cs} v={null} packet={null} onJump={() => {}} />);
    expect($('[data-testid="ck-checker"]')).toBeNull();
    expect($('[data-testid="ck-tests"]')).not.toBeNull();
  });

  it("the words: who closed each finding, or who must", () => {
    const v = OPEN();
    expect(checkerWords(checkerCount(v.findings))).toBe("9 found → 5 open (4 tool · 4 agent · 1 you)");
    expect(checkerWords(checkerCount([]))).toBe("0 found");
    const by = (id: string) => noteWords(v.findings.find((f) => f.id === id)!).text;
    expect([by("V-04"), by("V-07"), by("V-01")]).toEqual(["fixed by tool", "Needs you", "Open"]);
    const edited = { ...v.findings[5], status: "fixed" as const, override: { ...v.findings[5], id: "O-1", action: "edit" } as never };
    expect(noteWords(edited).text).toBe("fixed by you");
  });
});

describe("what the retired tab also said", () => {
  it("a guard, a P0 waive waiting on its approver, and a pending re-check that holds every action", async () => {
    const v = parseValidation({
      validation: { ...VALIDATION_OPEN, guard: { code: "input_substituted", source: "examples/lumen-pay.good.yaml", why: "same bytes" } },
      overrides: [{ ref: "O-1", id: "ovr_abc", action: "waive", finding_id: "V-07", actor: "usr_owner01abc", approver: "usr_reviewer9xyz" }],
      rerun: { id: "vrr_1", state: "running" }, head_sha: "9f2c1d0",
    })!;
    await mount(<CheckRows cs={parseChangeSet({ number: 9, state: "open", source: "local" })} v={v} packet={null} onJump={() => {}} />);
    expect($('[data-testid="vr-guard"]')!.textContent).toContain("input_substituted");
    expect($('[data-testid="vr-guard"]')!.textContent).toContain("examples/lumen-pay.good.yaml");
    expect($('[data-checker="gwlint"]')!.textContent).toContain("re-checking…");
    unmount();
    engine();
    await mount(<NotesOn v={v} />);
    expect(note("V-07").querySelector('[data-testid="vr-note-by"]')!.textContent).toBe("Waive waits for approval");
    expect(note("V-07").textContent).toContain("to approve");
    expect(note("V-07").textContent).not.toMatch(/usr_/);
    expect((note("V-09").querySelector('[data-testid="vr-accept"]') as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("Files changed: each finding is a note on its line", () => {
  it("every finding sits under its own line; one with no row in this diff is shown at the top of the file", () => {
    const v = OPEN();
    v.findings = v.findings.map((f) => (f.id === "V-03" ? { ...f, location: { ...f.location, line: null } } : f));
    const file = parseUnifiedDiff([
      "diff --git a/config/tenants/lumen-pay.yaml b/config/tenants/lumen-pay.yaml", "--- a/config/tenants/lumen-pay.yaml", "+++ b/config/tenants/lumen-pay.yaml",
      "@@ -16,3 +16,2 @@", "     upstream_auth:", "-      api_key: \"x\"", "     cors:", "@@ -35,1 +34,1 @@", "-    url: http", "+    url: https"].join("\n"))[0];
    const { at, loose } = placeNotes(v.findings, file, v.target);
    expect(at.get("old:17")!.map((f) => f.id)).toEqual(["V-03"]);   // a line the fix removed: the original's side
    expect(at.get("old:35")!.map((f) => f.id)).toEqual(["V-09"]);
    expect(at.get("new:16")).toBeUndefined();
    expect(loose.map((f) => f.id)).toContain("V-07");
  });

  it("pins severity · rule · who, the proposed removal of a Needs-you finding, and its actions", async () => {
    engine();
    await mount(<NotesOn v={OPEN()} />);
    expect($$('[data-testid="change-set-pinned"] [data-testid="vr-fix"]')).toHaveLength(9);
    const v07 = note("V-07");
    expect(v07.querySelector(".vr-sev")!.textContent).toBe("P0");
    expect(v07.querySelector(".ln-rule")!.textContent).toBe("no-sensitive-paths-exposed");
    expect(v07.querySelector('[data-testid="vr-note-by"]')!.textContent).toBe("Needs you");
    expect(v07.querySelector('[data-testid="vr-proposed-remove"]')!.textContent).toContain("remove lines 21–33 of the original file");
    const acts = [...v07.querySelectorAll('[data-testid="vr-controls"] button')].map((b) => b.textContent);
    expect(acts).toEqual(["Accept", "Revert line", "Edit", "Remove lines", "Waive"]);
    expect(note("V-04").querySelector('[data-testid="vr-note-by"]')!.textContent).toBe("fixed by tool");
    // Each note carries its finding's diff and the ruleset's guide.
    expect(note("V-09").querySelector('[data-testid="vr-diff-lines"]')!.textContent).toBe('-     url: "http://lumen-pay-core.internal.example"+     url: "https://lumen-pay-core.internal.example"');
    expect(note("V-03").querySelector('[data-testid="vr-guide"]')!.textContent).toMatch(/^guide v3: Delete the key/);
    expect([...note("V-09").querySelectorAll('[data-testid="vr-controls"] button')].map((b) => b.textContent)).toEqual(["Accept", "Revert line", "Edit"]);
    // Neutral words: the original file, never the partner's.
    expect(document.body.textContent).not.toMatch(/partner/i);
    expect(document.body.textContent).not.toContain("sk_live_4");
  });

  it("Accept, Revert line, Edit and Waive each send the §3 override with the head sha; Revert line asks why", async () => {
    const calls = engine();
    await mount(<NotesFromEngine />);
    await press(note("V-03").querySelector('[data-testid="vr-accept"]'));
    expect(posts(calls)[0]).toMatchObject({ path: "/v1/changes/northgate-gateway/9/findings/V-03/override", body: { action: "accept", head_sha: "eaff448" } });
    await settle();
    await press(note("V-06").querySelector('[data-testid="vr-reject"]'));
    expect(posts(calls)).toHaveLength(1);
    const send = note("V-06").querySelector('[data-testid="vr-reject-send"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect(send.getAttribute("title")).toBe("Write why first");
    await type(note("V-06").querySelector('[data-testid="vr-reject-reason"]'), "Partner named one origin");
    await press(send);
    expect(posts(calls)[1].body).toMatchObject({ action: "reject", reason: "Partner named one origin", head_sha: "eaff448" });
    await press(note("V-06").querySelector('[data-testid="vr-edit"]'));
    await type(note("V-06").querySelector('[data-testid="vr-edit-text"]'), '      allow_origins: ["https://dashboard.lumenpay.example"]');
    await type(note("V-06").querySelector('[data-testid="vr-edit-reason"]'), "Dashboard needs cookies");
    await press(note("V-06").querySelector('[data-testid="vr-edit-save"]'));
    expect(posts(calls)[2].body).toMatchObject({ action: "edit", text: '      allow_origins: ["https://dashboard.lumenpay.example"]', reason: "Dashboard needs cookies" });
    await press(note("V-02").querySelector('[data-testid="vr-waive"]'));
    expect((note("V-02").querySelector('[data-testid="vr-waive-send"]') as HTMLButtonElement).disabled).toBe(true);
    await type(note("V-02").querySelector('[data-testid="vr-waive-reason"]'), "no PII in query params, KAN-12");
    await press(note("V-02").querySelector('[data-testid="vr-waive-send"]'));
    expect(posts(calls)[3].body).toMatchObject({ action: "waive", reason: "no PII in query params, KAN-12" });
    expect(posts(calls)[3].body).not.toHaveProperty("approver_id");
  });

  it("a P0 waive asks for an approver; Revert to original on the file header asks why", async () => {
    const calls = engine();
    await mount(<NotesOn v={OPEN()} originalCommits={2} />);
    await press(note("V-07").querySelector('[data-testid="vr-waive"]'));
    await type(note("V-07").querySelector('[data-testid="vr-waive-reason"]'), "Needed for a week");
    const send = note("V-07").querySelector('[data-testid="vr-waive-send"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    await type(note("V-07").querySelector('[data-testid="vr-waive-approver"]'), "security-lead");
    expect(send.textContent).toBe("Ask security-lead");
    await press(send);
    expect(posts(calls)[0].body).toMatchObject({ action: "waive", approver_id: "security-lead" });
    unmount();
    const again = engine();
    await mount(<NotesOn v={OPEN()} originalCommits={2} />);
    const head = $('[data-testid="harness-head"]')!;
    expect(head.querySelector('[data-testid="vr-original-first"]')!.textContent).toBe("2 commits: original as given → fixes");
    await press(head.querySelector('[data-testid="vr-revert-all"]'));
    await type(head.querySelector('[data-testid="vr-revert-all-reason"]'), "start over from the original");
    await press(head.querySelector('[data-testid="vr-revert-all-send"]'));
    expect(posts(again).at(-1)).toMatchObject({ path: "/v1/changes/northgate-gateway/9/validation/revert-all",
      body: { reason: "start over from the original", head_sha: "eaff448" } });
    expect(head.querySelector('[data-testid="vr-said"]')!.textContent).toBe("Reverted to the original file · re-checking now.");
  });

  it("an engine without the override routes: said on the note pressed, then every action disabled with the reason", async () => {
    engine((c) => (c.method === "POST" ? { status: 404, body: { detail: "Not Found" } } : undefined));
    await mount(<NotesOn v={OPEN()} />);
    await press(note("V-03").querySelector('[data-testid="vr-accept"]'));
    expect(note("V-03").querySelector('[data-testid="vr-row-said"]')!.textContent).toContain("not on this BOSS yet");
    const accept = note("V-09").querySelector('[data-testid="vr-accept"]') as HTMLButtonElement;
    expect(accept.disabled).toBe(true);
    expect(accept.getAttribute("title")).toContain("not deployed");
    expect(($('[data-testid="vr-revert-all"]') as HTMLButtonElement).disabled).toBe(true);
  });

  it("an engine refusal is said in full on that note; the form stays open with what was typed", async () => {
    engine((c) => (c.method === "POST" ? { status: 422, body: { error: "reason_required", message: "Say why." } } : undefined));
    await mount(<NotesOn v={OPEN()} />);
    await press(note("V-02").querySelector('[data-testid="vr-waive"]'));
    await type(note("V-02").querySelector('[data-testid="vr-waive-reason"]'), "no PII in query params");
    await press(note("V-02").querySelector('[data-testid="vr-waive-send"]'));
    const said = note("V-02").querySelector('[data-testid="vr-row-said"]')!;
    expect(said.getAttribute("role")).toBe("alert");
    expect(said.textContent).toBe("Write why first. The reason is recorded on the change.");
    expect(note("V-02").querySelector('[data-testid="vr-waive-form"]')).not.toBeNull();
  });

  it("one override per press: after a press succeeds every note waits for the re-read", async () => {
    const calls = engine();
    await mount(<NotesOn v={OPEN()} />);
    const accept = note("V-03").querySelector('[data-testid="vr-accept"]') as HTMLButtonElement;
    await act(async () => { accept.click(); accept.click(); });
    await settle();
    expect(posts(calls)).toHaveLength(1);
    const again = note("V-03").querySelector('[data-testid="vr-accept"]') as HTMLButtonElement;
    expect(again.disabled).toBe(true);
    expect(again.getAttribute("title")).toBe("Re-checking after the last change");
    await press(note("V-09").querySelector('[data-testid="vr-accept"]'));
    expect(posts(calls)).toHaveLength(1);
  });

  it("Edit opens the whole hunk with the original lines beside it; a masked value is never committed", async () => {
    const line = (before: string | null, after: string | null) =>
      ({ finding: "V-06", rule: "cors-no-wildcard-with-credentials", file: "t.yaml", line: 18, before, after, kind: "agent", hunk_id: "h-06" });
    const withHunk = (after3: string) => parseValidation({ ...VALIDATION_OPEN, fixes: [
      line("    cors:", null), line("      allow_origins: [\"*\"]", null), line("      allow_credentials: true", null),
      line(null, "    cors:"), line(null, "      allow_origins: [\"*\"]"), line(null, after3)] })!;
    const calls = engine();
    await mount(<NotesOn v={withHunk("      allow_credentials: false")} />);
    await press(note("V-06").querySelector('[data-testid="vr-edit"]'));
    const box = note("V-06").querySelector('[data-testid="vr-edit-text"]') as HTMLTextAreaElement;
    expect(box.value).toBe("    cors:\n      allow_origins: [\"*\"]\n      allow_credentials: false");
    expect(note("V-06").querySelector('[data-testid="vr-edit-original"]')!.textContent).toBe("    cors:\n      allow_origins: [\"*\"]\n      allow_credentials: true");
    await type(note("V-06").querySelector('[data-testid="vr-edit-reason"]'), "Dashboard needs cookies");
    await press(note("V-06").querySelector('[data-testid="vr-edit-save"]'));
    expect(posts(calls)[0].body).toMatchObject({ action: "edit", reason: "Dashboard needs cookies" });
    unmount();
    engine();
    await mount(<NotesOn v={withHunk("      allow_credentials: [SECRET]")} />);
    await press(note("V-06").querySelector('[data-testid="vr-edit"]'));
    await type(note("V-06").querySelector('[data-testid="vr-edit-reason"]'), "why");
    const save = note("V-06").querySelector('[data-testid="vr-edit-save"]') as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("title")).toBe("Replace the masked value first");
  });

  it("an accepted fix shows 'accepted', not a second Accept; an override names the person and what they did", async () => {
    engine();
    await mount(<NotesOn v={parseValidation({ ...VALIDATION_OPEN, overrides: [{ ref: "O-1", action: "accept", finding_id: "V-03", actor: "Owner" }] })!} />);
    expect(note("V-03").querySelector('[data-testid="vr-accept"]')).toBeNull();
    expect(note("V-03").querySelector('[data-testid="vr-controls"]')!.textContent).toContain("accepted");
    unmount();
    engine();
    await mount(<NotesOn v={dry2()} change={14} />);
    const rec = note("V-06").querySelector('[data-testid="vr-override-record"]')!;
    expect(rec.textContent).toMatch(/^O-1 · Priya Raman/);
    expect(rec.textContent).toContain("edited the lines");
  });
});

describe("Remove lines and the agent's proposal (dry run 2)", () => {
  it("the admin route's block (original lines 21–33) is shown; a reason is required; the engine's remove is sent", async () => {
    const calls = engine();
    await mount(<NotesOn v={dry2()} change={14} />);
    const card = note("V-07");
    expect(note("V-09").querySelector('[data-testid="vr-remove"]')).toBeNull();
    await press(card.querySelector('[data-testid="vr-remove"]'));
    const lines = [...card.querySelectorAll('[data-testid="vr-remove-lines"] span')].map((e) => e.textContent);
    expect(lines[0]).toBe("-   - path: /v1/admin/accounts");
    expect(lines).toHaveLength(13);
    expect(card.querySelector('[data-testid="vr-remove-where"]')!.textContent).toContain("lines 21–33 of the original file");
    expect(card.querySelector('[data-testid="vr-remove-where"]')!.getAttribute("title")).toBe("On the branch: lines 32–44 of config/tenants/lumen-pay.yaml");
    const send = card.querySelector('[data-testid="vr-remove-send"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect(send.textContent).toBe("Remove 13 lines");
    await type(card.querySelector('[data-testid="vr-remove-reason"]'), "Internal admin surface; never for tenants.");
    await press(send);
    expect(posts(calls)).toHaveLength(1);
    expect(posts(calls)[0]).toMatchObject({ path: "/v1/changes/northgate-gateway/14/findings/V-07/override",
      body: { action: "remove", reason: "Internal admin surface; never for tenants.", head_sha: "fa05ac2d6d71e40d29aa1160c5d2c3ade3656f5d" } });
    expect(card.querySelector('[data-testid="vr-row-said"]')!.textContent).toBe("V-07 removed · re-checking now.");
  });

  it("an engine that took the empty edit instead: the same press falls back to {action: edit, text: ''}", async () => {
    const calls = engine((c) => (c.method === "POST" && c.body?.action === "remove"
      ? { status: 422, body: { detail: [{ loc: ["body", "action"], msg: "Input should be 'accept', 'reject', 'edit', 'waive' or 'reopen'" }] } } : undefined));
    await mount(<NotesOn v={dry2()} change={14} />);
    await press(note("V-07").querySelector('[data-testid="vr-remove"]'));
    await type(note("V-07").querySelector('[data-testid="vr-remove-reason"]'), "Drop the admin route.");
    await press(note("V-07").querySelector('[data-testid="vr-remove-send"]'));
    expect(posts(calls).map((c) => c.body!.action)).toEqual(["remove", "edit"]);
    expect(note("V-07").querySelector('[data-testid="vr-row-said"]')!.textContent).toBe("V-07 removed · re-checking now.");
  });

  it("Accept on the agent's proposal shows only while it applies; a 409 proposal_gone hides it and says so once", async () => {
    const calls = engine((c) => (c.method === "POST" ? { status: 409, body: { error: "proposal_gone", message: "The lines this proposal changes are not on the branch." } } : undefined));
    await mount(<NotesOn v={dry2()} change={14} />);
    const card = note("V-07");
    expect(card.querySelector('[data-testid="vr-proposed-lines"]')!.textContent).toContain("- path: /v1/admin/accounts");
    await press(card.querySelector('[data-testid="vr-accept"]'));
    expect(posts(calls)[0].body).toMatchObject({ action: "accept" });
    expect(card.querySelector('[data-testid="vr-accept"]')).toBeNull();
    expect(card.querySelector('[data-testid="vr-proposal-gone"]')!.textContent).toContain("The agent's proposal is no longer valid");
    expect(card.querySelector('[data-testid="vr-row-said"]')).toBeNull();
    expect(card.querySelector('[data-testid="vr-edit"]')).not.toBeNull();
    expect(card.querySelector('[data-testid="vr-remove"]')).not.toBeNull();
    unmount();
    engine();
    await mount(<NotesOn v={dry2({ before: "x", after: "y", applicable: false })} change={14} />);
    expect(note("V-07").querySelector('[data-testid="vr-accept"]')).toBeNull();
  });
});

describe("fix a given file: original → fixed", () => {
  it("the engine's first commit is found by its words; the compare reads 'Original file … head'", () => {
    const commits = [{ sha: "b".repeat(40), message: "governed: implement" }, { sha: "a".repeat(40), message: "Original file as given: examples/x.yaml → config/x.yaml" }];
    expect(isOriginalCommit(commits[1].message)).toBe(true);
    expect(originalCommit(commits)!.sha).toBe("a".repeat(40));
    expect(originalCommit([commits[0]])).toBeNull();
    const full = commits.map((c) => ({ ...c, author: "", date: null, fixes: [], additions: null, deletions: null, files: null }));
    expect(rangeLabel({ base: "a".repeat(40), head: null }, "main", full)).toBe("Original file … bbbbbbb · latest");
  });
});

describe("the Validation tab is gone: ?tab=validation opens Checks", () => {
  it("tabOf maps the old tab; the page has no Validation tab and lands on Checks with gwlint's row", async () => {
    const { tabOf, ChangeSetView } = await import("../app/changes/[projectId]/[number]/change-set-view");
    const { identityFor } = await import("./_helpers/multirepo-fixtures");
    const { me, org } = identityFor("owner");
    expect(tabOf("validation")).toBe("checks");
    expect(tabOf("files")).toBe("files");
    expect(tabOf("nope")).toBeNull();
    window.history.replaceState(null, "", "/changes/northgate-gateway/9?tab=validation");
    engine((c) => (c.path.endsWith("/local-changes/9") ? { status: 200, body: {
      number: 9, title: "Lumen Pay tenant onboarding", state: "open", source: "local", base_branch: "main", branch: "boss/code/crun_1", repository: "local_northgate/northgate",
      checks: { tests_passed: "true", where: "BOSS's sandbox" }, files: [], diff: "", commits: [] } }
      : c.path.endsWith("/validation") ? undefined : c.method === "GET" ? { status: 404, body: { error: "not_found" } } : undefined));
    await mount(<ChangeSetView projectId="northgate-gateway" number="9" tab="validation" me={me} org={org} project={{ ...org, scope: "project:northgate-gateway" }} />);
    expect($('[data-testid="change-set-tab-validation"]')).toBeNull();
    expect($('[data-testid="change-set-tab-checks"]')!.getAttribute("aria-selected")).toBe("true");
    expect($('[data-testid="ck-checker"][data-checker="gwlint"]')!.textContent).toContain("9 found → 5 open");
    expect(window.location.search).toBe("?tab=checks");
  });
});

describe("the merge card: one verdict line and a link to Checks", () => {
  it("blocked: the verdict, the severity counts and Checks ›, never the findings table; invalid still lists its problems", async () => {
    const blocked = parsePacket({ work: { change: 9, title: "Lumen Pay" }, verdict: { state: "blocked", reasons: ["open_p0:V-07"] },
      findings: (VALIDATION_OPEN as { findings: unknown[] }).findings })!;
    await mount(<ReviewPacketView packet={blocked} projectId="northgate-gateway" compact />);
    expect($('[data-testid="vr-verdict"]')!.textContent).toContain("Blocked");
    expect($('[data-testid="vr-verdict"]')!.textContent).toContain("3 P0 · 1 P1 open");
    expect($('[data-testid="vr-checks-link"]')!.getAttribute("href")).toBe("/changes/northgate-gateway/9?tab=checks");
    expect($('[data-testid="vr-p01"]')).toBeNull();
    expect($('[data-testid="vr-sev-P0"]')!.textContent).toContain("3 open");
    expect($$('[data-testid="vr-ref"]')).toHaveLength(0);
    // Wireframe C's provenance line stays on the card.
    expect($('[data-testid="vr-provenance"]')).not.toBeNull();
    unmount();
    await mount(<ReviewPacketView packet={parsePacket(PACKET_INVALID)!} projectId="northgate-gateway" compact />);
    expect($('[data-testid="vr-verdict"]')!.textContent).toContain("Packet invalid");
    expect($('[data-testid="vr-invalid"]')).not.toBeNull();
  });
});
