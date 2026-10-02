import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { dockSpeaker, NO_ROLE_HEADING } from "../lib/dock-subject";
import { codeOnly } from "./_helpers/source";
import { doorCallsIn } from "./every-dock-door-names-its-own-role.test";

/**
 * A door that opens the dock with `role: null` asks BOSS in general — and the dock says so.
 *
 * The incident (boss-1 console walk, 2026-10-02): Repo and model's "Ask BOSS about this step",
 * the Observability starters and the BOSS Agent briefing items all open the dock with
 * `role: null`. lib/dock-subject.ts documents that as "asks BOSS in general", and the console
 * shell then headed the panel "Deployment Manager" and sent the turn with that roleName, so a
 * question about connecting a repository was answered by the role whose subject is hosts.
 */
const APP = join(process.cwd(), "app");

function nullRoleDoors(): string[] {
  const out: string[] = [];
  for (const f of readdirSync(APP).filter((x) => x.endsWith(".tsx") || x.endsWith(".ts"))) {
    const src = readFileSync(join(APP, f), "utf8");
    for (const call of doorCallsIn(src)) {
      if (call.startsWith("openDockAbout({") && /\brole: null\b/.test(call)) out.push(f);
    }
  }
  return out;
}

describe("no role means BOSS in general", () => {
  it("finds the null-role doors the incident named", () => {
    const doors = nullRoleDoors();
    for (const f of ["setup-ui.tsx", "monitoring-ui.tsx", "boss-agent-home.tsx"]) {
      expect(doors, f).toContain(f);
    }
  });

  it("an empty role is headed Ask BOSS and sends no roleName", () => {
    expect(dockSpeaker({ role: "", discussing: false })).toEqual({
      heading: NO_ROLE_HEADING,
      roleName: undefined,
    });
    expect(NO_ROLE_HEADING).toBe("Ask BOSS");
  });

  it("a named role is the role, both in the head and in the turn", () => {
    expect(dockSpeaker({ role: "Deployment Manager", discussing: false })).toEqual({
      heading: "Deployment Manager",
      roleName: "Deployment Manager",
    });
  });

  it("the shell takes both the heading and the roleName from that one definition", () => {
    const shell = codeOnly("app/ConsoleShell.tsx");
    expect(shell).toContain("dockSpeaker({ role: dockRole, discussing: dockDiscussing }).heading");
    expect(shell).toContain("dockSpeaker({ role: dockRole, discussing: false }).roleName");
    // No role literal of its own: a role the shell invents is a role no door asked for.
    expect(shell).not.toMatch(/"(the )?Deployment Manager"/);
  });
});
