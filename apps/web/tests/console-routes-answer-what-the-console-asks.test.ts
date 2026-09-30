/** Every URL the console fetches must have a handler for the method it uses.
 *
 *  `app/api/deployment-targets/route.ts` exported only DELETE. The workflow view fetched it
 *  with GET, got 405, and — because both loads sat in one async function whose first branch
 *  ended `if (!res.ok) return;` — that reply exited the function before the HOSTS request was
 *  ever made. The deploy step then told the operator "this project has no verified host. Add
 *  one under Infra and Models" while Infra showed the host VERIFIED and READY TO DEPLOY.
 *
 *  They reported it about ten times. Three fixes went to the message, the picker markup and
 *  the wire format; none of them could work, because the request that fills the picker was
 *  never being made. A missing handler in one file, surfacing as a wrong sentence in another.
 *
 *  This walks the console's own source, collects the `/api/...` URLs it fetches with GET, and
 *  fails if the matching route file does not export one.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";

const APP = new URL("../app", import.meta.url).pathname;

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "api" || entry === "node_modules") continue;
      out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

/** The `/api/<name>` endpoints the console fetches without an explicit non-GET method. */
function fetchedWithGet(): Map<string, string[]> {
  const found = new Map<string, string[]>();
  for (const file of sourceFiles(APP)) {
    const text = readFileSync(file, "utf8");
    // `fetch("/api/x?...")` or fetch(`/api/x?...`) — the call and enough of what follows to
    // see whether a method was set.
    const calls = text.matchAll(/fetch\(\s*[`"']\/api\/([a-z0-9-]+)[^)]*\)/gi);
    for (const call of calls) {
      const whole = call[0];
      // A method option means it is not a GET; only GET-by-default calls are checked here.
      if (/method:\s*["'](POST|PUT|PATCH|DELETE)["']/i.test(whole)) continue;
      const name = call[1];
      found.set(name, [...(found.get(name) ?? []), file.replace(APP, "app")]);
    }
  }
  return found;
}

describe("the console's api routes answer the method the console uses", () => {
  const fetched = fetchedWithGet();

  it("finds the console's own GET fetches, so this test cannot pass vacuously", () => {
    // If the scan silently matched nothing, everything below would pass while proving nothing.
    expect(fetched.size).toBeGreaterThan(5);
    expect([...fetched.keys()]).toContain("deployment-hosts");
    expect([...fetched.keys()]).toContain("deployment-targets");
  });

  it.each([...fetched.keys()].sort())("/api/%s exports GET", (name) => {
    const route = join(APP, "api", name, "route.ts");
    // Some names are served by a catch-all or live elsewhere; only assert on routes that exist
    // as their own file, which is the case this bug was in.
    if (!existsSync(route)) return;
    const source = readFileSync(route, "utf8");
    expect(source, `app/api/${name}/route.ts has no GET, but the console fetches it with GET `
      + `(from ${fetched.get(name)?.join(", ")}). A 405 here surfaces as a wrong message `
      + `somewhere else entirely.`).toMatch(/export\s+(async\s+)?function\s+GET\b/);
  });
});
