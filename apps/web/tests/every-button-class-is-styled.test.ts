/**
 * NO BUTTON FALLS BACK TO THE BROWSER'S OWN LOOK (KG tester round 1: Approvals' "View resolved
 * history" was a grey native button). Its class, `linkish`, was defined nowhere. Every literal
 * className on a <button> must name at least one class some stylesheet defines.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

function files(dir: string, ext: RegExp): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (name === "node_modules" || name.startsWith(".") || name.startsWith("__")) return [];
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path, ext);
    return ext.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}

const css = ["app", "components"].flatMap((d) => files(d, /\.css$/)).map((f) => readFileSync(f, "utf8")).join("\n");
const defined = new Set([...css.matchAll(/\.([A-Za-z_][\w-]*)/g)].map((m) => m[1]));

describe("every button class is styled", () => {
  it("each <button className=\"…\"> names a class a stylesheet defines", () => {
    const bare: string[] = [];
    let seen = 0;
    for (const f of ["app", "components"].flatMap((d) => files(d, /\.tsx$/))) {
      for (const m of readFileSync(f, "utf8").matchAll(/<button\b[^>]*?className="([^"]+)"/g)) {
        seen += 1;
        const tokens = m[1].split(/\s+/);
        if (!tokens.some((t) => defined.has(t))) bare.push(`${f}: className="${m[1]}"`);
      }
    }
    expect(seen).toBeGreaterThan(200);
    expect(bare).toEqual([]);
  });
});
