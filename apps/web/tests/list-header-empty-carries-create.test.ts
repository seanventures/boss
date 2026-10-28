/**
 * ListHeader hides its `create` action while the list is empty and relies on the card's
 * EmptyState to carry it. A card that passes `create` without an EmptyState has NO way to make
 * the first item: on 2026-10-28 "Approve on my behalf" showed no button on a fresh install.
 * Every file that hands ListHeader a `create` must also render an EmptyState with an `action`.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOTS = ["app", "components"].map((d) => join(__dirname, "..", d));

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (name === "node_modules") return [];
    return statSync(path).isDirectory() ? files(path) : path.endsWith(".tsx") ? [path] : [];
  });
}

describe("a ListHeader create action is reachable when the list is empty", () => {
  const callers = ROOTS.flatMap(files).filter((f) => /<ListHeader[^>]*\bcreate=\{/s.test(readFileSync(f, "utf8")));

  it("finds the callers", () => expect(callers.length).toBeGreaterThan(3));

  it.each(callers.map((f) => [f.split("/apps/web/")[1] ?? f, f]))("%s renders an EmptyState with an action", (_n, f) => {
    const src = readFileSync(f, "utf8");
    expect(/<EmptyState\b[\s\S]{0,400}?\baction=\{/.test(src)).toBe(true);
  });
});
