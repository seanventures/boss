// @vitest-environment happy-dom
/**
 * GWLINT'S RULES, THE BUILT COLUMN AND THE ENGINE'S CONTRACT (validation-review SPEC §4–§5).
 *
 * Lumen Pay's file as gwlint saw it: 9 findings (4 P0 · 4 P1 · 1 P2). The Work panel's Built
 * column shows the funnel; the ruleset editor opens a change rather than editing a hidden
 * setting; GET …/validation's shape parses whole. The findings themselves are a checker on the
 * change page (tests/validation-is-a-checker-on-the-change-page.test.tsx).
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, settle, unmount } from "./_helpers/live-render";
import { parseRulesets, parseValidation } from "../lib/validation-review";
import { RULESET_NORTHGATE, VALIDATION_OPEN } from "../lib/validation-review-fixtures";
import { RulesetEditor } from "../app/validation/ruleset-editor";
import { ValidationBuilt } from "../app/validation/built-summary";
import { parseChangeSet } from "../lib/repo-links";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

type Call = { method: string; path: string; body: Record<string, unknown> | null };
function engine(status = 200, answer: Record<string, unknown> = { ok: true }) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    calls.push({ method: init?.method ?? "GET", path, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (path === "/v1/changes/northgate-gateway/9/validation" && (init?.method ?? "GET") === "GET") return new Response(JSON.stringify(VALIDATION_OPEN), { status: 200 });
    return new Response(JSON.stringify(status === 200 ? answer : { detail: "not found" }), { status, headers: { "content-type": "application/json" } });
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

describe("the Work panel's Built column", () => {
  it("shows the gwlint funnel, the P0/P1 chips and See every fix — never prose", async () => {
    await mount(<ValidationBuilt v={parseValidation(VALIDATION_OPEN)!} tests={{ passed: true, count: null, label: "go test ./... pass" }}
                                 later={[{ key: "a", line: "More tests (+172 test lines)" }]} fixesHref="/changes/northgate-gateway/9?tab=validation" testid="wp" />);
    expect($('[data-testid="wp-funnel"]')!.textContent).toBe("gwlint9found→4 auto→5open1 needs you");
    expect($('[data-testid="wp-sev"]')!.textContent).toContain("4 found · 3 open");
    expect($('[data-testid="wp-see-fixes"]')!.getAttribute("href")).toBe("/changes/northgate-gateway/9?tab=validation");
  });
});

describe("the change page asks for a workflow run only by its run id", () => {
  it("a build id in run_id is never sent to /v1/workflow-runs/", () => {
    expect(parseChangeSet({ number: 9, run_id: "crun_2bc5f2" }).workflow_run_id).toBeNull();
    expect(parseChangeSet({ number: 9, run_id: "crun_2bc5f2", workflow_run_id: "wfr_40229c5c" }).workflow_run_id).toBe("wfr_40229c5c");
  });
});

describe("the ruleset editor (§4)", () => {
  const rs = () => parseRulesets([RULESET_NORTHGATE])[0];

  it("shows each rule's level, auto-fix and guide; a disabled rule says its findings are suppressed", async () => {
    engine();
    await mount(<RulesetEditor rs={rs()} projectId="northgate-gateway" />);
    expect($$('[data-testid="vr-rule"]').length).toBe(9);
    expect($('[data-testid="vr-ruleset-version"]')!.textContent).toContain("v3 · a1b2c3d");
    expect($('[data-rule="public-route-no-auth-block"]')!.textContent).toContain("suppressed");
    expect($('[data-rule="lumen-no-refund-without-schema"]')!.textContent).toContain("agent-checked");
    expect(($('[data-testid="vr-ruleset-propose"]') as HTMLButtonElement).disabled).toBe(true);
  });

  it("editing a guide and saving opens a ruleset change with the new text", async () => {
    const calls = engine(200, { change: 11 });
    await mount(<RulesetEditor rs={rs()} projectId="northgate-gateway" />);
    const guide = $('[data-rule="cors-no-wildcard-with-credentials"] [data-testid="vr-rule-guide"]');
    await type(guide, "Default to allow_credentials: false. Keep credentials only if the partner named exact origins.");
    expect($('[data-rule="cors-no-wildcard-with-credentials"]')!.textContent).toContain("edited · ships in v4");
    await press($('[data-testid="vr-ruleset-propose"]'));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("/v1/projects/northgate-gateway/rulesets/northgate/propose");
    expect(post.body).toMatchObject({ base_version: 3, base_sha: "a1b2c3d" });
    const sent = (post.body!.rules as { id: string; guide: string }[]).find((r) => r.id === "cors-no-wildcard-with-credentials")!;
    expect(sent.guide).toBe("Default to allow_credentials: false. Keep credentials only if the partner named exact origins.");
    expect($('[data-testid="vr-ruleset-opened"]')!.textContent).toContain("opened as change #11");
  });

  it("lowering a P0 says it needs a second approver", async () => {
    engine();
    await mount(<RulesetEditor rs={rs()} projectId="northgate-gateway" />);
    const sel = $('[data-rule="no-inlined-secrets"] [data-testid="vr-rule-sev"]') as HTMLSelectElement;
    await act(async () => { sel.value = "P1"; sel.dispatchEvent(new Event("change", { bubbles: true })); });
    expect($('[data-testid="vr-ruleset-lock"]')!.textContent).toContain("no-inlined-secrets: lowering a P0 needs a second approver");
  });
});

describe("the engine's contract (PRs #276–#278)", () => {
  it("GET …/validation's {validation, overrides, rerun, head_sha, packet}: O-n labels, per-line fixes, guard", async () => {
    const { parseValidation } = await import("../lib/validation-review");
    const v = parseValidation({
      validation: { ...VALIDATION_OPEN, schema: "boss.validation/v1", evidence: [{ id: "E-1", kind: "command", cmd: "gwlint", exit: 1 }, { id: "E-3", kind: "command", cmd: "gwlint", exit: 1 }],
        before: { command: "gwlint validate", exit: 1, findings: ["V-01"] }, after: { command: "gwlint validate", exit: 1, findings: [] },
        fixes: [{ finding: "V-09", rule: "upstream-https-required", file: "t.yaml", line: 36, before: "url: http://x", after: null, kind: "agent", hunk_id: "h-1" },
                { finding: "V-09", rule: "upstream-https-required", file: "t.yaml", line: 36, before: null, after: "url: https://x", kind: "agent", hunk_id: "h-1" },
                { finding: null, rule: null, file: "t.yaml", line: 40, before: null, after: "extra: 1", kind: "agent", hunk_id: "h-2" }],
        guard: { code: "input_substituted", source: "examples/lumen-pay.good.yaml", why: "same bytes" } },
      overrides: [{ ref: "O-1", id: "ovr_abc", action: "waive", finding_id: "V-07", actor: "usr_owner01abc", approver: "usr_reviewer9xyz" }],
      // (ids as the engine stores them: the record names people in words, never the id)
      rerun: { id: "vrr_1", state: "running" }, head_sha: "9f2c1d0", packet: { verdict: { state: "blocked", reasons: ["open_p0:V-07", "rerun_pending"] } },
    })!;
    expect(v.overrides[0].id).toBe("O-1");
    expect(v.rerun.state).toBe("pending");
    expect(v.before.evidence).toBe("E-1");
    expect(v.after!.evidence).toBe("E-3");
    expect(v.guard!.code).toBe("input_substituted");
    expect(v.packet!.verdict.reasons).toContain("rerun_pending");
  });

  it("a stale head says the change moved on; a 404 the route wrote is a real answer, not 'not deployed'", async () => {
    const { sendOverride } = await import("../lib/validation-review");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "stale_head", message: "The change moved on; review the newer packet.", packet: {} }), { status: 409, headers: { "content-type": "application/json" } })));
    const stale = await sendOverride("/v1/changes/p/9/findings/V-01/override", { action: "accept", head_sha: "old" });
    expect(stale.stale).toBe(true);
    expect(stale.error).toBe("This change moved on; review the new version.");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "no_finding", message: "There is no finding V-99 on change #9." }), { status: 404, headers: { "content-type": "application/json" } })));
    const real = await sendOverride("/v1/changes/p/9/findings/V-99/override", { action: "accept", head_sha: "x" });
    expect(real.missing).toBe(false);
    expect(real.error).toBe("There is no finding V-99 on change #9.");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "approver_required", message: "A P0 waive needs a second person." }), { status: 403, headers: { "content-type": "application/json" } })));
    expect((await sendOverride("/v1/changes/p/9/findings/V-07/override", { action: "waive", reason: "r", head_sha: "x" })).code).toBe("approver_required");
  });

  it("packet: rerun pending holds Merge as Re-checking…; rule<n> reasons read as rows; deferred_to is open but not blocking", async () => {
    const { parsePacket, reasonRows } = await import("../lib/validation-review");
    const { packetHold, ReviewPacketView } = await import("../app/validation/review-packet");
    expect(packetHold(parsePacket({ verdict: { state: "blocked", reasons: ["rerun_pending"] }, rerun: { id: "vrr_1", state: "pending" } }))!.label).toBe("Re-checking…");
    expect(reasonRows(["rule2:C-1:claim without evidence"])).toEqual([{ check: "rule 2 · C-1", problem: "claim without evidence" }]);
    const p = parsePacket({ verdict: { state: "ready", reasons: [] }, findings: [
      { id: "R-1", source: { kind: "reviewer", name: "security" }, severity: "P1", title: "--fix rewrites the tenant quota", location: { file: "internal/lint/fixer.go", line: 69 },
        status: "open", fix_kind: "agent", deferred_to: "crun_2bc5f2aa" }] })!;
    await mount(<ReviewPacketView packet={p} projectId="northgate-gateway" />);
    const row = $('[data-finding="R-1"]')!;
    expect(row.textContent).toContain("deferred → follow-up");
    expect(row.textContent).toContain("crun_2bc5f2aa");
    expect(row.textContent).toContain("fixer.go:69");
  });
});
