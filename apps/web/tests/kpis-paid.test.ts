import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: { createSelfImprovementProposal: vi.fn() },
}));

describe("paid self-improvement action feedback", async () => {
  const { revalidatePath } = await import("next/cache");
  const { client } = await import("../lib/client");
  const { submitSelfImprovementProposalAction } = await import("../app/actions-paid");

  beforeEach(() => vi.clearAllMocks());

  it("submits a proposal and revalidates the page", async () => {
    vi.mocked(client.createSelfImprovementProposal).mockResolvedValueOnce({ id: "sip1", project_id: "boss-dogfood", task_id: "t_sip", title: "Improve", summary: "s", change_ref: "https://ex", diff_summary: "d", expected_impact: [], test_plan: [], canary_scope: [], rollback_plan: [], success_metrics: [], status: "proposed", risk_tier: 2, created_at: "2026-06-05T00:00:00Z", updated_at: "2026-06-05T00:00:00Z" } as any);
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "exec");
    form.set("title", "Improve");
    form.set("summary", "s");
    form.set("change_ref", "https://ex");
    form.set("diff_summary", "d");
    await submitSelfImprovementProposalAction(form);
    expect(revalidatePath).toHaveBeenCalledWith("/");
  });
});
