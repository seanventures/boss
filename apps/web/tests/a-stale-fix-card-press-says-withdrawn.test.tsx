// @vitest-environment happy-dom
/**
 * A PRESS THAT WITHDREW ITS CARD SAYS SO (PR #170 review).
 *
 * Pressing a stale re-check "Fix these" card withdraws it: the API answers 409
 * `{status: "withdrawn", detail: <note>}` and commits the withdrawal. The inbox and the project
 * drawer said "Approved."; both now say "Withdrawn: <note>", in the API's words.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BossApiError } from "@boss/api-client";

const NOTE = "A newer build of this change (crun_new) exists; its own checks decide what is left to fix. Nothing was started.";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
  redirect: vi.fn(),
}));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/headers", () => ({
  headers: async () => new Headers(),
  cookies: async () => ({ get: () => undefined }),
}));
vi.mock("../lib/client", async () => {
  const { BossApiError: E } = await import("@boss/api-client");
  return {
    PROJECT_ID: "northgate",
    resolveProjectId: (projectId?: string) => projectId?.trim() || "northgate",
    API_URL: "http://127.0.0.1:8000",
    authHeaders: (extra?: Record<string, string>) => extra ?? {},
    client: {
      approveDecision: vi.fn(async () => {
        throw new E(NOTE, 409, { detail: NOTE, status: "withdrawn", decision_id: "dec_fix" });
      }),
    },
  };
});

import { DecisionActs } from "../app/projects/drawers-more";
import type { PxCtx } from "../app/projects/project-page";
import { withdrawnNote } from "../lib/decision-withdrawn";
import { FX_OVERVIEW } from "../lib/project-experience-fixtures";
import { parseOverview, type NeedsYou } from "../lib/project-experience";
import { click, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => unmount());

describe("a stale Fix card's press", () => {
  it("reads only a 409 that says withdrawn", () => {
    expect(withdrawnNote(new BossApiError("x", 409, { status: "withdrawn", detail: NOTE }))).toBe(NOTE);
    expect(withdrawnNote(new BossApiError("x", 409, { detail: "refused" }))).toBeNull();
    expect(withdrawnNote(new BossApiError("x", 200, { status: "withdrawn" }))).toBeNull();
  });

  it("says Withdrawn in the inbox, never Approved", async () => {
    const { resolveDecisionAction } = await import("../app/actions");
    const fd = new FormData();
    fd.set("decision_id", "dec_fix");
    fd.set("project_id", "northgate");
    fd.set("actor", "ceo");
    fd.set("action", "approve");
    const out = await resolveDecisionAction(fd);
    expect(out).toMatchObject({ ok: true, message: `Withdrawn: ${NOTE}`,
                                data: { decision: { id: "dec_fix", status: "withdrawn" } } });
  });

  it("says Withdrawn in the project drawer, never Approved", async () => {
    stubFetch((c) => (c.method === "POST" && c.path === "/v1/decisions/dec_fix/approve"
      ? { status: 409, body: { detail: NOTE, status: "withdrawn", decision_id: "dec_fix" } }
      : undefined));
    const wire = {
      id: "dec_fix", kind: "decision", label: "Fix these (about $1.20)", detail: "", mini: [],
      action: { label: "Fix these", href: "?drawer=needs:dec_fix", decision_id: "dec_fix" },
    };
    const n: NeedsYou = parseOverview({ ...FX_OVERVIEW, needs_you: [wire] }).needs_you[0];
    const ctx = { projectId: "northgate", work: null, overview: { recent_work: [] }, open: vi.fn(),
                  refreshWork: vi.fn() } as unknown as PxCtx;
    const host = await mountAs(<div><DecisionActs ctx={ctx} n={n} /></div>);
    await click(tid("px-drawer-needs-approve"));
    expect(host.textContent).toContain(`Withdrawn: ${NOTE}`);
    expect(host.textContent).not.toContain("Approved");
    expect((ctx.refreshWork as unknown as ReturnType<typeof vi.fn>)).toHaveBeenCalled();
  });
});
