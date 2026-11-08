/**
 * Every type size in the console comes from the type scale (the --fs-* tokens in app/globals.css),
 * and the scale never drops below the sizes people can read.
 *
 * The project screens (Project overview, Work, Add a project, the New work modal, the brief card,
 * onboarding, started cards, drawers) read a size smaller than the rest of the console: a median of
 * 11px with ~70% of their text at 11px or below, against 14px and 8% on Workflows. Two causes, both
 * held here:
 *  - the scale was copied from the wireframes, whose sizes are drawn small (meta 11.4px, tags
 *    10.6px, labels 10.3px). The tokens now match what the Workflows page renders, and FLOORS keeps
 *    each one from sliding back;
 *  - the new stylesheets hard-coded .5–.66rem instead of using the tokens, so no fix to the scale
 *    reached them. A literal size (rem, px, or an em below 0.8 that shrinks its parent) now fails,
 *    in every stylesheet that is not on the LEGACY list, and a LEGACY file can only lose them.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";
import { declarations } from "./_helpers/css-declarations";

const ROOT = join(__dirname, "..");
const ROOT_PX = 19; // the laptop root (app/layout-scale.css)

/** The floor of each token, in px at the 19px root. Workflows renders body and rows at ~14px,
 *  buttons at ~13.3px, meta at ~12.5px; nothing that is read goes below 12px except a MONO CAPS
 *  label, which reads at 11px because capitals are taller. */
const FLOORS: Record<string, number> = {
  "--fs-display": 24,
  "--fs-page": 20,
  "--fs-title": 15.5,
  "--fs-body": 13.5,
  "--fs-row": 13.5,
  "--fs-ui": 13,
  "--fs-meta": 12.5,
  "--fs-tag": 12,
  "--fs-label": 11,
};

/** Stylesheets outside the console's scale: the marketing page has its own fluid type, and the
 *  scale itself sets the one px root size. */
const OUTSIDE = new Set(["app/launch/launch.module.css", "app/layout-scale.css"]);

/** Rules that size their own text on purpose, with the reason. A selector is allowed when it
 *  starts with one of these. */
const ALLOW: Record<string, string> = {
  // A diagram: the flow chart's nodes are laid out in px (lib/ambient-layout.ts) and its words are
  // sized in em against the chart's own pinned base (FlowCanvas sets it to the laptop root), so
  // the text always fits its box; the whole chart grows by zoom-to-fit, not by the root.
  ".aa-canvas": "the Agents-at-work flow chart: a px-laid-out diagram with em text against its own base",
};

/** Stylesheets written before the scale existed, and how many literal sizes each still has. The
 *  number can only go down: convert a rule to a token and lower it (remove the entry at 0). Every
 *  new stylesheet starts at zero, so it is not on this list.
 */
const LEGACY: Record<string, number> = {
  "app/activity/log.css": 38,
  "app/agents/agent-world.css": 238,
  "app/command-center/cc.css": 87,
  "app/command-center/returning-brief.module.css": 3,
  "app/design-system.css": 169,
  "app/factory/security/security.css": 37,
  "app/globals.css": 698,
  "app/govern/govern.css": 64,
  "app/identity/identity-account.css": 5,
  "app/identity/identity-agents.css": 13,
  "app/identity/identity-app-sign-in.css": 2,
  "app/identity/identity-auth.css": 22,
  "app/identity/identity-people.css": 20,
  "app/identity/identity-roles.css": 13,
  "app/identity/identity-sso.css": 4,
  "app/identity/identity.css": 179,
  "app/improve/improve.css": 42,
  "app/infra/cloud.css": 10,
  "app/infra/deploys.css": 22,
  "app/knowledge/kg.css": 123,
  "app/multirepo/multirepo.css": 70,
  "app/project-archive.module.css": 6,
  "app/research/research.css": 98,
  "app/self-fix/self-fix.css": 26,
  "app/setup/setup.css": 62,
  "app/skills/kit.css": 51,
  "app/workflows/compose.css": 82,
  "app/workflows/editor.css": 16,
  "app/workflows/flow.css": 36,
  "app/workflows/full-page.css": 1,
  "app/workflows/inspector.css": 49,
  "app/workflows/library.css": 29,
  "app/workflows/pipeline.css": 53,
  "components/v2/shell.css": 129,
  "components/v2/templates/templates.css": 47,
  "components/v2/tokens.css": 2,
};

/** The screens this scale was fixed for: they must stay at zero literal sizes. */
const CONVERTED = [
  "app/projects/px.css", "app/projects/new-work.css", "app/projects/brief-card.css",
  "app/projects/onboarding.css", "app/projects/started.css", "app/projects/jump.css",
  "app/projects/work.css", "app/start-project/start-project.css", "app/shell.css", "app/stations.css",
  "components/stuck-work.css", "components/work-agreement.module.css", "app/agent-ops/agent-ops.css",
  "app/factory/factory.css", "app/factory/cockpit.css", "app/infra/infra.css", "app/observability/obs.css",
];

function stylesheets(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (["node_modules", ".next", "tests"].includes(name)) return [];
    if (statSync(path).isDirectory()) return stylesheets(path);
    return name.endsWith(".css") ? [path] : [];
  });
}

const files = [...stylesheets(join(ROOT, "app")), ...stylesheets(join(ROOT, "components"))]
  .map((path) => ({ rel: relative(ROOT, path), css: readFileSync(path, "utf8") }))
  .filter((f) => !OUTSIDE.has(f.rel));

const TOKEN = /^var\(--fs-[a-z]+\)(\s*!important)?$/;
const KEYWORD = /^(inherit|initial|unset|revert|revert-layer)(\s*!important)?$/;
const RELATIVE = /^(\d*\.?\d+)(em|%)(\s*!important)?$/;

/** Is this size written as a number instead of a token? (An em or % of 0.8 or more follows its
 *  parent, which is a token; a smaller one shrinks it below the scale.) */
export function literalSize(prop: string, value: string): boolean {
  const v = value.trim();
  if (prop === "font-size") {
    if (TOKEN.test(v) || KEYWORD.test(v)) return false;
    const rel = v.match(RELATIVE);
    if (rel) return Number(rel[1]) / (rel[2] === "%" ? 100 : 1) < 0.8;
    return true;
  }
  if (prop === "font") {
    // The size is the first length after the style/weight words; a line-height follows a "/".
    const size = v.match(/(?:^|\s)(\d*\.?\d+)(rem|px|em|%)(?=[\s/]|$)/);
    if (!size) return false;
    if (size[2] === "em" || size[2] === "%") return Number(size[1]) / (size[2] === "%" ? 100 : 1) < 0.8;
    return true;
  }
  return false;
}

function literals(css: string) {
  return declarations(css).filter((d) => ["font-size", "font"].includes(d.prop)
    && !Object.keys(ALLOW).some((a) => d.selector.split(",").every((s) => s.trim().startsWith(a)))
    && literalSize(d.prop, d.value));
}

function tokens(): Record<string, number> {
  const globals = readFileSync(join(ROOT, "app/globals.css"), "utf8");
  const out: Record<string, number> = {};
  for (const d of declarations(globals)) {
    if (d.selector !== ":root" || !d.prop.startsWith("--fs-")) continue;
    const m = d.value.match(/^(\d*\.?\d+)rem$/);
    out[d.prop] = m ? Number(m[1]) * ROOT_PX : NaN;
  }
  return out;
}

describe("type is a token", () => {
  it("reads a literal size from a token", () => {
    expect(literalSize("font-size", ".6rem")).toBe(true);
    expect(literalSize("font-size", "11px")).toBe(true);
    expect(literalSize("font-size", "0.6em")).toBe(true);
    expect(literalSize("font-size", "calc(var(--fs-tag) * .8)")).toBe(true);
    expect(literalSize("font-size", "var(--fs-meta)")).toBe(false);
    expect(literalSize("font-size", "var(--fs-tag) !important")).toBe(false);
    expect(literalSize("font-size", "inherit")).toBe(false);
    expect(literalSize("font-size", "0.85em")).toBe(false);
    expect(literalSize("font", "600 .66rem var(--mono)")).toBe(true);
    expect(literalSize("font", "600 var(--fs-label)/1.4 var(--mono)")).toBe(false);
    expect(literalSize("font", "inherit")).toBe(false);
  });

  it("every token sits at or above its floor (px at the 19px root)", () => {
    const t = tokens();
    for (const [name, floor] of Object.entries(FLOORS)) {
      expect(t[name], `${name} is defined in rem on :root`).toBeGreaterThan(0);
      expect(t[name], `${name} = ${t[name]?.toFixed(1)}px, below its ${floor}px floor`).toBeGreaterThanOrEqual(floor - 0.05);
    }
    // No token outside the scale, and the order holds: a title is never smaller than body text.
    expect(Object.keys(t).sort()).toEqual(Object.keys(FLOORS).sort());
    expect(t["--fs-page"]).toBeGreaterThan(t["--fs-title"]);
    expect(t["--fs-title"]).toBeGreaterThan(t["--fs-body"]);
    expect(t["--fs-body"]).toBeGreaterThanOrEqual(t["--fs-ui"]);
    expect(t["--fs-ui"]).toBeGreaterThan(t["--fs-meta"]);
    expect(t["--fs-meta"]).toBeGreaterThanOrEqual(t["--fs-tag"]);
    expect(t["--fs-tag"]).toBeGreaterThan(t["--fs-label"]);
  });

  it("no stylesheet redefines a token (one scale, set once)", () => {
    const found: string[] = [];
    for (const { rel, css } of files) {
      for (const d of declarations(css)) {
        if (d.prop.startsWith("--fs-") && !(rel === "app/globals.css" && d.selector === ":root")) {
          found.push(`${rel}:${d.line} ${d.selector} { ${d.prop}: ${d.value} }`);
        }
      }
    }
    expect(found).toEqual([]);
  });

  it("every type size uses a token, outside the LEGACY stylesheets", () => {
    const bad: string[] = [];
    for (const { rel, css } of files) {
      if (rel in LEGACY) continue;
      for (const d of literals(css)) {
        bad.push(`${rel}:${d.line} ${d.selector} { ${d.prop}: ${d.value} } — use var(--fs-page|title|body|row|ui|meta|tag|label|display)`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("a LEGACY stylesheet only loses literal sizes", () => {
    const grew: string[] = [];
    for (const { rel, css } of files) {
      if (!(rel in LEGACY)) continue;
      const n = literals(css).length;
      if (n > LEGACY[rel]) grew.push(`${rel}: ${n} literal sizes, was ${LEGACY[rel]} — use a var(--fs-*) token`);
      if (n === 0) grew.push(`${rel}: no literal sizes left — remove it from LEGACY`);
    }
    for (const rel of Object.keys(LEGACY)) expect(files.some((f) => f.rel === rel), `${rel} still exists`).toBe(true);
    expect(grew).toEqual([]);
  });

  it("the project screens, Code Tasks and their shared pieces are fully on the scale", () => {
    for (const rel of CONVERTED) {
      expect(rel in LEGACY, `${rel} must not be on LEGACY`).toBe(false);
      expect(files.some((f) => f.rel === rel), rel).toBe(true);
    }
  });

  it("every allowlisted rule still exists (a stale entry would hide a new one)", () => {
    const all = files.map((f) => f.css).join("\n");
    for (const sel of Object.keys(ALLOW)) expect(all, sel).toContain(sel);
  });
});
