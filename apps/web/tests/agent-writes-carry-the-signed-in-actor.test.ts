/**
 * Every console write to an agent is made as the signed-in member — never a literal actor.
 *
 * THE INCIDENT. The Create-agent wizard (Agents page and Agent Studio) posted `actor: "vp_agents"`
 * for the Studio definition, version, validate and publish. On an authed install the console's
 * key is bound to the principal 'ceo', and 'vp_agents' does not carry 'ceo' in its role_keys, so
 * require_authenticated_actor_binding refused every one of them with 403 — after POST /v1/roles
 * had already committed, leaving an orphan role per attempt. Grants from the agent page and the
 * workflow builder sent the literal 'executive_console', so the audit trail named the console
 * instead of the person.
 *
 * THE RULE, FOR THE WHOLE CLASS. A file that writes an agent — the agent resource, its skill or
 * MCP grants, Agent Studio, or the skills proxy that grants — takes the actor from the page and
 * passes it through. This scan FINDS those files by what they call (so a new screen is covered
 * the day it is written), checks the list against the reviewed one below (so the set cannot
 * shrink or grow unnoticed), and fails on any literal actor in any of them.
 */
import { readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const ROOT = process.cwd();

/** What makes a file an AGENT WRITER: it reaches one of these. */
const AGENT_WRITE_SURFACE = [
  /\/v1\/roles["'`?]/,            // register a role (and its list read)
  /\/v1\/role-skills/,            // give / take a skill
  /\/v1\/role-mcp-servers/,       // give / take an MCP server
  /\/v1\/agents\b/,               // the agent resource: create, edit, delete
  /\/v1\/agent-studio\//,         // Studio profile writes
  /["'`]\/api\/skills["'`]/,      // the skills proxy, which also grants
  /from "\.\.\/lib\/agents"|from "\.\/lib\/agents"|from "\.\/agents"/,
  /from "\.\/agent-studio-actions"/,
  /from "\.\.\/lib\/agent-wizard"/,
];

/** Every console file that writes an agent today, reviewed. A new one must be added here — and
 *  pass the same check — rather than slip in unreviewed. */
const KNOWN_AGENT_WRITERS = [
  "app/agent-creation-wizard.tsx",
  "app/agent-detail-ui.tsx",
  "app/agent-quick-create.tsx",
  "app/agent-studio-actions.ts",
  "app/agent-studio-ui.tsx",
  "app/api/agent/route.ts",
  "app/api/skills/route.ts",
  "app/delivery-workflows-ui.tsx",
  // The shared MCP panel (skills-mcp stream): removing a server an agent holds releases that
  // agent's grant through /api/skills, so it is an agent writer too — reviewed at integration.
  "app/mcp-servers-ui.tsx",
  "app/skills-ui.tsx",
  "app/workforce-ui.tsx",
  "lib/agent-studio.ts",
  "lib/agents.ts",
];

/** Builds the bodies the writers above send, so it is held to the same rule. */
const BODY_BUILDERS = ["lib/agent-wizard.ts"];

/** Files that write agents AND other things owned elsewhere. Only their agent write sites are
 *  held to this rule here; the rest of the file belongs to its own area's checks. */
const SHARED: Record<string, string> = {
  "app/delivery-workflows-ui.tsx":
    "the workflow builder; its agent write is the 'Give <skill> to <captain>' grant on a step "
    + "row. Its deployment writes are the Infra area's.",
};

/** A literal identity in place of the signed-in one, in any of the shapes it has taken. */
const LITERAL_ACTOR = [
  /\bactor\s*:\s*["'`][a-z_]+["'`]/,          // { actor: "vp_agents" }
  /\bactor\s*=\s*["'`][a-z_]+["'`]/,          // function f(actor = "ceo")
  /\bactor\s*\?\?\s*["'`][a-z_]+["'`]/,       // body.actor ?? "executive_console"
  /\bactor\s*\|\|\s*["'`][a-z_]+["'`]/,       // actor || "operator"
  /\bactor=["'][a-z_]+["']/,                  // <X actor="executive_console" />
  /\bowner\s*:\s*["'`]vp_agents["'`]/,        // owner: "vp_agents"
  /["'`]vp_agents["'`]/,                      // the literal that 403'd, anywhere at all
];

function literalActors(code: string): string[] {
  return LITERAL_ACTOR.map((pattern) => code.match(pattern)?.[0]).filter(Boolean) as string[];
}

/** The text of each agent write in a shared file: from where it names the agent surface to the
 *  end of the request it builds (a request body follows its URL within a few lines). */
function writeSites(code: string): string {
  const sites: string[] = [];
  for (const pattern of AGENT_WRITE_SURFACE) {
    const global = new RegExp(pattern.source, "g");
    for (const m of code.matchAll(global)) sites.push(code.slice(m.index ?? 0, (m.index ?? 0) + 600));
  }
  return sites.join("\n");
}

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(path);
  }
  return out;
}

const writers = ["app", "lib", "components"]
  .flatMap((dir) => sourceFiles(join(ROOT, dir)))
  .map((path) => ({ rel: relative(ROOT, path), code: codeOnly(path) }))
  .filter(({ code }) => AGENT_WRITE_SURFACE.some((pattern) => pattern.test(code)));

describe("agent writes carry the signed-in actor", () => {
  it("the set of files that write agents is the reviewed one", () => {
    expect(writers.map((w) => w.rel).sort()).toEqual([...KNOWN_AGENT_WRITERS].sort());
  });

  it.each(KNOWN_AGENT_WRITERS)("%s sends no literal actor", (rel) => {
    const file = writers.find((w) => w.rel === rel);
    expect(file, `${rel} no longer writes agents; take it off the list`).toBeDefined();
    const hits = literalActors(rel in SHARED ? writeSites(file!.code) : file!.code);
    expect(hits, `${rel} names an actor instead of passing the signed-in one`).toEqual([]);
  });

  it.each(BODY_BUILDERS)("%s builds no body with a literal actor", (rel) => {
    expect(literalActors(codeOnly(join(ROOT, rel)))).toEqual([]);
  });

  it("a shared file is still found by its agent write, so the window check has work to do", () => {
    for (const rel of Object.keys(SHARED)) {
      const file = writers.find((w) => w.rel === rel);
      expect(file && writeSites(file.code).length, rel).toBeGreaterThan(0);
    }
  });

  it("the agent screens are handed the signed-in actor by the page", () => {
    const page = codeOnly(join(ROOT, "app/page.tsx"));
    expect(page).toMatch(/<WorkforceArea[^>]*actor=\{activeActorId\}/);
    expect(page).toMatch(/<AgentStudio[\s\S]{0,200}actor=\{activeActorId\}/);
  });
});
