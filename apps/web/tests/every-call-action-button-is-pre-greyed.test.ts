/**
 * EVERY CONTROL THAT PRESSES A SERVER ACTION IS PRE-GREYED FOR A VIEWER WHO LACKS ITS PERMISSION
 * (P1-18, IDENTITY.md §6.5).
 *
 * lib/call-action.ts takes the permission an action declares, but only uses it AFTER the press (a
 * step-up retry, a mirror refresh on 403). A viewer without the permission must see the control
 * disabled with the reason before pressing it. So: for every `callAction(x, …, "perm")` in the
 * console, the same file must gate "perm" — through components/gated.tsx (useGate / GatedButton),
 * useCan / CanButton / Can, or a SubmitButton / ActionForm `permission="perm"`.
 *
 * The only exemptions are LOADS: reads made by an effect or a poll, never by a person's press.
 * Each is listed below with why. A new button with an ungated permission fails here.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { isPermission } from "../lib/identity/permissions.gen";

const ROOT = process.cwd();
const SCAN = ["app", "components", "lib"];

/** `file action` pairs that are loads (an effect, a poll), not a press. */
const LOADS = new Set([
  "app/shepherd-ui.tsx getShepherdOverviewAction",           // the overview's load and refresh after a sweep
  "app/deploy-ui.tsx getDeploymentAction",                   // polls a running deployment's status
  "app/deploy-ui.tsx getDeliveryDefaultsAction",             // reads the default owner on mount
  "app/deploy-ui.tsx listRefsAction",                        // fills the branch list when a repo is chosen
  "app/focused-agents-ui.tsx loadFocusedAgentsAction",       // the panel's load and refresh
  "app/meeting-room-ui.tsx loadMeetingEvidenceAction",       // loads a meeting's evidence on open
  "app/agent-process-ui.tsx loadAgentProcessTemplatesAction", // the panel's load
  "app/agent-process-ui.tsx loadAgentProcessRunsAction",     // the panel's load
  "app/agent-process-ui.tsx loadAgentProcessRunAction",      // opens a run's detail (a read)
  "app/metatron-captain-ui.tsx loadMetatronCaptainSessionAction", // the session's load
  "app/metatron-captain-ui.tsx loadMetatronTheaterAction",   // the theater's load
  "app/process-manager-ui.tsx loadProcessManagerAction",     // the panel's load and refresh
  "app/factory/legacy-build-parts.tsx getQaBenchmarkReportAction", // loads a benchmark report
  "app/factory/legacy-build-parts.tsx getCodeTaskActivityAction",  // polls a build's activity
]);

/** callAction without a permission literal: generic plumbing whose caller carries the permission. */
const GENERIC = new Set([
  "app/client-forms.tsx action", // ActionForm: gated by its own `permission` prop (useCan) and every SubmitButton inside
]);

function files(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    if (name === "node_modules") continue;
    const path = join(dir, name);
    const st = statSync(join(ROOT, path));
    if (st.isDirectory()) out.push(...files(path));
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(path);
  }
  return out;
}

type Site = { file: string; action: string; permission: string | null; line: number };

function sites(): Site[] {
  const out: Site[] = [];
  for (const file of SCAN.flatMap(files)) {
    if (file === "lib/call-action.ts") continue;
    const src = readFileSync(join(ROOT, file), "utf8");
    const re = /callAction\(\s*([A-Za-z_$][\w$]*)\s*(?:,\s*([^,()]+|\([^)]*\)[^,]*))?(?:,\s*"([^"]+)")?\s*\)/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(src))) {
      out.push({ file, action: m[1], permission: m[3] ?? null, line: src.slice(0, m.index).split("\n").length });
    }
  }
  return out;
}

/** `export const NAME: Permission = "key"` anywhere in the console: a gate may name it. */
const NAMED: Map<string, string> = new Map(SCAN.flatMap(files).flatMap((file) =>
  [...readFileSync(join(ROOT, file), "utf8").matchAll(/export const ([A-Z_]+): Permission = "([^"]+)"/g)].map((m) => [m[1], m[2]] as [string, string])));

function gates(src: string, p: string): boolean {
  const q = p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (new RegExp(`(useGate|useCan)\\(\\s*"${q}"|permission=(\\{\\s*)?"${q}"`).test(src)) return true;
  return [...src.matchAll(/(?:useGate|useCan)\(\s*([A-Z_]+)|permission=\{\s*([A-Z_]+)\s*\}/g)].some((m) => NAMED.get(m[1] ?? m[2]) === p);
}

describe("every callAction button is pre-greyed", () => {
  const all = sites();

  it("finds the console's callAction sites", () => {
    expect(all.length).toBeGreaterThan(60);
  });

  it("every call names a catalogue permission (or is the generic ActionForm plumbing)", () => {
    const bad = all.filter((s) => s.permission === null ? !GENERIC.has(`${s.file} ${s.action}`) : !isPermission(s.permission))
      .map((s) => `${s.file}:${s.line} ${s.action} ${s.permission ?? "(no permission)"}`);
    expect(bad).toEqual([]);
  });

  it("every pressed call's permission is gated in the same file", () => {
    const missing = all
      .filter((s) => s.permission && !LOADS.has(`${s.file} ${s.action}`))
      .filter((s) => !gates(readFileSync(join(ROOT, s.file), "utf8"), s.permission!))
      .map((s) => `${s.file}:${s.line} ${s.action} needs a gate for ${s.permission}`);
    expect(missing).toEqual([]);
  });

  it("the load exemptions are still real (no stale entry hides a new press)", () => {
    const seen = new Set(all.map((s) => `${s.file} ${s.action}`));
    expect([...LOADS].filter((k) => !seen.has(k))).toEqual([]);
  });

  it("Organization's writes (app/agents/org-actions.ts) are pre-greyed wherever they are pressed", () => {
    const declared = readFileSync(join(ROOT, "app/agents/org-actions.ts"), "utf8");
    const perms: Record<string, string> = {
      give: NAMED.get("ORG_GIVE_PERMISSION")!, giveRecommended: NAMED.get("ORG_GIVE_PERMISSION")!, move: NAMED.get("ORG_MOVE_PERMISSION")!,
    };
    expect(perms).toEqual({ give: "agent_ability:assign", giveRecommended: "agent_ability:assign", move: "agent:update" });
    expect(declared).toMatch(/refused\(ORG_GIVE_PERMISSION\)/);
    const missing: string[] = [];
    for (const file of SCAN.flatMap(files)) {
      if (file === "app/agents/org-actions.ts") continue;
      const src = readFileSync(join(ROOT, file), "utf8");
      for (const m of src.matchAll(/actions\??\.(give|giveRecommended|move)\(/g)) {
        if (!gates(src, perms[m[1]])) missing.push(`${file} ${m[1]} needs a gate for ${perms[m[1]]}`);
      }
    }
    expect(missing).toEqual([]);
  });
});
