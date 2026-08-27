import { describe, it, expect } from "vitest";

import { codeOnly, cssOnly, tsxFiles } from "./_helpers/source";

/**
 * Things CSS accepts in silence.
 *
 * The v2 shell had `animation: v2-pulse 2.4s ...` on the Hive halo and no `@keyframes v2-pulse`
 * anywhere in the file. That is not an error -- the declaration parses, applies, and does
 * nothing. The halo sat perfectly still, which is indistinguishable from an idle agent, so the
 * one thing the animation was there to say ("this agent is working RIGHT NOW") was never said.
 * Nothing caught it: the screen rendered, the tests passed, the reference check passed.
 *
 * These rules are cheap and each one corresponds to a defect that reaches production looking
 * exactly like correct output.
 */

/**
 * BOTH v2 stylesheets, concatenated, because that is how the browser sees them: one document,
 * one global namespace for @keyframes and custom properties. Checking shell.css alone is what
 * produced the mistake this file exists to prevent -- v2-pulse looked undefined (it lives in
 * tokens.css), so a second definition was added under the same name, which does not create a
 * new animation but REPLACES the existing one for every element already using it.
 */
const FILES = ["components/v2/tokens.css", "components/v2/shell.css"];
const CSS = FILES.map(cssOnly).join("\n");

/** Names used by an `animation` / `animation-name` declaration. */
function animationNames(css: string): string[] {
  const out = new Set<string>();
  for (const m of css.matchAll(/animation(?:-name)?\s*:\s*([^;}]+)[;}]/g)) {
    for (const part of m[1].split(",")) {
      // The name is the one token that is not a time, a count, or a timing function.
      for (const tok of part.trim().split(/\s+/)) {
        if (/^[a-zA-Z_-][\w-]*$/.test(tok)
            && !/^(none|infinite|alternate|forwards|backwards|both|normal|reverse|running|paused|linear|ease|ease-in|ease-out|ease-in-out|step-start|step-end)$/.test(tok)
            && !/^(cubic-bezier|steps)/.test(tok)) {
          out.add(tok);
        }
      }
    }
  }
  return [...out];
}

describe("v2 stylesheet integrity", () => {
  it("every animation it names is actually defined", () => {
    const defined = new Set(
      [...CSS.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]),
    );
    const used = animationNames(CSS);
    expect(used.length).toBeGreaterThan(0);          // or this test proves nothing
    expect(used.filter((n) => !defined.has(n))).toEqual([]);
  });

  it("no two rules define the same animation name", () => {
    // Not a style question. @keyframes is a single global namespace across every stylesheet on
    // the page, so a duplicate name silently retargets every existing user of it -- here, the
    // running-agent status dot, which lost its own motion and faded to opacity .1.
    const names = [...CSS.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]);
    const dupes = names.filter((n, i) => names.indexOf(n) !== i);
    expect(dupes).toEqual([]);
    expect(names.length).toBeGreaterThan(1);
  });

  it("every animation can be turned off by prefers-reduced-motion", () => {
    // Motion is a signal on these screens, not decoration. For a reader who has asked the OS to
    // stop animating things it becomes noise they cannot escape, and status has to stay
    // legible without it — which it does, because colour and text carry it too.
    const reduced = CSS.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/g)?.join("\n") ?? "";
    expect(reduced).toBeTruthy();

    const defined = [...CSS.matchAll(/@keyframes\s+([\w-]+)/g)].map((m) => m[1]);
    // Every keyframe that MOVES something (not just colour) must be reachable by the
    // reduced-motion block through one of the selectors it disables.
    const movers = defined.filter((n) => {
      const block = CSS.match(new RegExp(`@keyframes\\s+${n}\\s*\\{(?:[^{}]|\\{[^{}]*\\})*\\}`))?.[0] ?? "";
      return /transform:|translate|scale/.test(block);
    });
    expect(movers.length).toBeGreaterThan(0);
    for (const n of movers) {
      const users = [...CSS.matchAll(new RegExp(`([^{}]+)\\{[^{}]*animation[^;}]*\\b${n}\\b`, "g"))]
        .map((m) => m[1].trim().split("\n").pop()!.trim());
      for (const sel of users) {
        // Strip the attribute and pseudo parts: a rule disabling `.v2-dot` covers
        // `.v2-dot[data-status="RUNNING"]`, and requiring the exact string would fail against
        // a stylesheet that is in fact correct.
        const head = sel.split(",")[0].trim().split(/[\s>]/).pop()!
          .replace(/\[[^\]]*\]/g, "").replace(/:.*$/, "");
        expect(reduced, `${n} is used by ${sel} but reduced-motion does not disable it`)
          .toContain(head);
      }
    }
  });

  it("every SVG shape that scales sets transform-box", () => {
    // An SVG element transforms about the VIEWPORT origin by default, so a scale() meant to
    // breathe in place instead drags the shape across the canvas. HTML elements do not have
    // this problem, so the rule must apply to SVG shapes only -- asserting it everywhere
    // flagged the status dot, which is a <span>.
    //
    // Which classes are SVG is read from the components, not guessed from the class name.
    const svgClasses = new Set<string>();
    for (const file of tsxFiles("components/v2")) {
      const src = codeOnly(file);
      for (const m of src.matchAll(/<(circle|path|rect|ellipse|polygon|line|g|text)\b[^>]*className="([^"]+)"/g)) {
        for (const c of m[2].split(/\s+/)) svgClasses.add(c);
      }
    }
    expect(svgClasses.size).toBeGreaterThan(0);   // or this test proves nothing

    const scaling = new Set(
      [...CSS.matchAll(/@keyframes\s+([\w-]+)\s*\{(?:[^{}]|\{[^{}]*\})*\}/g)]
        .filter((m) => /transform:\s*scale/.test(m[0]))
        .map((m) => m[1]),
    );

    const offenders: string[] = [];
    for (const cls of svgClasses) {
      for (const m of CSS.matchAll(new RegExp(`\\.${cls}\\b[^{]*\\{([^}]*)\\}`, "g"))) {
        const usesScaling = [...scaling].some((n) =>
          new RegExp(`animation(?:-name)?:[^;}]*\\b${n}\\b`).test(m[1]));
        if (!usesScaling) continue;
        const own = [...CSS.matchAll(new RegExp(`\\.${cls}\\b[^{]*\\{([^}]*)\\}`, "g"))]
          .map((x) => x[1]).join(" ");
        if (!/transform-box:\s*fill-box/.test(own)) offenders.push(cls);
      }
    }
    expect(offenders).toEqual([]);
  });
});
