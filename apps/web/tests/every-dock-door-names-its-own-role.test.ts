import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * A panel that opens the chat must say who is answering.
 *
 * `dock_role` is a URL parameter, so it SURVIVES until something overwrites it. A door that
 * sets `dock_ask` and not `dock_role` therefore opens whichever role the operator last spoke
 * to — and the dock dutifully renders that name above an answer about something else.
 *
 * Reported as: the Deployment Manager card, whose whole subject is hosts, releases and
 * rollback readiness, opening a chat headed "Ask Security Reviewer". Three of the six doors
 * into the dock had this bug; it is invisible on a fresh page and appears only after you have
 * used a different agent, which is why it survived.
 *
 * So the rule is per-door and absolute: every place that sets `dock_ask` also decides
 * `dock_role` in the same breath — `set` to name an agent, `delete` to ask BOSS in general.
 * Silence is not a third option; silence is inheritance.
 */
const APP = join(process.cwd(), "app");

function sourcesSettingDockAsk(): Array<[string, string]> {
  return readdirSync(APP)
    .filter((f) => f.endsWith(".tsx") || f.endsWith(".ts"))
    .map((f) => [f, readFileSync(join(APP, f), "utf8")] as [string, string])
    .filter(([, src]) => src.includes('searchParams.set("dock_ask"'));
}

describe("every door into the dock names its own role", () => {
  it("finds the doors at all", () => {
    // If this drops to zero the rest of the suite passes vacuously.
    expect(sourcesSettingDockAsk().length).toBeGreaterThanOrEqual(5);
  });

  it("decides dock_role wherever it sets dock_ask", () => {
    const offenders: string[] = [];
    for (const [file, src] of sourcesSettingDockAsk()) {
      const asks = (src.match(/searchParams\.set\("dock_ask"/g) ?? []).length;
      const decides = (src.match(/searchParams\.(set|delete)\("dock_role"/g) ?? []).length;
      if (decides < asks) offenders.push(`${file} (${asks} ask, ${decides} role)`);
    }
    expect(
      offenders,
      "these open the dock without saying who answers, so it inherits the last agent addressed",
    ).toEqual([]);
  });

  it("the Deployment Manager addresses the Deployment Manager", () => {
    // The instance that was reported. Named explicitly so the fix cannot be quietly undone
    // by a refactor that still satisfies the counting rule above.
    const src = readFileSync(join(APP, "deployment-manager-ui.tsx"), "utf8");
    expect(src).toContain('url.searchParams.set("dock_role", "Deployment Manager");');
  });
});
