/**
 * EVERY CLIENT WRITE IS PRE-GREYED FOR A VIEWER WHO LACKS ITS PERMISSION — the writes that do
 * not go through a server action (P1-18; the callAction ones are
 * tests/every-call-action-button-is-pre-greyed.test.ts).
 *
 * The console's other writes leave the browser through lib/api-write.ts (apiWrite / runPress).
 * lib/identity/write-permissions.ts names each one — a helper, a dispatcher's operation, or a
 * component that writes inline — with the permission its API route checks. This test:
 *
 *   (a) finds every browser file that calls apiWrite / runPress and fails if it is not accounted
 *       for: a registered helper module, a registered inline writer, or a listed exemption;
 *       and every export of a helper module that writes must be registered;
 *   (b) for every file that calls a registered helper, operation or writes inline, requires the
 *       same file to gate that permission (components/gated.tsx useGate / GatedButton, useCan,
 *       CanButton, Can, an ActionForm/SubmitButton `permission=`, or a named Permission constant).
 *
 * Exemptions are explicit and commented: background writes nobody presses, and server-side code.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { ACTION_PERMISSIONS } from "../lib/identity/action-permissions";
import { isPermission } from "../lib/identity/permissions.gen";
import { INLINE_WRITES, WRITE_HELPERS, WRITE_OPS } from "../lib/identity/write-permissions";

const ROOT = process.cwd();
const SCAN = ["app", "components", "lib"];

/** Files that call apiWrite/runPress but are not a person's press in the browser. */
const NOT_BROWSER_PRESSES: Record<string, string> = {
  "lib/api-write.ts": "the write helper itself",
  "lib/call-action.ts": "server actions: tests/every-call-action-button-is-pre-greyed.test.ts",
  "lib/server-action.ts": "used by server actions (\"use server\" files) only",
  "lib/identity/api.ts": "identity writes: every identity control is a CanButton (identity-mirroring tests)",
  "lib/captain-playbook.ts": "no browser importer: server-side Captain playbooks",
  "lib/metatron.ts": "read by app/page.tsx on the server",
  "lib/observability-suite.ts": "server-side feed reads; the browser imports only its types",
  "lib/release-planning.ts": "server actions (app/actions.ts); the browser imports only its types",
  "lib/agent-converse.ts#postStream": "inside converseApi / useConversation (registered)",
};

/** Exports of helper modules the "sends a write" heuristic catches that are not writes. */
const NOT_WRITES = new Set([
  "lib/agents.ts#errorSentence",                 // words for a refusal
  "lib/agent-instructions.ts#refusal",           // words for a refusal
  "lib/agent-instructions.ts#instructionsApi.modes", // not a member that sends
  "lib/workflow-api.ts#checkSpec",               // POST …/check is a read-only dry run (workflow:read), run as you type
  "lib/stack-deploy.ts#deployFormProblem",       // pure form validation
  "lib/security-scan.ts#runSecurityScan",        // no browser caller (server actions only)
  "lib/add-server.ts#readServer",                // POST …/servers/read reads the words and probes; creates nothing (assistant:chat), as you type
]);

/** `file helper` uses that run only inside a press gated on a STRICTER permission (every role
 *  holding the stricter one holds this one too: lib/identity/permissions.gen ROLE_PERMISSIONS). */
const SUBSUMED: Record<string, string> = {
  "app/skills/add-mcp-sheet.tsx mcpApi.test": "inside Save & test, gated on mcp_server:review (owner, admin ⊂ mcp_server:register)",
  "app/skills/mcp-drawer.tsx mcpApi.test": "inside Save & test, gated on mcp_server:review (owner, admin ⊂ mcp_server:register)",
};

/** Files that call a registered helper only as part of a press another file gates. */
const PASSTHROUGH: Record<string, string> = {
  "app/agents/org-actions.ts": "refuses through the mirror itself; its buttons (org-teams, org-agent-panel) are gated — the callAction test checks them",
  "app/workflows/editor/save/use-save-flow.tsx": "applyWorkClass runs inside Save, which workflow-editor.tsx gates on workflow:publish",
  "lib/watch-app.ts": "its helpers (registered) send through obsWrite; app/projects/watch-app.tsx gates each press on observability:manage",
  "lib/work-items.ts": "its helpers (registered) send through obsWrite; app/projects/work-tree.tsx gates each press on work_item:manage",
  "lib/skill-policies.ts": "its helpers (registered) send through obsWrite; app/skills/skill-access-rules.tsx gates each press on skill:manage / skill:read",
  "lib/subscription-pools.ts": "its helpers (registered) send through obsWrite; app/setup/pools-row.tsx gates each press on entitlement:manage",
  "lib/llm-gateways.ts": "its helpers (registered) send through obsWrite; app/setup/gateways-row.tsx gates each press on model_provider:manage",
  "lib/reflection.ts": "its helpers (registered) send through obsWrite; app/activity/reflection-panel.tsx gates each press on agent:update",
  "lib/agent-feedback.ts": "its helper (registered) sends through obsWrite; app/activity/agent-feedback.tsx gates each press on agent:update",
  "lib/work-ux.ts": "its helpers (registered) send through obsWrite; app/projects/work-item-extras.tsx gates each press on work_item:manage",
  "lib/work-items.ts": "its helpers (registered) send through obsWrite; app/projects/work-tree.tsx and work-access.tsx gate each press on work_item:manage / role_binding:assign",
  "lib/documents.ts": "its helpers (registered) send through obsWrite; app/projects/work-docs.tsx gates each press on work_item:manage",
  "lib/inbox.ts": "its helpers (registered) send through obsWrite; app/projects/work-inbox.tsx gates each press on work_item:read",
  "lib/tool-policies.ts": "its helpers (registered) send through obsWrite; app/skills/tool-rules.tsx gates each press on mcp_server:review / mcp_server:read",
  "app/always-allow-ui.tsx": "sends through obsWrite (/v1/decisions/ registered); its one press is a GatedButton on decision:approve",
};

/** `file helper-or-op` pairs that are background writes (an effect, a poll), never a press. */
const BACKGROUND = new Set([
  "app/delivery-workflows-ui.tsx post:recheck",        // the panel re-checks stalled runs on load; the Recheck press is gated
  "app/agent-ui.tsx inline",                            // opens (ensures) the chat thread on mount; Clear is gated
  "app/improve/checkin-card.tsx improveWrite:prompt_shown", // records that a prompt was shown
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

const read = (f: string) => readFileSync(join(ROOT, f), "utf8");
const isServer = (f: string, src: string) => f.startsWith("app/api/") || /^\s*["']use server["']/.test(src)
  || /(^|\/)(page|layout|route)\.tsx?$/.test(f) && !/^\s*["']use client["']/.test(src);
const ALL = SCAN.flatMap(files);
const BROWSER = ALL.filter((f) => !isServer(f, read(f)));

const NAMED: Map<string, string> = new Map(ALL.flatMap((file) =>
  [...read(file).matchAll(/export const ([A-Z_]+): Permission = "([^"]+)"/g)].map((m) => [m[1], m[2]] as [string, string])));

/** Does `src` gate permission `p` (the same rule as the callAction test)? */
function gates(src: string, p: string): boolean {
  const q = p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (new RegExp(`(useGate|useCan)\\(\\s*"${q}"|permission=(\\{\\s*)?"${q}"`).test(src)) return true;
  return [...src.matchAll(/(?:useGate|useCan)\(\s*([A-Z_]+)|permission=\{\s*([A-Z_]+)\s*\}/g)].some((m) => NAMED.get(m[1] ?? m[2]) === p);
}

const needs = (g: string | readonly string[]): string[] => (Array.isArray(g) ? [...g] : [g as string]).filter((x) => x !== "self");
const base = (mod: string) => mod.replace(/^.*\//, "").replace(/\.tsx?$/, "");
const imports = (src: string, mod: string, name: string) =>
  new RegExp(`import\\s*\\{[^}]*\\b${name}\\b[^}]*\\}\\s*from\\s*"[./]*(?:[^"]*/)?${base(mod)}"`).test(src);

/** Client-side action modules (not "use server") from ACTION_PERMISSIONS: their writes are helpers too. */
const CLIENT_ACTIONS: Record<string, string> = Object.fromEntries(Object.entries(ACTION_PERMISSIONS).filter(([k, p]) => {
  const file = k.split("#")[0];
  return !/^\s*["']use server["']/.test(read(file)) && !/:read(_all|_metadata)?$/.test(p);
}));

type Use = { file: string; what: string; permission: string };

function uses(): Use[] {
  const out: Use[] = [];
  const helpers: [string, string | readonly string[]][] = [...Object.entries(WRITE_HELPERS), ...Object.entries(CLIENT_ACTIONS)];
  for (const file of BROWSER) {
    const src = read(file);
    for (const [key, gate] of helpers) {
      const [mod, member] = key.split("#");
      if (file === mod) continue;
      const [obj, method] = member.includes(".") ? member.split(".") : [member, null];
      if (!imports(src, mod, obj)) continue;
      const called = method ? new RegExp(`\\b${obj}\\.${method}\\(`).test(src) : new RegExp(`\\b${obj}\\(`).test(src);
      if (called) for (const p of needs(gate)) out.push({ file, what: member, permission: p });
    }
    for (const [key, ops] of Object.entries(WRITE_OPS)) {
      const [mod, fn] = key.split("#");
      if (file !== mod && !imports(src, mod, fn)) continue;
      if (file === mod) continue;
      for (const m of src.matchAll(new RegExp(`\\b${fn}(?:<[^>(]*>)?\\(\\s*(?:[\\w.]+\\s*,\\s*)?[\\s]*["\`]([^"\`$]+)`, "g"))) {
        const op = m[1];
        const gate = ops[op] ?? Object.entries(ops).find(([k]) => k.endsWith("/") && op.startsWith(k))?.[1];
        if (BACKGROUND.has(`${file} ${fn}:${op}`)) continue;
        // "self" (no permission beyond reaching the screen) needs no gate, as for a helper.
        if (!gate) out.push({ file, what: `${fn}:${op}`, permission: `UNREGISTERED op ${op}` });
        else for (const p of needs(gate)) out.push({ file, what: `${fn}:${op}`, permission: p });
      }
    }
  }
  for (const [file, gatesNeeded] of Object.entries(INLINE_WRITES)) {
    for (const p of needs(gatesNeeded)) out.push({ file, what: "inline", permission: p });
  }
  return out;
}

describe("every client write is pre-greyed", () => {
  it("the registry names catalogue permissions only", () => {
    const all = [...Object.values(WRITE_HELPERS).flatMap(needs), ...Object.values(WRITE_OPS).flatMap((o) => Object.values(o).flatMap(needs)),
      ...Object.values(INLINE_WRITES).flatMap(needs)];
    expect(all.filter((p) => !isPermission(p))).toEqual([]);
  });

  it("every registered helper exists", () => {
    const missing = [...Object.keys(WRITE_HELPERS), ...Object.keys(WRITE_OPS)].filter((k) => {
      const [mod, member] = k.split("#");
      const name = member.split(".")[0];
      const method = member.split(".")[1];
      const src = read(mod);
      if (!new RegExp(`export (async )?(function\\*?|const) ${name}\\b|export\\s*/\\*\\*[\\s\\S]*?\\*/\\s*async function ${name}\\b`).test(src)) return true;
      return method ? !new RegExp(`\\n\\s+${method}:`).test(src) && !new RegExp(`async ${method}\\(`).test(src) : false;
    });
    expect(missing).toEqual([]);
  });

  it("(a) every browser file that writes is accounted for", () => {
    const owners = new Set([...Object.keys(WRITE_HELPERS), ...Object.keys(WRITE_OPS)].map((k) => k.split("#")[0]));
    const clientActionModules = new Set(Object.keys(CLIENT_ACTIONS).map((k) => k.split("#")[0]));
    const bad = BROWSER.filter((f) => /\b(apiWrite|runPress)\(/.test(read(f)))
      .filter((f) => !owners.has(f) && !(f in INLINE_WRITES) && !(f in NOT_BROWSER_PRESSES) && !clientActionModules.has(f)
        && !Object.keys(NOT_BROWSER_PRESSES).some((k) => k.startsWith(`${f}#`)));
    expect(bad).toEqual([]);
  });

  it("(a) every write a helper module exports is registered", () => {
    // A helper module's exported functions that send a write (directly, or through its own
    // send/write/post/postJson wrapper) must each be in WRITE_HELPERS (or be an operation dispatcher).
    const wrappers = /\b(apiWrite|runPress|send|write|postJson|postStream|post)\(/;
    const modules = new Set(Object.keys(WRITE_HELPERS).map((k) => k.split("#")[0]).filter((m) => m.startsWith("lib/")));
    const unregistered: string[] = [];
    for (const mod of modules) {
      const src = read(mod);
      const parts = src.split(/\n(?=export )/);
      for (const part of parts) {
        const m = /^export (?:async )?function\*? (\w+)/.exec(part);
        if (!m) continue;
        if (!wrappers.test(part.replace(/^[^{]*\{/, ""))) continue;
        if (/^(get|load|list|fetch|read)/.test(m[1])) continue;
        const k = `${mod}#${m[1]}`;
        if (!(k in WRITE_HELPERS) && !(k in WRITE_OPS) && !NOT_WRITES.has(k)) unregistered.push(k);
      }
      for (const obj of src.matchAll(/export const (\w+) = \{([\s\S]*?)\n\};/g)) {
        for (const mm of obj[2].matchAll(/\n\s+(\w+): \([^)]*\)[^=]*=>\s*\n?\s*(?:dry\()?(send|write)\(/g)) {
          if (mm[0].includes("dry(")) continue;
          const k = `${mod}#${obj[1]}.${mm[1]}`;
          if (!(k in WRITE_HELPERS) && !NOT_WRITES.has(k)) unregistered.push(k);
        }
      }
    }
    expect(unregistered).toEqual([]);
  });

  it("(b) every file that presses a write gates its permission", () => {
    const missing = uses()
      .filter((u) => !BACKGROUND.has(`${u.file} ${u.what}`) && !(u.file in PASSTHROUGH) && !(`${u.file} ${u.what}` in SUBSUMED))
      .filter((u) => u.permission.startsWith("UNREGISTERED") || !gates(read(u.file), u.permission))
      .map((u) => `${u.file} ${u.what} needs a gate for ${u.permission}`);
    expect([...new Set(missing)]).toEqual([]);
  });

  it("the must-haves are covered: run answers, the composer's steps, agent updates", () => {
    const all = uses();
    expect(all.some((u) => u.file === "app/agents/attention-card.tsx" && u.what === "sendRunAnswer")).toBe(true);
    expect(all.some((u) => u.file === "app/workflows/new-project-composer.tsx" && u.what === "post:start")).toBe(true);
    expect(all.filter((u) => u.what === "updateAgent").length).toBeGreaterThan(0);
  });

  it("the kit cards' Test and Turn on are drawn greyed, gated on the permissions their writes need", () => {
    // The press happens in the card (app/skills/ability-card.tsx), not in kit-home, so the card
    // must receive each writing action's permission and gate the button itself with useGate.
    const home = read("app/skills/kit-home.tsx");
    const card = read("app/skills/ability-card.tsx");
    const perms = /CARD_PERMISSIONS: CardPermissions = \{([^}]*)\}/.exec(home)?.[1] ?? "";
    expect(perms).toContain(`test: "${WRITE_HELPERS["lib/kit-api.ts#kitApi.test"]}"`);
    expect(perms).toContain(`turn_on: "${WRITE_HELPERS["lib/kit-api.ts#kitApi.enabled"]}"`);
    expect(home).toMatch(/<AbilityCard[\s\S]{0,300}?permissions=\{CARD_PERMISSIONS\}/);
    expect(card).toMatch(/useGate\(permission\)/);
    expect(card).toMatch(/permissions\?\.\[action\]/);
  });

  it("exemptions are still real (no stale entry hides a new press)", () => {
    const stale = [...BACKGROUND].filter((k) => !read(k.split(" ")[0]).includes(k.split(" ")[1].split(":").pop()!.replace("inline", "apiWrite")));
    expect(stale).toEqual([]);
  });
});
