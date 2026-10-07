/**
 * A link a browser opens never carries the address the web SERVER uses to reach the API.
 *
 * On boss-1 that address is http://127.0.0.1:8000; inlined into "Open preview" links it sent the
 * operator's browser to their own laptop, where another BOSS said "preview not found"
 * (prev_6500cdc6176748ed8b05). Enforced for every source file, so a new screen cannot repeat it.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";
import { browserHref, previewWebHref } from "../lib/browser-links";

const ROOT = join(__dirname, "..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (["node_modules", ".next", "tests"].includes(name)) return [];
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

const code = (text: string) =>
  text.split("\n").filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join("\n");

describe("a browser link never names the API host", () => {
  const files = [...sources(join(ROOT, "app")), ...sources(join(ROOT, "lib")),
    ...sources(join(ROOT, "components"))];

  it("only lib/client.ts reads the API address", () => {
    const readers = files.filter((f) => /process\.env\.NEXT_PUBLIC_BOSS_API_URL/.test(code(readFileSync(f, "utf8"))))
      .map((f) => relative(ROOT, f));
    expect(readers).toEqual(["lib/client.ts"]);
  });

  it("no server action hands the browser a URL built on API_URL", () => {
    const offenders = files.filter((f) =>
      /(url|href|link)\w*\s*:\s*`\$\{API_URL\}/.test(code(readFileSync(f, "utf8"))))
      .map((f) => relative(ROOT, f));
    expect(offenders).toEqual([]);
  });

  it("keeps gateway paths on the console's own origin and engine-chosen URLs as they are", () => {
    expect(previewWebHref("prev_1")).toBe("/v1/previews/prev_1/web/");
    expect(browserHref("/v1/previews/prev_1/web/")).toBe("/v1/previews/prev_1/web/");
    expect(browserHref("v1/previews/prev_1/web/")).toBe("/v1/previews/prev_1/web/");
    expect(browserHref("https://boss.example.com/v1/previews/p/web/")).toBe(
      "https://boss.example.com/v1/previews/p/web/");
  });
});

describe("a preview link on the console lands on the preview origin", () => {
  const read = (p: string) => readFileSync(join(ROOT, p), "utf8");

  it("does not strip the trailing slash an app redirects to (no 308 loop)", () => {
    expect(read("next.config.ts")).toMatch(/skipTrailingSlashRedirect:\s*true/);
    expect(read("app/v1/[...path]/route.ts")).toContain("`${API_URL}${incoming.pathname}${incoming.search}`");
  });

  it("sends every preview path to the preview origin instead of serving it (security finding #4)", () => {
    // The previewed app's own redirects are rewritten by the API on the preview origin; the
    // console never proxies a preview, so it has none to pass through.
    expect(read("app/v1/[...path]/route.ts")).toMatch(/if \(isPreviewCapabilityPath\(pathname\)\) return toPreviewOrigin\(req\)/);
  });

  it("lets a preview tab through middleware to that redirect, signed in or not", () => {
    expect(read("middleware.ts")).toMatch(/if \(isPreviewCapabilityPath\(path\)\) return NextResponse\.next\(\);/);
  });
});
