// @vitest-environment happy-dom
/**
 * Tool policy (skills-mcp spec E7, test 6): a tool that changes the world shows Ask locked, with
 * the lock's reason; changing a read to Off is one policy call.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render } from "./_helpers/kit-render";
import { clone, installKitApi, KITS } from "./_helpers/kit-api";

const { ToolsSection } = await import("../app/skills/tools-section");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("an ability's tools", () => {
  it("a tier-2 row shows Ask locked, Allow refused with the reason", async () => {
    installKitApi();
    const prs = clone(KITS.fresh).abilities.find((a) => a.key === "open_prs")!;
    await render(<ToolsSection ability={prs} projectId="p1" actor="ceo" onChanged={() => {}} />);
    const allow = $('[data-testid="kit-policy-skill:github.create_pr_real-allow"]') as HTMLButtonElement;
    expect(allow.disabled).toBe(true);
    expect(allow.dataset.tip).toBe("Changes the world · always asks a person");
    expect($('[data-testid="kit-policy-skill:github.create_pr_real-ask"]')!.getAttribute("aria-checked")).toBe("true");
    expect($('[data-testid="kit-policy-skill:github.merge_pr_real-off"]')!.getAttribute("aria-checked")).toBe("true");
  });

  it("turning a read Off is one policy call", async () => {
    const calls = installKitApi();
    const research = clone(KITS.fresh).abilities.find((a) => a.key === "web_research")!;
    await render(<ToolsSection ability={research} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await click('[data-testid="kit-policy-mcp:web-intel.study_site-off"]');
    const writes = calls.filter((c) => c.method === "POST");
    expect(writes).toHaveLength(1);
    expect(writes[0].body).toMatchObject({ action: "policy", ability: "web_research",
                                           tool_ref: "mcp:web-intel.study_site", policy: "off" });
  });

  it("an unreviewed server offers Mark reviewed, which asks first", async () => {
    const calls = installKitApi(clone(KITS.failing));
    const research = clone(KITS.failing).abilities.find((a) => a.key === "web_research")!;
    await render(<ToolsSection ability={research} projectId="p1" actor="ceo" onChanged={() => {}} />);
    await click('[data-testid="kit-review-open-web-intel"]');
    expect($('[data-testid="kit-review-pop"]')).not.toBeNull();
    await click('[data-testid="kit-review-confirm"]');
    expect(calls.find((c) => c.body?.action === "review")?.url).toBe("/api/mcp");
  });
});
