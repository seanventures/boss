/**
 * Infra and Observability are drawn from the design system only (infra-observability spec §9.5):
 * zero inline `style={{}}`, zero hex colours, no `<p>`, and nothing that refreshes the console —
 * the same guard the Factory has. The old screens had 340+ inline styles and GitHub-hex colours;
 * a new one coming back is a regression of the redesign, not a style nit.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const DIRS = ["app/infra", "app/observability"];
const files = DIRS.flatMap((d) => readdirSync(join(process.cwd(), d))
  .filter((f) => /\.(tsx?|css)$/.test(f)).map((f) => join(d, f)));
const read = (f: string) => readFileSync(join(process.cwd(), f), "utf8");

describe("Infra and Observability use tokens only", () => {
  it("finds the files", () => {
    expect(files.length).toBeGreaterThan(20);
  });
  it.each(files)("%s has no inline style, no hex colour", (f) => {
    const src = read(f);
    expect(src, "inline style").not.toMatch(/style=\{\{/);
    expect(src, "hex colour").not.toMatch(/#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?\b(?![\w-])/);
  });
  it.each(files.filter((f) => f.endsWith(".tsx")))("%s draws no <p> and never refreshes the console", (f) => {
    const src = read(f);
    expect(src).not.toMatch(/<p[\s>]/);
    expect(src).not.toMatch(/router\.refresh|requestConsoleRefresh|registerConsoleRefresh|setInterval\(/);
  });
});
