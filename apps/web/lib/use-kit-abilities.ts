"use client";

/**
 * The Kit's abilities, for screens outside Skills & MCP that need to say "Not set up" (the
 * Studio's scope picker, its Uses row, a custom step's binding picker). One read per project,
 * shared, and re-read when the window comes back into focus — so an ability set up in another
 * tab shows normally here without a reload (skills-mcp spec J6).
 */
import { useEffect, useState } from "react";

import type { Ability } from "./kit-types";

/** A built-in step's capability, and the ability it needs (services/kit.py CAPABILITY_ABILITY). */
export const CAPABILITY_ABILITY: Record<string, string> = {
  build: "open_prs", retry_build: "open_prs", test: "tests_ci", deploy: "deploy", redeploy: "deploy",
  monitor: "watch_health", document: "render_docs",
};

export function notSetUp(a: Ability | undefined): boolean {
  return Boolean(a && (a.status === "setup" || a.status === "needs_repo"));
}

export const setUpHref = (key: string) => `?area=skills&ability=${encodeURIComponent(key)}`;

const cache = new Map<string, { at: number; abilities: Ability[] }>();
const waiting = new Map<string, Promise<Ability[]>>();
const listeners = new Set<() => void>();

async function fetchAbilities(projectId: string): Promise<Ability[]> {
  const pending = waiting.get(projectId);
  if (pending) return pending;
  const p = (async () => {
    try {
      const r = await fetch(`/api/kit?project_id=${encodeURIComponent(projectId)}&check=none`, { cache: "no-store" });
      if (!r.ok) return cache.get(projectId)?.abilities ?? [];
      const abilities = ((await r.json()).abilities ?? []) as Ability[];
      cache.set(projectId, { at: Date.now(), abilities });
      listeners.forEach((l) => l());
      return abilities;
    } catch {
      return cache.get(projectId)?.abilities ?? [];
    } finally {
      waiting.delete(projectId);
    }
  })();
  waiting.set(projectId, p);
  return p;
}

export function useKitAbilities(projectId: string | null | undefined): Ability[] | null {
  const [, bump] = useState(0);
  useEffect(() => {
    if (!projectId) return;
    const listen = () => bump((n) => n + 1);
    listeners.add(listen);
    const hit = cache.get(projectId);
    if (!hit || Date.now() - hit.at > 30_000) void fetchAbilities(projectId);
    const back = () => { if (document.visibilityState === "visible") void fetchAbilities(projectId); };
    window.addEventListener("focus", back);
    return () => { listeners.delete(listen); window.removeEventListener("focus", back); };
  }, [projectId]);
  return projectId ? cache.get(projectId)?.abilities ?? null : null;
}

/** The ability a skill id or an MCP server name belongs to, by kind. */
export function abilityOf(abilities: Ability[] | null, kind: "skill" | "mcp", item: string): Ability | undefined {
  if (!abilities) return undefined;
  return abilities.find((a) => kind === "skill"
    ? a.tools.some((t) => t.ref === `skill:${item}`)
    : a.tools.some((t) => t.ref.startsWith(`mcp:${item}.`)) || a.mcp.some((m) => m.name === item)
      || (a.mcp_names ?? []).includes(item));
}

/** What a step can pick of one kind, per ability: {ability, items}. A tool the Kit has turned Off
 *  (Merge a PR, by default) is never offered: it would only be refused at run time (K15). */
export function itemsByAbility(abilities: Ability[] | null, kind: "skill" | "mcp"): { ability: Ability; items: string[] }[] {
  return (abilities ?? []).map((a) => ({
    ability: a,
    items: kind === "skill"
      ? a.tools.filter((t) => t.ref.startsWith("skill:") && t.policy !== "off").map((t) => t.ref.slice(6))
      : [...new Set([...(a.mcp_names ?? []), ...a.mcp.map((m) => m.name),
                     ...a.tools.filter((t) => t.ref.startsWith("mcp:")).map((t) => t.ref.slice(4).split(".")[0])])],
  })).filter((g) => g.items.length > 0);
}

/**
 * What a chip says for a skill id or server name (K15): the Kit's words, never the raw id. A
 * skill is its tool row's label (github.create_pr_real → "Open a PR"); a server backing exactly
 * one ability is that ability (context7 → "Library docs"); anything else stays as it is named.
 */
export function itemLabel(abilities: Ability[] | null, kind: "skill" | "mcp", item: string): string {
  if (!abilities) return item;
  if (kind === "skill") {
    for (const a of abilities) {
      const row = a.tools.find((t) => t.ref === `skill:${item}`);
      if (row?.label) return row.label;
    }
    return item;
  }
  const backs = abilities.filter((a) => a.mcp.some((m) => m.name === item) || (a.mcp_names ?? []).includes(item));
  return backs.length === 1 ? backs[0].label : item;
}
