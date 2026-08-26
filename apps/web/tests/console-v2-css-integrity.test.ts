import { describe, it, expect } from "vitest";

import { cssOnly } from "./_helpers/source";

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

const CSS = cssOnly("components/v2/shell.css");

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

  it("anything that scales inside the SVG scales about its own centre", () => {
    // An SVG element transforms about the VIEWPORT origin unless told otherwise, so a
    // scale() meant to breathe in place instead throws the shape across the canvas.
    for (const m of CSS.matchAll(/@keyframes\s+([\w-]+)\s*\{([^}]*\{[^}]*\}\s*)*\}/g)) {
      if (!/transform:\s*scale/.test(m[0])) continue;
      const users = [...CSS.matchAll(new RegExp(`([\\w.\\[\\]="'-]+)\\s*\\{[^}]*animation[^;}]*${m[1]}`, "g"))];
      for (const u of users) {
        const selector = u[1].replace(/\[[^\]]*\]/, "");
        const block = CSS.match(new RegExp(`\\${selector}\\s*\\{[^}]*\\}`));
        expect(block?.[0] ?? "", `${selector} scales but sets no transform-box`)
          .toMatch(/transform-box:\s*fill-box/);
      }
    }
  });
});
