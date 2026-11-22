// @vitest-environment happy-dom
/**
 * THE REVIEW PACKET IS ROWS, NOT PROSE (docs/design/validation-review/SPEC.md §1.4–§1.5, PR 2 web).
 *
 * The operator: "The Review packet display format is very bad - continuous sentence, no highlight
 * of important issues like P0 and P1s". The merge card for change #9 read as four fragments joined
 * with spaces, titled "Follow-up: deferred review findings — Follow-up: More tests — …", with
 * "Based on kgn_a491…, kgn_4d4e…" in large type. Now:
 *   - verdict → severity chips → the P0/P1 table first, one row per finding (rule · line · fix);
 *   - no element on the card holds more than two sentences;
 *   - the title is the work's root title, never a "Follow-up:" chain;
 *   - fact ids are a count chip, never inline;
 *   - an invalid packet lists its problems and Approve is disabled.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, mount, unmount } from "./_helpers/live-render";
import { parsePacket, rootTitle } from "../lib/validation-review";
import { PACKET_INVALID, PACKET_READY } from "../lib/validation-review-fixtures";
import { ReviewPacketView, packetHold } from "../app/validation/review-packet";
import { DecisionActions, recommendationLines } from "../app/decision-inbox-ui";
import type { DecisionFeedItem } from "../lib/decisions";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

/** Sentences in an element's OWN text (its text nodes, not its children's). */
function ownSentences(el: Element): number {
  const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join(" ").trim();
  if (!own) return 0;
  return own.split(/(?<=[.!?])\s+(?=[A-Z“"])/).filter((s) => /[A-Za-z]/.test(s)).length;
}
const sentencesOf = (el: Element) => (el.textContent ?? "").trim().split(/(?<=[.!?])\s+(?=[A-Z“"])/).filter((s) => /[A-Za-z]/.test(s)).length;

describe("the packet's words", () => {
  it("the title is the work's root title, never a chain of follow-up prefixes", () => {
    expect(rootTitle("Follow-up: deferred review findings — Follow-up: More tests — Validate tenant config with gwlint"))
      .toBe("Validate tenant config with gwlint");
    expect(rootTitle("Lumen Pay tenant onboarding")).toBe("Lumen Pay tenant onboarding");
  });

  it("a merge card without a packet draws one fact per line, never sentences joined with spaces", () => {
    const d = { id: "dec_1", question: "q", recommendation: {
      action: "review_pull_request", title: "Follow-up: More tests — Validate tenant config with gwlint",
      pull_request_url: "http://boss-local/local_northgate/northgate-gateway/pull/9", validation_status: "passed" } } as unknown as DecisionFeedItem;
    const lines = recommendationLines(d);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines[0]).toBe("Approve the code change “Validate tenant config with gwlint”.");
    for (const l of lines) expect(l.split(/(?<=\.)\s+(?=[A-Z])/).length).toBeLessThanOrEqual(2);
  });

  it("parses an absent or partial packet without throwing", () => {
    expect(parsePacket(undefined)).toBeNull();
    const thin = parsePacket({ verdict: { state: "ready" } })!;
    expect(thin.findings).toEqual([]);
    expect(thin.counts.P0).toEqual({ found: 0, open: 0, waived: 0 });
  });
});

describe("the ready packet (wireframe C)", () => {
  it("verdict, severity chips, then P0/P1 rows first — rule · line · fix, one row each", async () => {
    await mount(<ReviewPacketView packet={parsePacket(PACKET_READY)!} projectId="northgate-gateway" />);
    expect($('[data-testid="vr-verdict"]')!.getAttribute("data-state")).toBe("ready");
    expect($('[data-testid="review-packet-title"]')!.textContent).toBe("Merge change #9 · Lumen Pay tenant onboarding");
    expect($('[data-testid="vr-sev-P0"]')!.textContent).toContain("4 found");
    const rows = $$('[data-testid="vr-p01"] [data-testid="vr-packet-row"]');
    expect(rows.map((r) => r.getAttribute("data-finding"))).toEqual(["R-1", "V-06", "V-03", "V-07", "V-09", "V-01", "V-04", "V-05", "V-08"]);
    // The P1 the operator could not find: the fixer's never-fix rewrite, deferred to change #10.
    expect(rows[0].textContent).toContain("fixer.go:69");
    expect(rows[0].textContent).toContain("deferred");
    expect(rows[0].querySelector('[data-testid="vr-followup"]')!.getAttribute("href")).toBe("/changes/northgate-gateway/10");
    const v03 = rows.find((r) => r.getAttribute("data-finding") === "V-03")!;
    expect(v03.querySelector(".vr-c-rule")!.getAttribute("title")).toBe("no-inlined-secrets · L17");
    // Long values are clipped to one line each, and the whole value is the cell's tooltip.
    const v06 = rows.find((r) => r.getAttribute("data-finding") === "V-06")!;
    expect(v06.querySelector(".vr-c-ba")!.getAttribute("title")).toBe('["*"] → ["https://dashboard.lumenpay.example"]');
    expect(v06.querySelectorAll(".vr-c-ba .vr-clip")).toHaveLength(2);
    // Overrides name the person in words (a role here), never an id.
    expect($$('[data-testid="vr-override-row"]').map((r) => r.textContent).join(" ")).toContain("accept V-07 · Owner");
    // The P2 waive is below, folded.
    expect($('[data-testid="vr-p23"]')!.textContent).toContain("P2 / P3 · 1");
    // Provenance: the run by its wfr_ id, the build, the change; 14 facts as one chip.
    expect($('[data-testid="vr-prov-run"]')!.getAttribute("href")).toContain("wfr=wfr_40229c5c963c4f599c51");
    expect($('[data-testid="vr-prov-build"]')!.textContent).toContain("crun_58aecc16");
    expect($('[data-testid="vr-prov-change"]')!.getAttribute("href")).toBe("/changes/northgate-gateway/9");
    expect($('[data-testid="vr-facts"]')!.textContent).toBe("14 facts");
    expect($('[data-testid="review-packet"]')!.textContent).not.toMatch(/kgn_/);
    expect($('[data-testid="review-packet"]')!.textContent).not.toMatch(/Follow-up:/);
  });

  it("no element on the card holds more than two sentences", async () => {
    await mount(<ReviewPacketView packet={parsePacket(PACKET_READY)!} projectId="northgate-gateway" />);
    const card = $('[data-testid="review-packet"]')!;
    const over = [...card.querySelectorAll("*")].filter((el) => ownSentences(el) > 2).map((el) => el.textContent);
    expect(over).toEqual([]);
    for (const el of card.querySelectorAll("p, td, li, .vr-risk, .vr-verdict")) expect(sentencesOf(el)).toBeLessThanOrEqual(2);
  });

  it("a ref chip scrolls to the row it names", async () => {
    await mount(<ReviewPacketView packet={parsePacket(PACKET_READY)!} projectId="northgate-gateway" />);
    const target = document.getElementById("vr-ref-O-2")!;
    target.scrollIntoView = vi.fn();
    ($$('[data-testid="vr-risks"] [data-ref="O-2"]')[0] as HTMLButtonElement).click();
    expect(target.scrollIntoView).toHaveBeenCalled();
    expect(target.hasAttribute("data-flash")).toBe(true);
  });
});

describe("the invalid packet", () => {
  it("lists each failing rule, holds Merge, and never falls back to prose", async () => {
    const p = parsePacket(PACKET_INVALID)!;
    await mount(<ReviewPacketView packet={p} projectId="northgate-gateway" />);
    expect($('[data-testid="vr-verdict"]')!.textContent).toContain("Packet invalid");
    expect($('[data-testid="vr-verdict"]')!.textContent).toContain("2 problems · merge disabled");
    expect($$('[data-testid="vr-invalid"] tbody tr').map((r) => r.firstElementChild!.textContent)).toEqual(["input_substituted", "claim_without_evidence"]);
    expect($('[data-testid="vr-p01"]')).toBeNull();
    const card = $('[data-testid="review-packet"]')!;
    expect([...card.querySelectorAll("*")].filter((el) => ownSentences(el) > 2)).toEqual([]);
  });

  it("Approve and merge is disabled while the packet is invalid", async () => {
    const hold = packetHold(parsePacket(PACKET_INVALID))!;
    expect(hold.state).toBe("packet_invalid");
    const d = {
      id: "dec_5956", question: "Merge change #9", status: "pending", riskTier: 2, recommendation: { action: "review_pull_request" },
      options: [], costSummary: {}, createdAt: "2026-11-21T00:00:00Z", updatedAt: "2026-11-21T00:00:00Z",
      allowedActions: [{ action: "approve", label: "Approve and merge", tone: "approve", effect: "" }, { action: "reject", label: "Keep blocked", tone: "reject", effect: "" }],
      held: hold,
    } as unknown as DecisionFeedItem;
    await mount(<DecisionActions decision={d} projectId="northgate-gateway" actorId="ceo" />);
    const approve = $('[data-testid="decision-approve"]') as HTMLButtonElement;
    expect(approve.disabled).toBe(true);
    expect(approve.getAttribute("title")).toContain("Packet invalid");
    expect(packetHold(parsePacket(PACKET_READY))).toBeNull();
    expect(packetHold(parsePacket(PACKET_READY), true)!.label).toBe("Re-checking…");
  });
});

describe("the change page asks for a workflow run only by its run id (SPEC §5.5)", () => {
  it("a build id in run_id is never sent to /v1/workflow-runs/", async () => {
    const { parseChangeSet } = await import("../lib/repo-links");
    expect(parseChangeSet({ number: 9, run_id: "crun_2bc5f2" }).workflow_run_id).toBeNull();
    expect(parseChangeSet({ number: 9, run_id: "crun_2bc5f2", workflow_run_id: "wfr_40229c5c" }).workflow_run_id).toBe("wfr_40229c5c");
  });
});

describe("a blocked packet merges only with a reason (engine packet_blocked)", () => {
  it("Approve is the reasoned forced-merge form naming the open P0/P1 rows; invalid stays disabled", async () => {
    const { blockedPacketDecision } = await import("../app/decision-inbox-ui");
    const blocked = parsePacket({ verdict: { state: "blocked", reasons: ["open_p0:V-03"] }, findings: [
      { id: "V-03", source: { kind: "validator", name: "gwlint", rule_id: "no-inlined-secrets" }, severity: "P0", location: { line: 17 }, status: "open", fix_kind: "agent" },
      { id: "R-1", source: { kind: "reviewer", name: "security" }, severity: "P1", status: "open", deferred_to: "crun_2bc5" },
      { id: "V-09", source: { kind: "validator", rule_id: "upstream-https-required" }, severity: "P0", status: "fixed" }] })!;
    expect(packetHold(blocked)).toBeNull();
    const base = {
      id: "dec_9", question: "Merge change #9", status: "pending", riskTier: 2, recommendation: { action: "review_pull_request" },
      options: [], costSummary: {}, createdAt: "2026-11-21T00:00:00Z", updatedAt: "2026-11-21T00:00:00Z",
      allowedActions: [{ action: "approve", label: "Approve and merge", tone: "approve", effect: "" }, { action: "reject", label: "Keep blocked", tone: "reject", effect: "" }],
    } as unknown as DecisionFeedItem;
    await mount(<DecisionActions decision={blockedPacketDecision(base, blocked)} projectId="northgate-gateway" actorId="ceo" />);
    const form = $('[data-testid="decision-reasoned-approve"]')!;
    expect($('[data-testid="decision-blocked-rows"]')!.textContent).toBe("P0 V-03 no-inlined-secrets · L17");
    expect((form.querySelector('input[name="force"]') as HTMLInputElement).value).toBe("true");
    expect(form.querySelector('[data-testid="decision-reason"]')).not.toBeNull();
    expect($('[data-testid="decision-approve"]')!.textContent).toBe("Merge anyway");
    // A ready or invalid packet is unchanged.
    expect(blockedPacketDecision(base, parsePacket(PACKET_READY)!)).toBe(base);
    expect(blockedPacketDecision(base, parsePacket(PACKET_INVALID)!)).toBe(base);
  });
});
