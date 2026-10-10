/**
 * A page's width and its type size come from the page scale (app/layout-scale.css), never from a
 * number written into one page's CSS.
 *
 * On a 2560px monitor the Command Center sat in a 1240px column of 13px text with half the
 * screen empty, because ~20 page containers each hard-coded a px max-width (1240, 1120, 960,
 * 760…) and ~950 type sizes were px, which no root size could reach. Every page container now
 * uses a --page-max* token that is the old width at ≤1440px and grows on a wider screen, and
 * every type size is rem so the root size grows it.
 *
 * Enforced for every console stylesheet, so a new page cannot bring the narrow column back:
 *  - a `max-width` above 640px written in px fails, unless the rule is a component with its own
 *    size (a sheet, a drawing) named in INTRINSIC below with the reason;
 *  - a `font-size`, `font` or `line-height` in px fails (use rem: 19px = 1rem at ≤1600px).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");

/** Components whose width is their own, not the page's: they keep a px width on purpose. */
const INTRINSIC: Record<string, string> = {
  ".askboss-sheet-body": "the Ask BOSS sheet: a side sheet keeps its own measure",
  ".ag-ln-stage": "a drawing (the lineage stage) sized to its artwork",
  ".ag-swarm-stage": "a drawing (the swarm stage) sized to its artwork",
  ".v2-hive-svg": "a drawing (the hive SVG) sized to its artwork",
};

/** Stylesheets outside the console's scale: the marketing page has its own fluid type, and the
 *  scale itself sets the one px root size. */
const OUTSIDE = new Set(["app/launch/launch.module.css", "app/layout-scale.css"]);

function stylesheets(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (["node_modules", ".next", "tests"].includes(name)) return [];
    if (statSync(path).isDirectory()) return stylesheets(path);
    return name.endsWith(".css") ? [path] : [];
  });
}

type Decl = { selector: string; prop: string; value: string; line: number };

/** Every declaration with the selector of the rule it sits in (at-rule preludes are not
 *  declarations, so `@media (max-width: 900px)` is never read as a width). */
export function declarations(css: string): Decl[] {
  const src = css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));
  const out: Decl[] = [];
  const stack: string[] = [];
  let start = 0;
  const breaks: number[] = [];
  for (let i = src.indexOf("\n"); i >= 0; i = src.indexOf("\n", i + 1)) breaks.push(i);
  const lineAt = (i: number) => {
    let lo = 0, hi = breaks.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (breaks[mid] < i) lo = mid + 1; else hi = mid; }
    return lo + 1;
  };
  const flush = (end: number) => {
    const chunk = src.slice(start, end);
    let offset = start;
    for (const part of chunk.split(";")) {
      const colon = part.indexOf(":");
      const sel = stack[stack.length - 1] ?? "";
      if (colon > 0 && sel && !sel.startsWith("@")) {
        const prop = part.slice(0, colon).trim().toLowerCase();
        if (/^[a-z-]+$/.test(prop)) {
          out.push({ selector: sel, prop, value: part.slice(colon + 1).trim(),
            line: lineAt(offset + part.search(/\S/)) });
        }
      }
      offset += part.length + 1;
    }
  };
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (ch === "{") {
      const prelude = src.slice(start, i);
      // A nested rule's prelude follows the last declaration of its parent.
      const cut = prelude.lastIndexOf(";");
      if (cut >= 0) { flush(start + cut); }
      stack.push(prelude.slice(cut + 1).trim().replace(/\s+/g, " "));
      start = i + 1;
    } else if (ch === "}") {
      flush(i);
      stack.pop();
      start = i + 1;
    }
  }
  return out;
}

const files = [...stylesheets(join(ROOT, "app")), ...stylesheets(join(ROOT, "components"))]
  .map((path) => ({ rel: relative(ROOT, path), css: readFileSync(path, "utf8") }))
  .filter((f) => !OUTSIDE.has(f.rel));

describe("a page's width is a layout token", () => {
  it("reads declarations, not at-rule preludes", () => {
    const d = declarations("@media (max-width: 900px) { .a { max-width: 1000px; color: red } }\n.b { max-width: 50% }");
    expect(d.map((x) => [x.selector, x.prop, x.value])).toEqual([
      [".a", "max-width", "1000px"], [".a", "color", "red"], [".b", "max-width", "50%"]]);
  });

  it("scans the console's stylesheets", () => {
    expect(files.length).toBeGreaterThan(20);
    expect(files.some((f) => f.rel === "app/command-center/cc.css")).toBe(true);
  });

  it("no page container sets a px max-width above 640px", () => {
    const wide: string[] = [];
    for (const { rel, css } of files) {
      for (const d of declarations(css)) {
        if (d.prop !== "max-width") continue;
        const big = [...d.value.matchAll(/(\d+(?:\.\d+)?)px/g)].some((m) => Number(m[1]) > 640);
        if (!big) continue;
        const selectors = d.selector.split(",").map((s) => s.trim());
        if (selectors.every((s) => s in INTRINSIC)) continue;
        wide.push(`${rel}:${d.line} ${d.selector} { max-width: ${d.value} } — use var(--page-max), ` +
          `--page-max-compact, --page-max-mid or --page-max-narrow (app/layout-scale.css), or name ` +
          `a component with its own size in INTRINSIC with the reason`);
      }
    }
    expect(wide).toEqual([]);
  });

  it("every allowlisted component still exists (a stale entry would hide a new page)", () => {
    const all = files.map((f) => f.css).join("\n");
    for (const sel of Object.keys(INTRINSIC)) expect(all, sel).toContain(sel);
  });

  it("no type size is px, so the root size reaches every page", () => {
    const px: string[] = [];
    for (const { rel, css } of files) {
      for (const d of declarations(css)) {
        if (!["font-size", "font", "line-height"].includes(d.prop)) continue;
        if (/(^|[^\w.-])\d+(\.\d+)?px\b/.test(d.value) && !/^0px$/.test(d.value)) {
          px.push(`${rel}:${d.line} ${d.selector} { ${d.prop}: ${d.value} } — write it in rem (19px = 1rem)`);
        }
      }
    }
    expect(px).toEqual([]);
  });

  it("no grid column is px, so a column of text grows with its text", () => {
    // A 66px time column held "16:17:59" at 19px root and overlapped the level beside it once
    // the type grew. A gap subtracted inside calc() stays px, like the gap it matches.
    const px: string[] = [];
    for (const { rel, css } of files) {
      for (const d of declarations(css)) {
        if (!["grid-template-columns", "grid-auto-columns", "grid-template"].includes(d.prop)) continue;
        let v = d.value;
        for (let at = v.indexOf("calc("); at >= 0; at = v.indexOf("calc(")) {
          let depth = 0, end = at + 4;
          for (; end < v.length; end++) {
            if (v[end] === "(") depth++;
            else if (v[end] === ")" && --depth === 0) break;
          }
          v = v.slice(0, at) + v.slice(end + 1);
        }
        if (/(^|[^\w.-])\d+(\.\d+)?px\b/.test(v)) {
          px.push(`${rel}:${d.line} ${d.selector} { ${d.prop}: ${d.value} } — write the track in rem`);
        }
      }
    }
    expect(px).toEqual([]);
  });

  it("the scale is loaded and every page token it promises is defined", () => {
    const layout = readFileSync(join(ROOT, "app/layout.tsx"), "utf8");
    expect(layout).toContain('import "./layout-scale.css"');
    const scale = readFileSync(join(ROOT, "app/layout-scale.css"), "utf8");
    const used = new Set(files.flatMap((f) => [...f.css.matchAll(/var\((--(?:page|shell)-[a-z-]+)\)/g)].map((m) => m[1])));
    expect(used.size).toBeGreaterThan(3);
    for (const token of used) expect(scale, token).toMatch(new RegExp(`${token}:`));
    // The root size is set once, there; a second px root size elsewhere would fight it.
    for (const { rel, css } of files) {
      for (const d of declarations(css)) {
        if (d.prop === "font-size" && /(^|,)\s*html\s*($|,)/.test(d.selector)) {
          throw new Error(`${rel}:${d.line} sets the root size; it lives in app/layout-scale.css`);
        }
      }
    }
  });
});
