// @vitest-environment happy-dom
/**
 * BOSSAPP-897: the first press after "Confirm it's you" said "Reload and retry". The step-up
 * rotates the session (and so its CSRF token); the provider re-read /me on its own before the
 * refused write came back, so the held token already matched the fresh one and the retry was
 * skipped. The retry is decided against the token the refused request SENT.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { idPost, setCsrfToken } from "../lib/identity/api";

afterEach(() => { vi.unstubAllGlobals(); setCsrfToken(""); });

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("a write refused for a stale CSRF token", () => {
  it("is sent again with the new token even when the provider already holds it", async () => {
    setCsrfToken("old");
    const sent: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).endsWith("/v1/identity/me")) return json(200, { csrf_token: "new" });
      const token = new Headers(init?.headers).get("X-Boss-CSRF") ?? "";
      sent.push(token);
      if (token === "old") {
        setCsrfToken("new");              // the provider's own /me re-read, landing first
        return json(403, { error: "csrf_failed" });
      }
      return json(200, { ok: true });
    }));
    await expect(idPost("/v1/identity/github/app/manifest", {})).resolves.toEqual({ ok: true });
    expect(sent).toEqual(["old", "new"]);
  });

  it("is not sent again when the session's token really is the one refused", async () => {
    setCsrfToken("same");
    let writes = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).endsWith("/v1/identity/me")) return json(200, { csrf_token: "same" });
      writes += 1;
      return json(403, { error: "csrf_failed" });
    }));
    await expect(idPost("/v1/identity/x", {})).rejects.toMatchObject({ code: "csrf_failed" });
    expect(writes).toBe(1);
  });
});
