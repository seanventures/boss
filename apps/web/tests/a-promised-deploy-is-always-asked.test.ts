/**
 * A deploy that promises a person's approval always asks for it (D2, 2026-10-18).
 *
 * The composer drew "Deploy — Ships only when you say yes" and "Nothing deploys without your
 * approval" while the staging policy shipped the deploy unasked. The words now come from the
 * server's one answer (deploy_policy.step_answer, via /asks and the installed outline); these
 * pin that no console surface says "you approve" for a deploy the server says nobody is asked.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { timelineOf, type Ask, type InstalledStep } from "../app/workflows/compose-plan";

const STEPS: InstalledStep[] = [
  { id: "build", approval: "auto", capability: "build" },
  { id: "deploy", approval: "manual", capability: "deploy" },
  { id: "deploy_again", label: "Deploy again (More tests)", approval: "manual", capability: "deploy" },
];
const deploys = (t: ReturnType<typeof timelineOf>) => t.filter((s) => s.deploy);

describe("a deploy's words are who the server says answers it", () => {
  it("asked: you say yes, on every deploy step", () => {
    const asks: Ask[] = ["deploy", "deploy_again"].map((id) => (
      { step_id: id, kind: "gate", who: "you", says: "Ships only when you say yes", promised: true }));
    for (const s of deploys(timelineOf({ steps: STEPS, mode: "new", asks }))) {
      expect(s.who.kind).toBe("you");
      expect(s.line).toBe("Ships only when you say yes");
    }
  });

  it("shipped by policy: the policy's words, never a promise", () => {
    const asks: Ask[] = ["deploy", "deploy_again"].map((id) => (
      { step_id: id, kind: "gate", who: "boss", policy: "staging",
        says: "Ships automatically under your staging policy" }));
    for (const s of deploys(timelineOf({ steps: STEPS, mode: "new", asks }))) {
      expect(s.who.kind).not.toBe("you");
      expect(s.line).toBe("Ships automatically under your staging policy");
    }
  });

  it("a stale 'say yes' from the server never reaches a step nobody is asked at", () => {
    const asks: Ask[] = [{ step_id: "deploy", kind: "gate", who: "boss", says: "" }];
    const [d] = deploys(timelineOf({ steps: STEPS.slice(0, 2), mode: "new", asks }));
    expect(d.line).not.toMatch(/say yes/);
  });

  it("with no server answer, a deploy the catalogue lists as policy-decided promises nobody", () => {
    const [d] = deploys(timelineOf({ steps: [{ ...STEPS[1], approval: "policy" }], mode: "new",
                                     gates: [], automatic: [] }));
    expect(d.who.kind).not.toBe("you");
    expect(d.line).not.toMatch(/say yes/);
  });
});

/** Every console source file. */
function sources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) sources(path, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(path);
  }
  return out;
}

describe("the promise is written in one place", () => {
  const root = join(__dirname, "..");
  const files = ["app", "lib", "components"].flatMap((d) => sources(join(root, d)));
  const said = (re: RegExp) => files.filter((f) => re.test(readFileSync(f, "utf8")))
    .map((f) => f.slice(root.length + 1)).sort();

  it("'Ships only when you say yes' is only the plan's words, drawn only at a gate", () => {
    expect(said(/Ships only when you say yes/)).toEqual(["app/workflows/compose-plan.ts"]);
  });

  it("'Nothing deploys without your approval' is only the composer's, behind the derived check", () => {
    expect(said(/Nothing deploys without your approval/)).toEqual(["app/workflows/new-project-composer.tsx"]);
    const src = readFileSync(join(root, "app/workflows/new-project-composer.tsx"), "utf8");
    expect(src).toMatch(/unaskedDeploy \? `\$\{unaskedDeploy\.line\}\.` : "Nothing deploys without your approval\."/);
  });
});
