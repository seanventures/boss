/**
 * A GATED CONTROL KEEPS ITS REFUSAL TOOLTIP (round 3 role walk). useGate's props carry, when the
 * person lacks the permission, `title` = "Needs “…” · ask an Admin" — and three Repo-and-models
 * controls spread them BEFORE their own title, so the caller's tip replaced the reason: a Viewer
 * saw a disabled select that said "The pipeline profile, as floors." and not what it needs.
 * The spread must come after any title in the same tag (when allowed the gate sets no title, so
 * the caller's tip shows).
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    if (n === "node_modules" || n.startsWith(".")) return [];
    return statSync(p).isDirectory() ? files(p) : p.endsWith(".tsx") ? [p] : [];
  });
}

/** Index of the `>` that closes the opening tag the spread at `from` sits in. */
function tagEnd(s: string, from: number): number {
  let depth = 0;
  let quote: string | null = null;
  for (let i = from; i < s.length; i += 1) {
    const c = s[i];
    if (quote) { if (c === quote && s[i - 1] !== "\\") quote = null; continue; }
    if (c === '"' || c === "'" || c === "`") quote = c;
    else if (c === "{") depth += 1;
    else if (c === "}") depth -= 1;
    else if (c === ">" && depth === 0) return i;
  }
  return s.length;
}

describe("gated controls", () => {
  it("never put their own title after a gate's props", () => {
    const bad: string[] = [];
    for (const f of [...files(join(ROOT, "app")), ...files(join(ROOT, "components"))]) {
      const s = readFileSync(f, "utf8");
      for (const m of s.matchAll(/\{\.\.\.(\w+)\.props\}/g)) {
        const rest = s.slice(m.index! + m[0].length, tagEnd(s, m.index! + m[0].length));
        if (/(^|\s)title=/.test(rest)) bad.push(`${f.slice(ROOT.length + 1)}:${s.slice(0, m.index!).split("\n").length} (${m[1]})`);
      }
    }
    expect(bad).toEqual([]);
  });
});
