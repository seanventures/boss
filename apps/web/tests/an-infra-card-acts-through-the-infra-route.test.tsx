// @vitest-environment happy-dom
/**
 * Infra's attention cards (infra-observability spec §2.2): `<app> deploy failed` and `<host>
 * unreachable` reach Command Center and Infra's NEEDS YOU through the same AttentionCard. The
 * card is called Infra, draws exactly the server's answers, presses them through
 * POST /v1/infra/attention/act {id, option, actor, project_id} (the server action), says what
 * the press did in place, and `Open` follows the item's own `open` href — never a press.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { parseAttention } from "../lib/agents-live-board";
import { attentionDeployFailed, attentionHostDown } from "./_helpers/infra-fixtures";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

const act = vi.fn(async (form: FormData) => ({ ok: true, result: `Rolling back ${String(form.get("id"))}` }));
vi.mock("../app/infra/infra-attention-actions", () => ({ actOnInfraAttentionAction: (f: FormData) => act(f) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AttentionCard, kindWord, infraOpenPatch } = await import("../app/agents/attention-card");

const failed = parseAttention(attentionDeployFailed());
const down = parseAttention(attentionHostDown());

afterEach(() => { unmount(); act.mockClear(); });

describe("an Infra card", () => {
  it("is called Infra and draws the server's answers, Open as a link", async () => {
    await mount(<AttentionCard item={failed} projectId="p1" actor="ceo" />);
    expect(kindWord(failed)).toBe("Infra");
    expect($(".ag-att-context")?.textContent).toBe("Infra");
    expect($$('[data-testid^="ag-att-infra-"]').map((b) => b.textContent)).toEqual(["Roll back", "Open"]);
    const open = $('[data-testid="ag-att-infra-open"]') as HTMLAnchorElement;
    expect(open.tagName).toBe("A");
    expect(new URLSearchParams(open.getAttribute("href")!.split("?")[1]).get("deploy")).toBe("stack:sd_failed");
  });

  it("a press posts that option for that item and says what it did, in place", async () => {
    await mount(<AttentionCard item={failed} projectId="p1" actor="ceo" />);
    await click($('[data-testid="ag-att-infra-rollback"]'));
    await settle();
    expect(act).toHaveBeenCalledTimes(1);
    const form = act.mock.calls[0][0] as FormData;
    expect([form.get("id"), form.get("option"), form.get("actor"), form.get("project_id")])
      .toEqual(["infra:deploy_failed:t_down", "rollback", "ceo", "p1"]);
    expect($('[data-testid="ag-att-done"]')?.textContent).toBe("Rolling back infra:deploy_failed:t_down");
  });

  it("Open goes to the item's home without a press", async () => {
    await mount(<AttentionCard item={down} projectId="p1" actor="ceo" />);
    expect($$('[data-testid^="ag-att-infra-"]').map((b) => b.textContent)).toEqual(["Re-verify", "Open"]);
    await click($('[data-testid="ag-att-infra-open"]'));
    expect(act).not.toHaveBeenCalled();
    const q = new URLSearchParams(window.location.search);
    expect([q.get("area"), q.get("view"), q.get("host")]).toEqual(["infra", "fleet", "h_down"]);
  });

  it("the open href becomes a patch that names every param and closes the other drawers", () => {
    expect(infraOpenPatch("?area=infra&view=deploys&deploy=stack%3Asd_failed")).toMatchObject(
      { area: "infra", view: "deploys", deploy: "stack:sd_failed", host: null, target: null, preview: null });
  });
});
