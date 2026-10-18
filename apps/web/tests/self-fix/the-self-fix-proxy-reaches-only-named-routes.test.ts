/**
 * /api/self-fix (app/api/self-fix/route.ts) reaches ONLY the routes named in lib/self-fix-ops.ts:
 * an unknown view or op is refused before anything leaves the console; each named one goes to its
 * /v1 path with its own body; the patch export passes through as bytes; approval is not an op (a
 * self-fix install is answered in Approvals, like any decision).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/client", () => ({
  API_URL: "http://boss-api",
  authHeaders: (extra?: Record<string, string>) => ({ Authorization: "Bearer test-key", ...(extra ?? {}) }),
}));

const { GET, POST } = await import("../../app/api/self-fix/route");
const { SELF_FIX_OPS, SELF_FIX_READS } = await import("../../lib/self-fix-ops");

afterEach(() => { vi.unstubAllGlobals(); });

function upstream(answer: () => Response = () => Response.json({ ok: true })) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    calls.push({ url: String(input), method: (init?.method ?? "GET").toUpperCase(),
                 body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return answer();
  }));
  return calls;
}
const get = (q: string) => GET(new Request(`http://console/api/self-fix?${q}`));
const post = (body: Record<string, unknown>) =>
  POST(new Request("http://console/api/self-fix", { method: "POST", body: JSON.stringify(body),
                                                      headers: { "Content-Type": "application/json" } }));

describe("the self-fix proxy", () => {
  it("names its reads and ops, and approval is not one of them", () => {
    expect(Object.keys(SELF_FIX_READS).sort()).toEqual(
      ["autonomy", "diff", "list", "one", "patch", "recommendations", "run_autonomy", "settings", "upstream"]);
    expect(Object.keys(SELF_FIX_OPS).sort()).toEqual(
      ["cancel", "edit_preview", "fork", "preview", "remove", "remove_all", "send", "settings", "start"]);
    expect(Object.keys(SELF_FIX_OPS).some((k) => /approve|decide|ticket/.test(k))).toBe(false);
  });

  it("refuses an unknown view or op without calling the API", async () => {
    const calls = upstream();
    expect((await get("view=tickets&project_id=p1")).status).toBe(400);
    expect((await get("view=list")).status).toBe(400);
    expect((await post({ op: "approve", project_id: "p1" })).status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it("each read goes to its own /v1 path", async () => {
    const calls = upstream();
    await get("view=one&project_id=p1&id=sfx_1");
    await get("view=autonomy&project_id=p1&window=7d");
    await get("view=run_autonomy&project_id=p1&run_id=wfr_1");
    expect(calls.map((c) => c.url)).toEqual([
      "http://boss-api/v1/self-fixes/sfx_1?project_id=p1",
      "http://boss-api/v1/autonomy?project_id=p1&window=7d",
      "http://boss-api/v1/autonomy/runs/wfr_1?project_id=p1",
    ]);
  });

  it("each op goes to its own path with its own body", async () => {
    const calls = upstream();
    await post({ op: "send", project_id: "p1", actor: "ceo", id: "sfx_1", send_id: "sfs_1", checked: true, extra: "x" });
    await post({ op: "remove_all", project_id: "p1", actor: "ceo" });
    await post({ op: "settings", project_id: "p1", actor: "ceo", on: false });
    expect(calls.map((c) => [c.method, c.url])).toEqual([
      ["POST", "http://boss-api/v1/self-fixes/sfx_1/upstream/send"],
      ["POST", "http://boss-api/v1/self-fixes/remove-all"],
      ["PUT", "http://boss-api/v1/self-fix/settings"],
    ]);
    expect(calls[0].body).toEqual({ project_id: "p1", actor: "ceo", send_id: "sfs_1", checked: true });
    expect(calls[2].body).toEqual({ project_id: "p1", actor: "ceo", on: false });
  });

  it("passes the patch export through as bytes", async () => {
    upstream(() => new Response(new Uint8Array([1, 2, 3]), {
      status: 200, headers: { "content-type": "application/x-tar", "content-disposition": "attachment; filename=fix.tar" } }));
    const r = await get("view=patch&project_id=p1&id=sfx_1");
    expect(r.headers.get("content-disposition")).toContain("fix.tar");
    expect(new Uint8Array(await r.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
  });

  it("says the API's refusal as the API said it", async () => {
    upstream(() => Response.json({ detail: "this self-fix is installing; there is nothing to send" }, { status: 409 }));
    const r = await post({ op: "send", project_id: "p1", actor: "ceo", id: "sfx_1", send_id: "s", checked: true });
    expect(r.status).toBe(409);
    expect((await r.json()).error).toContain("nothing to send");
  });
});
