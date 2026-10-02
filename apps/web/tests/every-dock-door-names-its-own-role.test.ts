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

/** Every opening of the dock goes through `openDockAbout` (lib/dock-subject.ts) — see
 *  every-ask-door-attaches-a-subject.test.ts — so the per-door decision is the `role:` it
 *  passes. The helper's type makes `role` required (`string | null`); this pins that no door
 *  works around it.
 *
 *  THE CALL IS FOUND BY BALANCING ITS PARENTHESES, not by searching for a closing token. The
 *  first version took the end as the next `});` — and boss-agent-home's door closes with
 *  `})}>`, so the search returned -1, the next search restarted from 0, and the loop never
 *  ended: the worker died out of memory and this rule silently stopped being checked. A scan
 *  that cannot find the end stops, and the door it could not read is reported as an offender
 *  rather than skipped. */
export function doorCallsIn(src: string): string[] {
  const out: string[] = [];
  const OPEN = "openDockAbout(";
  let at = src.indexOf(OPEN);
  while (at >= 0) {
    let depth = 0;
    let end = -1;
    let quote: string | null = null;
    for (let i = at + OPEN.length - 1; i < src.length; i++) {
      const ch = src[i];
      if (quote) {
        if (ch === "\\") i++;
        else if (ch === quote) quote = null;
        continue;
      }
      if (ch === '"' || ch === "'" || ch === "`") quote = ch;
      else if (ch === "(") depth++;
      else if (ch === ")" && --depth === 0) { end = i + 1; break; }
    }
    // Unbalanced: report the rest of the file as the call (it will fail the role rule only if
    // it truly lacks one) and STOP — never restart the search behind where it began.
    out.push(src.slice(at, end < 0 ? src.length : end));
    if (end < 0) break;
    at = src.indexOf(OPEN, end);
  }
  return out;
}

function doorCalls(): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (const f of readdirSync(APP).filter((x) => x.endsWith(".tsx") || x.endsWith(".ts"))) {
    const src = readFileSync(join(APP, f), "utf8");
    // Calls only — not the import line.
    for (const call of doorCallsIn(src)) if (call.startsWith("openDockAbout({")) out.push([f, call]);
  }
  return out;
}

describe("every door into the dock names its own role", () => {
  it("finds the doors at all", () => {
    // If this drops to zero the rest of the suite passes vacuously.
    expect(doorCalls().length).toBeGreaterThanOrEqual(8);
  });

  it("reads a door that closes with })}> — the one that hung the scan", () => {
    const files = doorCalls().map(([f]) => f);
    expect(files).toContain("boss-agent-home.tsx");
    const [call] = doorCallsIn(
      'x <b onClick={() => openDockAbout({ subject: s, question: "why (it) failed?", role: null })}>');
    expect(call.endsWith('role: null })')).toBe(true);
    // Unbalanced source ends the scan instead of looping.
    expect(doorCallsIn("openDockAbout({ role: null").length).toBe(1);
  });

  it("decides the role wherever it opens the dock", () => {
    const offenders = doorCalls()
      .filter(([, call]) => !/\brole\b/.test(call))
      .map(([file]) => file);
    expect(
      offenders,
      "these open the dock without saying who answers, so it inherits the last agent addressed",
    ).toEqual([]);
  });

  it("the helper sets or clears dock_role on every opening — silence is inheritance", () => {
    const HELPER = readFileSync(join(process.cwd(), "lib", "dock-subject.ts"), "utf8");
    expect(HELPER).toContain("dock_role: o.role,");
    expect(HELPER).toContain("if (value === null) url.searchParams.delete(key);");
  });

  it("the Deployment Manager addresses the Deployment Manager", () => {
    // The instance that was reported. Named explicitly so the fix cannot be quietly undone
    // by a refactor that still satisfies the counting rule above.
    const src = readFileSync(join(APP, "deployment-manager-ui.tsx"), "utf8");
    expect(src).toContain('role: "Deployment Manager"');
  });
});
