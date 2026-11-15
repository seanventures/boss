/**
 * Setup › Events (P9a-7, BOSSAPP-1099) is drawn from the design system only, like Infra and
 * Observability: zero inline `style={{}}`, zero hex colours, no `<p>`, type from the --fs-* scale
 * (tests/type-is-a-token.test.ts holds the stylesheet), no `??` guarding a string (a console guard
 * returns "" not null, so `x ?? fallback` silently keeps the empty string).
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const DIR = "app/events";
const files = [...readdirSync(join(process.cwd(), DIR)).filter((f) => /\.(tsx?|css)$/.test(f)).map((f) => join(DIR, f)), "lib/intake.ts", "lib/intake-rules.ts"];
const read = (f: string) => readFileSync(join(process.cwd(), f), "utf8");

describe("Setup › Events uses tokens only", () => {
  it("finds the files", () => {
    expect(files.length).toBeGreaterThan(10);
  });
  it.each(files)("%s has no inline style, no hex colour", (f) => {
    const src = read(f);
    expect(src, "inline style").not.toMatch(/style=\{\{/);
    expect(src, "hex colour").not.toMatch(/#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?\b(?![\w-])/);
  });
  it.each(files.filter((f) => f.endsWith(".tsx")))("%s draws no <p>", (f) => {
    expect(read(f)).not.toMatch(/<p[\s>]/);
  });
  it("the stylesheet sizes type from the scale only", () => {
    const css = read("app/events/events.css");
    const sizes = [...css.matchAll(/font-size:\s*([^;}]+)/g)].map((m) => m[1].trim());
    expect(sizes.filter((v) => !/^var\(--fs-[a-z]+\)$/.test(v) && v !== "inherit")).toEqual([]);
  });
  it.each(files.filter((f) => f.endsWith(".tsx") || f.endsWith(".ts")))("%s never guards a string with ??", (f) => {
    // `a ?? ""`, `x.name ?? y`: a string guard must be `||` (an empty string is not a value).
    const bad = read(f).split("\n").filter((l) => /\?\?\s*(""|''|`)/.test(l) || /\b(name|label|reason|text|display|error|title|word)\s*\?\?/.test(l));
    expect(bad).toEqual([]);
  });
});
