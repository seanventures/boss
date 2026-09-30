/**
 * A `\uXXXX` in a JSX attribute is six characters, not a character.
 *
 * The composer's placeholder shipped reading, on screen:
 *
 *     Describe what to build — or pick Ask from the menu to just ask…
 *
 * A JSX attribute value is not a JavaScript string literal — nothing un-escapes it — so the
 * backslash sequence renders as itself. The same text inside `{"..."}` or a ternary IS a string
 * literal and does un-escape, which is why the identical sequence is correct four other places
 * in this codebase and wrong only here.
 *
 * Caught by looking at the page. Worth a test because the two cases are indistinguishable by
 * eye in a diff, and this one reached production.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/** Every attribute value written as a plain quoted string, per file. */
function attributeStrings(src: string): string[] {
  // `foo="..."` — deliberately not `foo={...}`, which is an expression and un-escapes normally.
  return [...src.matchAll(/\s[a-zA-Z-]+="([^"\n]*)"/g)].map((m) => m[1]);
}

describe("markup does not print its own escape sequences", () => {
  const files = readdirSync("app").filter((f) => f.endsWith(".tsx"));

  it("finds files to check", () => {
    expect(files.length).toBeGreaterThan(20);
  });

  for (const file of files) {
    it(`${file} has no \\uXXXX inside a JSX attribute`, () => {
      const offenders = attributeStrings(readFileSync(join("app", file), "utf8"))
        .filter((v) => /\\u[0-9a-fA-F]{4}/.test(v));
      expect(offenders, `these would render the backslash literally`).toEqual([]);
    });
  }
});
