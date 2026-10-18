// @vitest-environment happy-dom
/**
 * The self-fix approval card (docs/product/BOSS_FIXES_ITSELF.md §10.2), drawn in its Approvals
 * row for `recommendation.action === "self_fix.install"`: the diagnosis and the rejected first
 * guess, the diff summary and View diff, the new regression test marked ✗→✓, the reviewers, the
 * files, the tier and the host-effect badge, "Installs on this one-box and resumes the step",
 * cost/minutes/engine, the people note — and the inbox's own answers carrying the content hash.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionFeedItem } from "../../lib/decisions";
import { $$, click, mountAs, tid, unmount } from "../_helpers/multirepo-fixtures";
import { SFX, card, fakeSelfFixApi, newState } from "./_fixtures";

const resolve = vi.fn();
vi.mock("../../app/actions", async (original) => ({
  ...(await original<typeof import("../../app/actions")>()),
  resolveDecisionAction: (fd: FormData) => resolve(fd),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("../../app/client-relative-time", () => ({ ClientRelativeTime: () => React.createElement("time", null, "4 min ago") }));

const { DecisionInbox } = await import("../../app/decision-inbox-ui");

beforeEach(() => { resolve.mockReset(); });
afterEach(unmount);

function decision(extra: Record<string, unknown> = {}) {
  const c = card();
  return {
    id: "dec_sfx1", status: "pending", question: `Install a BOSS self-fix? ${c.title}`, riskTier: 3,
    recommendation: { action: "self_fix.install", self_fix_id: SFX, card: c, title: c.title, reason_code: "self_fix_install",
                      action_preview: { content_hash: "hash_sfx_v1", summary: c.title, commit: "c".repeat(40), tier: 3 } },
    options: [], costSummary: { usd: 3.12 }, createdAt: "2026-10-17T08:12:00Z", updatedAt: "2026-10-17T08:12:00Z",
    allowedActions: [
      { action: "approve", label: "Approve and install", tone: "approve", effect: "Installs the fix on this one-box.", needs: null },
      { action: "revise", label: "Request changes", tone: "neutral", effect: "One more repair round with your note.", needs: "note" },
      { action: "reject", label: "Reject", tone: "reject", effect: "Nothing is installed.", needs: null },
    ],
    ...extra,
  };
}
function mount(d: Record<string, unknown> = decision()) {
  return mountAs(
    <DecisionInbox feed={{ projectId: "p1", apiReachable: true, items: [d as unknown as DecisionFeedItem] }}
                   tasks={[]} roles={[]} projectId="p1" actorId="ceo" orgId="org_1" />, "owner");
}

describe("the self-fix approval card", () => {
  it("draws every part of the card", async () => {
    fakeSelfFixApi(newState());
    await mount();
    expect(tid("sfx-card")).toBeTruthy();
    expect(tid("sfx-card-title")?.textContent).toBe(card().title);
    expect(tid("sfx-card-tier")?.textContent).toBe("Tier 3");
    expect(tid("sfx-card-host-effect")?.getAttribute("title")).toContain("docker rm -f");
    expect(tid("sfx-card-broke")?.textContent).toContain("Port 8000");
    expect(tid("sfx-card-why")?.textContent).toContain("nothing retired it there");
    expect(tid("sfx-card-rejected")?.getAttribute("title")).toContain("fix the single-container retire step");
    expect(tid("sfx-card-confidence")?.textContent).toBe("high");
    expect(tid("sfx-card-confidence")?.getAttribute("title")).toContain("seen 2 times");
    expect(tid("sfx-card-change")?.textContent).toContain("stack_deploy.py");
    expect(tid("sfx-card-change")?.textContent).toContain("+58");
    expect(tid("sfx-card-change")?.textContent).toContain("−1");
    const fresh = tid("sfx-card-new-test")!;
    expect(fresh.textContent).toBe("✗→✓ NEW test_a_release_leaving_the_shared_project_retires_its_app.py");
    expect(fresh.getAttribute("title")).toContain("fails on BOSS as it was, passes with this fix");
    expect(tid("sfx-card-tests")?.textContent).toContain("41 affected");
    expect(tid("sfx-card-tests")?.textContent).toContain("none weakened");
    expect($$('[data-testid^="sfx-card-seat-"]').map((s) => s.textContent)).toEqual(
      ["code ✓", "security ✓", "regression ✓", "consistency ✓"]);
    expect(tid("sfx-card-seat-security_check")?.getAttribute("title")).toContain("quoting checked");
    expect(tid("sfx-card-review")?.textContent).toContain("other engine");
    expect(tid("sfx-card-cost")?.textContent).toBe("$3.12 · 14 min · codex");
    expect(tid("sfx-card-on-approve")?.textContent).toBe("Installs on this one-box and resumes the step");
    expect(tid("sfx-card-people")?.getAttribute("title")).toContain("single-operator waiver");
    expect(tid("sfx-card-open")?.getAttribute("href")).toContain(`sfx=${SFX}`);
  });

  it("answers through the inbox's own form, with the card's content hash", async () => {
    fakeSelfFixApi(newState());
    await mount();
    expect(tid("decision-approve")?.textContent).toBe("Approve and install");
    expect(tid("decision-reject")).toBeTruthy();
    await click(tid("decision-approve"));
    const form = Object.fromEntries((resolve.mock.calls[0][0] as FormData).entries());
    expect(form).toMatchObject({ decision_id: "dec_sfx1", project_id: "p1", actor: "ceo", content_hash: "hash_sfx_v1" });
    expect(tid("decision-approve")?.getAttribute("value")).toBe("approve");
  });

  it("View diff reads the diff once, read only", async () => {
    const calls = fakeSelfFixApi(newState());
    await mount();
    await click(tid("sfx-card-view-diff"));
    expect(tid("sfx-card-diff")?.textContent).toContain("+fixed");
    expect(calls.filter((c) => c.search.includes("view=diff"))).toHaveLength(1);
    expect(tid("sfx-card-diff")?.querySelector("textarea")).toBeNull();
  });

  it("a tier-2 fix with no host effect shows no badge and one approver", async () => {
    fakeSelfFixApi(newState());
    const c = card({ tier: 2, flags: [], change: { ...card().change, host_effect: [] } });
    await mount(decision({ recommendation: { ...decision().recommendation, card: c } }));
    expect(tid("sfx-card-tier")?.textContent).toBe("Tier 2");
    expect(tid("sfx-card-host-effect")).toBeNull();
    expect(tid("sfx-card-people")?.textContent).toContain("One approver");
  });

  it("a decided card shows its verdict, not the answers", async () => {
    fakeSelfFixApi(newState());
    await mount(decision({ status: "approved", approvedBy: "usr_x" }));
    await click(tid("decision-filter-all"));
    expect(tid("sfx-card")).toBeTruthy();
    expect(tid("decision-approve")).toBeNull();
  });
});
