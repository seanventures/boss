/**
 * One identity, everywhere (spec §2.1, S-kit acceptance: "every role_key and alias resolves to a
 * non-fallback face").
 *
 * The engine's alias table (agent_world_types.ALIASES) is read straight from the Python file,
 * so a seat, captain id or stage role the engine maps and the web does not fails here rather
 * than showing up faceless on one screen.
 */
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { specForRole } from "../lib/agent-avatar";
import {
  ALIASES, agentIdentity, hasKnownFace, loadIdentities, primeIdentities, resetIdentities, resolveRoleKey,
} from "../lib/agent-identity";
import { agentIdentity as fromContract, avatarSvg as svgFromContract } from "../lib/agent-world-types";
import { avatarSvg } from "../lib/agent-avatar";
import { CAST, CAST_BY_KEY } from "../lib/avatar-kit/cast";

const PY = readFileSync(resolve(__dirname, "../../../engine/boss_engine/services/agent_world_types.py"), "utf8");

function pyBlock(name: string): string {
  const at = PY.indexOf(`${name}:`);
  expect(at, name).toBeGreaterThan(-1);
  const open = PY.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < PY.length; i++) {
    if (PY[i] === "{") depth++;
    if (PY[i] === "}" && --depth === 0) return PY.slice(open, i + 1).replace(/#[^\n]*/g, "");
  }
  throw new Error(`unterminated ${name}`);
}

const PY_ALIASES: Record<string, string> = Object.fromEntries(
  [...pyBlock("ALIASES").matchAll(/"([^"]+)"\s*:\s*"([^"]+)"/g)].map((m) => [m[1], m[2]]));
const CANONICAL = [...pyBlock("CANONICAL_ROLE_KEYS").matchAll(/"([^"]+)"/g)].map((m) => m[1]);

/** The §0.1 cast and the four archetypes: the characters drawn by hand. */
const HAND = ["engineering_captain", "backend_engineer", "frontend_engineer", "operations_engineer", "sre", "pm",
  "chief_architect", "adversarial_reviewer", "security_reviewer", "qa", "release_manager", "design_critic",
  "docs_writer", "finops_sentinel", "software_engineer",
  "first_principles_researcher", "visionary_founder", "relentless_operator", "teacher_who_simplifies"];

afterEach(() => resetIdentities());

describe("the web speaks the engine's one namespace", () => {
  it("mirrors the engine's alias table exactly", () => {
    expect(Object.keys(PY_ALIASES).length).toBeGreaterThan(30);
    expect(ALIASES).toEqual(PY_ALIASES);
  });

  it("has a face drawn for every canonical role key, and nothing it should not", () => {
    expect(CANONICAL.length).toBeGreaterThan(30);
    expect(CAST.map((c) => c.roleKey).sort()).toEqual([...CANONICAL].sort());
  });

  it("draws every §0.1 role and archetype by hand", () => {
    for (const k of HAND) expect(CAST_BY_KEY[k]?.designed, k).toBe("hand");
    for (const k of HAND) expect(CAST_BY_KEY[k]?.callsign, k).toBeTruthy();
    for (const k of ["first_principles_researcher", "visionary_founder", "relentless_operator", "teacher_who_simplifies"]) {
      expect(CAST_BY_KEY[k].archetype, k).toBe(true);
      expect(CAST_BY_KEY[k].name.startsWith("The "), k).toBe(true);
    }
  });

  it.each(Object.entries(PY_ALIASES))("alias %s lands on %s's own face", (alias, key) => {
    expect(hasKnownFace(alias)).toBe(true);
    const who = agentIdentity(alias);
    expect(who.roleKey).toBe(key);
    expect(who.avatar).toEqual(CAST_BY_KEY[key].avatar);
  });

  it("resolves every canonical key, any casing, and each display name and callsign", () => {
    for (const c of CAST) {
      expect(resolveRoleKey(c.roleKey)).toBe(c.roleKey);
      expect(resolveRoleKey(c.roleKey.toUpperCase())).toBe(c.roleKey);
      expect(resolveRoleKey(` ${c.name} `)).toBe(c.roleKey);
      if (c.callsign) expect(resolveRoleKey(c.callsign)).toBe(c.roleKey);
    }
  });

  it("resolves the display name every shipped role YAML gives itself", () => {
    const dir = resolve(__dirname, "../../../packages/specpack/roles");
    for (const f of readdirSync(dir).filter((x) => x.endsWith(".yaml"))) {
      const name = /display_name:\s*(.+)/.exec(readFileSync(resolve(dir, f), "utf8"))?.[1]?.trim();
      expect(name, f).toBeTruthy();
      expect(resolveRoleKey(name!), f).toBe(f.replace(/\.yaml$/, ""));
    }
  });

  it("never returns a blank for a name nobody knows: a stable seeded face instead", () => {
    const a = agentIdentity("shift_swap_auditor");
    expect(a.roleKey).toBe("shift_swap_auditor");
    expect(a.name).toBe("Shift swap auditor");
    expect(a.avatar).toEqual(specForRole("shift_swap_auditor"));
    expect(agentIdentity("shift_swap_auditor")).toEqual(a);
    expect(hasKnownFace("shift_swap_auditor")).toBe(false);
    expect(() => agentIdentity("")).not.toThrow();
  });

  it("is the function the frozen contract exports, not the phase-0 stub", () => {
    expect(fromContract("qa").roleKey).toBe("qa");
    expect(svgFromContract(specForRole("qa"), { size: 24 })).toBe(avatarSvg(specForRole("qa"), { size: 24 }));
  });
});

describe("the install's own identities win once loaded", () => {
  it("takes the persona's callsign and a custom agent's own face", () => {
    primeIdentities([
      { role_key: "qa", agent_id: "agt_7f3k2q4m", display_name: "QA Lead", callsign: "Vee", tagline: "Prove it.", avatar: { kit: "boss-v1", seed: "agt_7f3k2q4m" } },
      { role_key: "shift_swap_auditor", agent_id: "agt_aaaa", display_name: "Shift Swap Auditor", avatar: { kit: "boss-v1", seed: "agt_aaaa" }, lifecycle: "paused" },
    ]);
    const qa = agentIdentity("QA / Test");
    expect(qa).toMatchObject({ roleKey: "qa", agentId: "agt_7f3k2q4m", callsign: "Vee", tagline: "Prove it." });
    // A bare seed on a shipped role is a default, not a reroll: Vera keeps her magnifier.
    expect(qa.avatar).toEqual(CAST_BY_KEY.qa.avatar);
    expect(resolveRoleKey("vee")).toBe("qa");
    const custom = agentIdentity("Shift Swap Auditor");
    expect(custom).toMatchObject({ roleKey: "shift_swap_auditor", lifecycle: "paused" });
    expect(custom.avatar.seed).toBe("agt_aaaa");
  });

  it("keeps an operator's picked parts on a shipped role", () => {
    primeIdentities([{ role_key: "qa", avatar: { kit: "boss-v1", seed: "qa", palette: "purple" } }]);
    expect(agentIdentity("qa").avatar).toEqual({ ...CAST_BY_KEY.qa.avatar, palette: "purple" });
  });

  it("uses the server's aliases for seats it adds later", () => {
    primeIdentities([], { "Night Shift Lead": "sre" });
    expect(agentIdentity("night shift lead").roleKey).toBe("sre");
  });

  it("loads from /v1/agents/identities once per project, and fails soft", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ items: [{ role_key: "pm", callsign: "Jun" }], aliases: {} })));
    await loadIdentities("p1", fetcher as unknown as typeof fetch);
    await loadIdentities("p1", fetcher as unknown as typeof fetch);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String((fetcher.mock.calls[0] as unknown[])[0])).toBe("/v1/agents/identities?project_id=p1");
    expect(agentIdentity("pm").callsign).toBe("Jun");

    resetIdentities();
    await loadIdentities("p2", (async () => { throw new Error("down"); }) as unknown as typeof fetch);
    expect(agentIdentity("pm").callsign).toBe("Juno");
  });
});
