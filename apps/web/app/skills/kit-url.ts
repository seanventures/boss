/**
 * Skills & MCP's place in the URL (spec §5.1 deep links): `&tab=kit|who|library|templates|rules`,
 * `&ability=<key>` (the Kit with that drawer open), `&lib=skills|mcp`, `&skill=<key>`,
 * `&mcp=<id>`, `&role=<key>` (Who can, that agent's row), `&setup=1` (Essentials forced open).
 * Read once on load and written with replaceState, so a link or a reload lands on the same view.
 */
export type KitTab = "kit" | "who" | "library" | "templates" | "rules";
export type KitParams = {
  tab: KitTab; ability: string | null; lib: "skills" | "mcp"; skill: string | null;
  mcp: string | null; role: string | null; setup: boolean;
};

export function readKitParams(search?: string): KitParams {
  const p = new URLSearchParams(search ?? (typeof window !== "undefined" ? window.location.search : ""));
  const tab = p.get("tab");
  const lib = p.get("lib");
  const ability = p.get("ability");
  return {
    tab: tab === "who" || tab === "library" || tab === "templates" || tab === "rules" ? tab : (p.get("skill") || p.get("mcp") || lib) ? "library" : "kit",
    ability: ability && /^[a-z][a-z0-9_]{1,40}$/.test(ability) ? ability : null,
    lib: lib === "mcp" || p.get("mcp") ? "mcp" : "skills",
    skill: p.get("skill"), mcp: p.get("mcp"), role: p.get("role"), setup: p.get("setup") === "1",
  };
}

export function writeKitParams(change: Partial<Record<"tab" | "ability" | "lib" | "skill" | "mcp" | "role" | "setup", string | null>>): void {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(change)) {
      if (value === null || value === undefined || value === "") url.searchParams.delete(key);
      else url.searchParams.set(key, value);
    }
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  } catch { /* a sandboxed frame: the view still works, the URL just does not follow */ }
}
