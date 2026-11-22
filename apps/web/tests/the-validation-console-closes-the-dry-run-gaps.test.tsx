// @vitest-environment happy-dom
/**
 * THE DRY RUN'S CONSOLE GAPS (Lumen Pay → Northgate, change #11), each pinned:
 *   #6  Reject opens a reason field on its row; Send waits for a reason; errors are said on the row.
 *   #7  Edit opens the WHOLE hunk (every line), the partner's lines beside it for reference.
 *   #10 One count: review follow-ups are a "review" line, never P1 "found"; the badge, the card,
 *       the Built column and the Work panel's run/build read the same packet.
 *   #11 A finding whose line the fix removed shows the partner's line, never "—".
 *   #13 One override per press: a press after it succeeded (after "Confirm it's you") sends nothing.
 *   #14 The change page's Merge follows the packet: blocked → the reasoned "Merge anyway".
 *   #15 No ruleset / validators.yaml: Create ruleset and Add validator, pre-filled; a 404 says
 *       it needs the latest BOSS and keeps the buttons disabled.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, settle, unmount } from "./_helpers/live-render";
import {
  blockingOpen, blockingRows, parseValidatorsFile, rulesetCreateBody, validatorsBody, lineWords, NEEDS_LATEST, packetTally, parsePacket, parseValidation, rulesetTemplate, validatorTemplate,
} from "../lib/validation-review";
import { VALIDATION_OPEN } from "../lib/validation-review-fixtures";
import { ValidationFlow, ValidationTab } from "../app/validation/validation-tab";
import { ReviewPacketView, packetHold } from "../app/validation/review-packet";
import { ValidationBuilt } from "../app/validation/built-summary";
import { CreateValidation } from "../app/validation/ruleset-editor";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

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
const fix = (id: string) => $(`[data-testid="vr-fix"][data-finding="${id}"]`)!;
const posts = (calls: Call[]) => calls.filter((c) => c.method === "POST");

// The dry run's packet: 9 gwlint findings (4 P0 fixed, P1 V-03/V-08 open) and 5 review notes
// BOSS deferred to a follow-up. The merge card used to read "7 P1 open · 9 found".
const v = (id: string, sev: string, status: string, line: number | null, partner: number) => ({
  id, source: { kind: "validator", name: "gwlint", rule_id: `rule-${id}` }, severity: sev, status, fix_kind: "agent", hunk_id: "h-1",
  location: { file: "config/tenants/lumen-pay.yaml", line, partner_line: partner }, original: "x", fixed: null,
});
const r = (id: string) => ({ id, source: { kind: "reviewer", name: "security seat" }, severity: "P1", status: "open", deferred_to: "queued",
  location: { file: "config/tenants/lumen-pay.yaml", line: 29 }, title: "the route trusts a staging issuer" });
const DRY_PACKET = {
  packet_version: 3, head_sha: "1c1e2c8",
  work: { title: "Validate tenant config with gwlint", workflow_run: "wfr_d80b276788ac4e06845f", run_no: 13, build: "crun_05b0b342cbc8456cbea8", later_builds: [], change: 11 },
  verdict: { state: "blocked", reasons: ["open_p1:V-03", "open_p1:V-08"] },
  // What the engine counted on the card: the review notes inside P1.
  counts: { P0: { found: 4, open: 0, waived: 0 }, P1: { found: 9, open: 7, waived: 0 }, P2: { found: 1, open: 0, waived: 0 }, P3: { found: 0, open: 0, waived: 0 }, unmapped: 0 },
  findings: [v("V-03", "P1", "open", 7, 7), v("V-08", "P1", "open", 21, 21), v("V-05", "P0", "fixed", null, 17), v("V-06", "P0", "fixed", null, 20),
             v("V-07", "P0", "fixed", null, 21), v("V-09", "P0", "fixed", 37, 35), v("V-01", "P1", "fixed", 9, 1), v("V-04", "P1", "fixed", 19, 7),
             v("V-02", "P2", "fixed", 13, 5), r("R-1"), r("R-2"), r("R-3"), r("R-4"), r("R-5")],
};

describe("#6 Reject asks why on its row, and every error is said beside the row", () => {
  it("Reject opens a reason field; Send waits, with a tooltip, until a reason is typed", async () => {
    const calls = engine();
    await mount(<ValidationTab projectId="northgate-gateway" change={9} />);
    await press(fix("V-06").querySelector('[data-testid="vr-reject"]'));
    expect(posts(calls)).toHaveLength(0);
    const send = fix("V-06").querySelector('[data-testid="vr-reject-send"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect(send.getAttribute("title")).toBe("Write why first");
    await type(fix("V-06").querySelector('[data-testid="vr-reject-reason"]'), "Partner named one dashboard origin");
    expect(send.disabled).toBe(false);
    await press(send);
    expect(posts(calls)[0]).toMatchObject({ path: "/v1/changes/northgate-gateway/9/findings/V-06/override",
      body: { action: "reject", reason: "Partner named one dashboard origin", head_sha: "eaff448" } });
  });

  it("an engine refusal is said in full on that row, never as a small line at the top", async () => {
    engine((c) => (c.method === "POST" ? { status: 422, body: { error: "reason_required", message: "Say why." } } : undefined));
    await mount(<ValidationTab projectId="northgate-gateway" change={9} />);
    await press(fix("V-02").querySelector('[data-testid="vr-waive"]'));
    await type(fix("V-02").querySelector('[data-testid="vr-waive-reason"]'), "no PII in query params");
    await press(fix("V-02").querySelector('[data-testid="vr-waive-send"]'));
    const said = fix("V-02").querySelector('[data-testid="vr-row-said"]')!;
    expect(said.getAttribute("role")).toBe("alert");
    expect(said.textContent).toBe("Write why first. The reason is recorded on the change.");
    expect($('[data-testid="vr-said"]')).toBeNull();
    // The form stays open with what was typed, so the person can fix it there.
    expect(fix("V-02").querySelector('[data-testid="vr-waive-form"]')).not.toBeNull();
  });
});

describe("#7 Edit opens the whole hunk", () => {
  const line = (finding: string, before: string | null, after: string | null) =>
    ({ finding, rule: "cors-no-wildcard-with-credentials", file: "t.yaml", line: 18, before, after, kind: "agent", hunk_id: "h-06" });
  const withHunk = (after3: string) => ({ ...VALIDATION_OPEN, fixes: [
    line("V-06", "    cors:", null), line("V-06", "      allow_origins: [\"*\"]", null), line("V-06", "      allow_credentials: true", null),
    line("V-06", null, "    cors:"), line("V-06", null, "      allow_origins: [\"*\"]"), line("V-06", null, after3),
  ] });

  it("every line of the hunk is editable (allow_origins too), with the partner's lines for reference", async () => {
    const calls = engine(undefined, withHunk("      allow_credentials: false"));
    await mount(<ValidationTab projectId="northgate-gateway" change={9} />);
    await press(fix("V-06").querySelector('[data-testid="vr-edit"]'));
    const box = fix("V-06").querySelector('[data-testid="vr-edit-text"]') as HTMLTextAreaElement;
    expect(box.tagName).toBe("TEXTAREA");
    expect(box.value).toBe("    cors:\n      allow_origins: [\"*\"]\n      allow_credentials: false");
    expect(fix("V-06").querySelector('[data-testid="vr-edit-partner"]')!.textContent).toBe("    cors:\n      allow_origins: [\"*\"]\n      allow_credentials: true");
    const hunk = "    cors:\n      allow_origins: [\"https://dashboard.lumenpay.example\"]\n      allow_credentials: true";
    await type(box, hunk);
    await type(fix("V-06").querySelector('[data-testid="vr-edit-reason"]'), "Dashboard needs cookies");
    await press(fix("V-06").querySelector('[data-testid="vr-edit-save"]'));
    expect(posts(calls)[0].body).toMatchObject({ action: "edit", text: hunk, reason: "Dashboard needs cookies" });
  });

  it("a masked value left in the lines is never committed: Save waits and says why", async () => {
    engine(undefined, withHunk("      allow_credentials: [SECRET]"));
    await mount(<ValidationTab projectId="northgate-gateway" change={9} />);
    await press(fix("V-06").querySelector('[data-testid="vr-edit"]'));
    await type(fix("V-06").querySelector('[data-testid="vr-edit-reason"]'), "why");
    const save = fix("V-06").querySelector('[data-testid="vr-edit-save"]') as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("title")).toBe("Replace the masked value first");
    expect(fix("V-06").querySelector('[data-testid="vr-edit-masked"]')).not.toBeNull();
  });
});

describe("#10 one count, review follow-ups on their own line", () => {
  it("packetTally: P1 is 4 found · 2 open, review 5 — whatever the engine's counts said", () => {
    const t = packetTally(parsePacket(DRY_PACKET)!);
    expect(t.P1).toMatchObject({ found: 4, open: 2 });
    expect(t.P0).toMatchObject({ found: 4, open: 0, fixed: 4 });
    expect(t.review).toEqual({ found: 5, open: 5, deferred: 5, blocking: 0 });
    expect(blockingOpen(t)).toBe(2);
    // The engine's counts.review (and no findings) is read as-is.
    const thin = parsePacket({ verdict: { state: "blocked", reasons: [] }, counts: { P1: { found: 4, open: 2 }, review: { found: 5, open: 5, deferred: 4, blocking: 1 } } })!;
    expect(packetTally(thin).review).toEqual({ found: 5, open: 5, deferred: 4, blocking: 1 });
    expect(blockingOpen(packetTally(thin))).toBe(3);
  });

  it("a security review finding on a P0 line blocks: counted in review, in the badge, the row and Merge anyway", async () => {
    const p = parsePacket({ ...DRY_PACKET, verdict: { state: "blocked", reasons: ["open_p1:V-03", "open_p1:V-08", "security_review:R-9"] },
      findings: [...DRY_PACKET.findings, { id: "R-9", source: { kind: "reviewer", name: "security_check" }, severity: "P1", status: "open", blocks: true,
        location: { file: "config/tenants/lumen-pay.yaml", line: 29 }, title: "jwks_url points at the staging mock" }] })!;
    const t = packetTally(p);
    expect(t.P1.found).toBe(4);
    expect(t.review).toMatchObject({ found: 6, blocking: 1 });
    expect(blockingOpen(t)).toBe(3);
    expect(blockingRows(p)).toContain("P1 R-9 security_check · L29");
    expect(blockingRows(p).some((r) => r.includes("R-1"))).toBe(false);
    await mount(<ReviewPacketView packet={p} projectId="northgate-gateway" />);
    expect($('[data-finding="R-9"]')!.textContent).toContain("blocks merge");
    expect($('[data-finding="R-1"]')!.textContent).toContain("deferred");
    expect($('[data-testid="vr-verdict"]')!.textContent).toContain("2 P1 · 1 security review open");
  });

  it("the engine's deferred_to {by, number, href} links the follow-up change", () => {
    const f = parsePacket({ verdict: { state: "ready" }, findings: [{ ...r("R-1"), deferred_to: { by: "queued", number: 12, href: "/changes/northgate-gateway/12" } }] })!.findings[0];
    expect(f.followup).toEqual({ change: 12, href: "/changes/northgate-gateway/12", build: "" });
  });

  it("the merge card says 2 P1 open, and shows review as its own cell", async () => {
    await mount(<ReviewPacketView packet={parsePacket(DRY_PACKET)!} projectId="northgate-gateway" />);
    expect($('[data-testid="vr-verdict"]')!.textContent).toContain("2 P1 open");
    expect($('[data-testid="vr-sev-P1"]')!.textContent).toContain("4 found");
    expect($('[data-testid="vr-sev-P1"]')!.textContent).not.toContain("9 found");
    expect($('[data-testid="vr-sev-review"]')!.textContent).toContain("5 follow-ups · not blocking");
    // The open rows that block come first; the review follow-ups after them.
    const order = $$('[data-testid="vr-packet-row"]').map((e) => e.getAttribute("data-finding"));
    expect(order.indexOf("V-03")).toBeLessThan(order.indexOf("R-1"));
    // The run and build links are the packet's.
    expect($('[data-testid="vr-prov-run"]')!.textContent).toContain("Run #13");
    expect($('[data-testid="vr-prov-run"]')!.getAttribute("href")).toContain("wfr_d80b276788ac4e06845f");
    expect($('[data-testid="vr-prov-build"]')!.getAttribute("href")).toContain("crun_05b0b342cbc8456cbea8");
  });

  it("the Built column reads the same packet: P1 4 found · 2 open, and a review line", async () => {
    const val = parseValidation({ ...VALIDATION_OPEN, packet: DRY_PACKET })!;
    await mount(<ValidationBuilt v={val} tests={null} later={[]} fixesHref="" testid="wp" />);
    expect($('[data-testid="wp-sev"]')!.textContent).toContain("4 found · 2 open");
    expect($('[data-testid="wp-review"]')!.textContent).toContain("5 follow-ups");
  });

  it("the Work panel's run and build come from the packet, not a later wrapper run", async () => {
    const { workLinksOf } = await import("../app/projects/work-panel");
    const val = parseValidation({ ...VALIDATION_OPEN, packet: DRY_PACKET })!;
    const w = { links: { workflow_run: "/?area=delivery_workflows&wfr=wfr_wrapper18", build: "/?area=code_tasks&build=crun_ba5eeb2405ef" }, run_number: 18,
                attempts: [], kind: "feature" } as unknown as Parameters<typeof workLinksOf>[0];
    const l = workLinksOf(w, { number: 18 } as Parameters<typeof workLinksOf>[1], val, "northgate-gateway");
    expect(l.runNo).toBe(13);
    expect(l.run).toContain("wfr_d80b276788ac4e06845f");
    expect(l.build!.id).toBe("crun_05b0b342cbc8456cbea8");
    // Without a packet, the work's own story.
    const own = workLinksOf(w, { number: 18 } as Parameters<typeof workLinksOf>[1], null, "northgate-gateway");
    expect(own.runNo).toBe(18);
    expect(own.build!.id).toBe("crun_ba5eeb2405ef");
  });
});

describe("#11 a removed line shows the partner's line", () => {
  it("lineWords falls back to partner_line; a P0 row never reads —", async () => {
    expect(lineWords({ location: { file: "", line: null, end_line: null, partner_line: 17 } })).toBe("L17");
    const val = parseValidation({ ...VALIDATION_OPEN, findings: DRY_PACKET.findings.filter((f) => f.id.startsWith("V-")) })!;
    engine();
    await mount(<ValidationFlow v={val} projectId="northgate-gateway" change={11} onChanged={() => {}} />);
    const p0 = $$('[data-testid="vr-grp-P0"] [data-testid="vr-finding"] .line').map((e) => e.textContent);
    // The partner's lines (dry run 2, gap 11): the upstream's branch line 37 is its partner line 35.
    expect(p0).toEqual(["L17", "L20", "L21", "L35"]);
    expect(p0).not.toContain("—");
    expect(fix("V-05").textContent).toContain("· L17");
  });
});

describe("#13 one override per press", () => {
  it("after a press succeeds, every control waits for the re-read: a second press sends nothing", async () => {
    const calls = engine();
    const val = parseValidation(VALIDATION_OPEN)!;
    await mount(<ValidationFlow v={val} projectId="northgate-gateway" change={9} onChanged={() => {}} />);
    const accept = fix("V-03").querySelector('[data-testid="vr-accept"]') as HTMLButtonElement;
    await act(async () => { accept.click(); accept.click(); });
    await settle();
    expect(posts(calls)).toHaveLength(1);
    // The step-up replay already sent it: the button the script pressed again is now held.
    const again = fix("V-03").querySelector('[data-testid="vr-accept"]') as HTMLButtonElement;
    expect(again.disabled).toBe(true);
    expect(again.getAttribute("title")).toBe("Re-checking after the last change");
    await press(again);
    await press(fix("V-01").querySelector('[data-testid="vr-accept"]'));
    expect(posts(calls)).toHaveLength(1);
  });

  it("Needs you: the agent's refused change is shown as a proposal, and Accept sends accept", async () => {
    const needs = { id: "V-77", source: { kind: "validator", name: "gwlint", rule_id: "no-sensitive-paths-exposed" }, severity: "P0", status: "open",
      fix_kind: "human", hunk_id: "", location: { file: "config/tenants/lumen-pay.yaml", line: 21, partner_line: 21 }, original: "  - path: /v1/admin/accounts", fixed: null,
      held: { autofix: "human", by: "ruleset" }, proposed: { before: "  - path: /v1/admin/accounts", after: null, applicable: true, pieces: [] } };
    const calls = engine(undefined, { ...VALIDATION_OPEN, findings: [...(VALIDATION_OPEN as { findings: unknown[] }).findings, needs] });
    await mount(<ValidationTab projectId="northgate-gateway" change={9} />);
    expect(fix("V-77").querySelector('[data-testid="vr-proposed-lines"]')!.textContent).toContain("- path: /v1/admin/accounts");
    await press(fix("V-77").querySelector('[data-testid="vr-accept"]'));
    expect(posts(calls)[0]).toMatchObject({ path: "/v1/changes/northgate-gateway/9/findings/V-77/override", body: { action: "accept" } });
  });

  it("an accepted fix shows 'accepted', not a second Accept", async () => {
    engine(undefined, { ...VALIDATION_OPEN, overrides: [{ ref: "O-1", action: "accept", finding_id: "V-03", actor: "Owner" }] });
    await mount(<ValidationTab projectId="northgate-gateway" change={9} />);
    expect(fix("V-03").querySelector('[data-testid="vr-accept"]')).toBeNull();
    expect(fix("V-03").querySelector('[data-testid="vr-controls"]')!.textContent).toContain("accepted");
  });
});

describe("#14 the change page's Merge follows the packet", () => {
  async function decide(packetJson: unknown) {
    const { Decide } = await import("../app/changes/[projectId]/[number]/change-set-view");
    const { parseChangeSet } = await import("../lib/repo-links");
    const { mountAs, stubFetch } = await import("./_helpers/multirepo-fixtures");
    const packet = parsePacket(packetJson);
    const hold = packetHold(packet);
    const cs = parseChangeSet({ number: 11, state: "open", source: "local", base_branch: "main",
      merge: { how: "approve", decision_id: "dec_m1", ...(hold ? { held: hold } : {}) }, decision: { id: "dec_m1", status: "pending", question: "Merge?" } });
    const calls = stubFetch(() => ({ status: 200, body: { ok: true } }));
    await mountAs(<Decide cs={cs} projectId="northgate-gateway" number="11" reread={async () => cs} packet={packet} compact />);
    return calls;
  }
  const btn = () => document.querySelector('[data-testid="change-set-merge"]') as HTMLButtonElement;

  it("blocked: Merge opens the reasoned Merge anyway, listing the open rows; it sends the reason as a forced merge", async () => {
    const calls = await decide(DRY_PACKET);
    expect(btn().disabled).toBe(false);
    expect(btn().textContent).toContain("Merge anyway");
    await press(btn());
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
    expect(document.querySelector('[data-testid="change-set-blocked-rows"]')!.textContent).toContain("P1 V-03");
    expect(document.querySelector('[data-testid="change-set-blocked-rows"]')!.textContent).not.toContain("R-1");
    const send = document.querySelector('[data-testid="change-set-merge-anyway-send"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    await type(document.querySelector('[data-testid="change-set-merge-reason"]'), "Partner launch; V-03 tracked in a follow-up");
    await press(send);
    const post = calls.filter((c) => c.method === "POST");
    expect(post).toHaveLength(1);
    expect(post[0].path).toBe("/v1/projects/northgate-gateway/local-changes/11/merge");
    expect(post[0].body).toEqual({ note: "Partner launch; V-03 tracked in a follow-up", force: true });
  });

  it("stale: the checks ran on an older commit than the head — held, never Merge anyway", () => {
    const p = parsePacket({ ...DRY_PACKET, validators: [{ id: "gwlint", before: 9, after_open: 2 }] })!;
    expect(packetHold(p, false, "25e7f99aaa")!.state).toBe("packet_stale");
    expect(packetHold(p, false, "25e7f99aaa")!.reason).toContain("1c1e2c8");
    expect(packetHold(p, false, "1c1e2c8")).toBeNull();
    expect(packetHold(parsePacket({ ...DRY_PACKET, verdict: { state: "blocked", reasons: ["stale_evidence"] } }))!.label).toBe("Re-checking…");
  });

  it("invalid or re-checking: disabled, with the reason", async () => {
    await decide({ ...DRY_PACKET, verdict: { state: "invalid", reasons: ["rule3:counts:mismatch"] } });
    expect(btn().disabled).toBe(true);
    expect(btn().getAttribute("title")).toContain("Packet invalid");
    unmount();
    await decide({ ...DRY_PACKET, verdict: { state: "blocked", reasons: ["rerun_pending"] }, rerun: { id: "vrr_1", state: "pending" } });
    expect(btn().disabled).toBe(true);
    expect(btn().textContent).toContain("Re-checking");
  });
});

describe("#15 a repo with no ruleset or validators.yaml", () => {
  it("templates: the gwlint validator for config/tenants/*.yaml, and a P0/P1/P2 ruleset; the bodies create", () => {
    expect(validatorTemplate("northgate")).toMatchObject({ id: "gwlint", adapter: "gwlint-json", applies_to: ["config/tenants/*.yaml"], ruleset: "northgate" });
    expect(rulesetTemplate().find((x) => x.id === "no-inlined-secrets")).toMatchObject({ severity: "P0", autofix: "never" });
    expect(rulesetCreateBody(rulesetTemplate(), "gwlint")).toMatchObject({ base_version: null, base_sha: null, validator: "gwlint" });
    expect(validatorsBody(validatorTemplate("northgate"), null)).toEqual({ base_sha: null, validators: [{ id: "gwlint", applies_to: ["config/tenants/*.yaml"],
      run: "go run ./cmd/gwlint validate {file} --json", fix: "go run ./cmd/gwlint --fix {file}", output: { adapter: "gwlint-json" },
      rules_doc: "rules/api-guidelines.yaml", ruleset: "northgate" }] });
    expect(parseValidatorsFile({ exists: false, sha: null, validators: [] })).toEqual({ exists: false, sha: null, path: ".boss/validators.yaml", ids: [] });
    expect(parseValidatorsFile({ detail: "Not Found" })).toBeNull();
  });

  it("Create ruleset and Add validator each open a pre-filled form and call their propose route", async () => {
    const calls = engine((c) => (c.method === "POST" ? { status: 200, body: { change: 12 } } : undefined));
    await mount(<CreateValidation projectId="northgate-gateway" hasRuleset={false} />);
    await press($('[data-testid="vr-create-ruleset"]'));
    expect($$('[data-testid="vr-create-rules"] tbody tr').length).toBe(9);
    await press($('[data-testid="vr-create-send"]'));
    expect(posts(calls)[0]).toMatchObject({ path: "/v1/projects/northgate-gateway/rulesets/northgate-gateway/propose",
      body: { base_sha: null, base_version: null, validator: "gwlint" } });
    expect((posts(calls)[0].body!.rules as unknown[]).length).toBe(9);
    expect($('[data-testid="vr-create-said"]')!.textContent).toContain("change #12");
    await press($('[data-testid="vr-create-validator"]'));
    expect(($('[data-testid="vr-create-v-applies"]') as HTMLInputElement).value).toBe("config/tenants/*.yaml");
    expect(($('[data-testid="vr-create-v-adapter"]') as HTMLSelectElement).value).toBe("gwlint-json");
    await press($('[data-testid="vr-create-send"]'));
    expect(posts(calls)[1]).toMatchObject({ path: "/v1/projects/northgate-gateway/validators/propose",
      body: { base_sha: null, validators: [{ id: "gwlint", output: { adapter: "gwlint-json" }, applies_to: ["config/tenants/*.yaml"], ruleset: "northgate-gateway" }] } });
  });

  it("a validators.yaml that already declares one: only Create ruleset is offered", async () => {
    engine();
    await mount(<CreateValidation projectId="northgate-gateway" hasRuleset={false} hasValidators />);
    expect($('[data-testid="vr-create-ruleset"]')).not.toBeNull();
    expect($('[data-testid="vr-create-validator"]')).toBeNull();
  });

  it("an engine whose route answers 404: 'needs the latest BOSS', and the buttons stay disabled", async () => {
    engine((c) => (c.method === "POST" ? { status: 404, body: { error: "no_ruleset", message: "There is no ruleset northgate-gateway." } } : undefined));
    await mount(<CreateValidation projectId="northgate-gateway" hasRuleset={false} />);
    await press($('[data-testid="vr-create-ruleset"]'));
    await press($('[data-testid="vr-create-send"]'));
    expect($('[data-testid="vr-create-said"]')!.textContent).toBe(NEEDS_LATEST);
    for (const id of ["vr-create-ruleset", "vr-create-validator", "vr-create-send"]) {
      const b = $(`[data-testid="${id}"]`) as HTMLButtonElement;
      expect(b.disabled).toBe(true);
    }
    expect(($('[data-testid="vr-create-validator"]') as HTMLButtonElement).getAttribute("title")).toBe(NEEDS_LATEST);
  });
});
