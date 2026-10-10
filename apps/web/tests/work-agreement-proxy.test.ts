import { afterEach, expect, it, vi } from "vitest";
vi.mock("../lib/client", () => ({ API_URL: "http://api.test", authHeaders: (extra: Record<string, string>) => extra }));
vi.mock("../lib/api-write", () => ({ apiWrite: (url: string, init: RequestInit) => fetch(url, init) }));
const preview = await import("../app/api/builds/preview/route");
const build = await import("../app/api/builds/route");
afterEach(() => vi.unstubAllGlobals());
const request = (body: unknown) => new Request("http://console.test/api/builds", { method: "POST",
  body: JSON.stringify(body), headers: { "Idempotency-Key": "browser-read", "Content-Type": "application/json" } });
it("keeps a private brief in the POST body and forwards no press key to the read-only API", async () => {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init }); return Response.json({ schema_version: 1 });
  }));
  const body = { project_id: "p1", directive: "Private brief & credential-free task" };
  expect((await preview.POST(request(body))).status).toBe(200);
  expect(calls[0].url).toBe("http://api.test/v1/delivery-workflows/agreement");
  expect(calls[0].init.method).toBe("POST");
  expect(JSON.parse(String(calls[0].init.body))).toEqual(body);
  expect(new Headers(calls[0].init.headers).has("Idempotency-Key")).toBe(false);
  expect(calls[0].init.cache).toBe("no-store");
});
it("forwards a canonical read refusal as a useful sentence", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "No workflow is selected." }, { status: 409 })));
  const response = await preview.POST(request({ project_id: "p1", directive: "Change" }));
  expect(response.status).toBe(409); expect((await response.json()).error).toBe("No workflow is selected.");
});
it("an unbound start refuses locally before any upstream effect", async () => {
  const send = vi.fn(); vi.stubGlobal("fetch", send);
  expect((await build.POST(request({ project_id: "p1", directive: "Change" }))).status).toBe(409);
  expect(send).not.toHaveBeenCalled();
});
it("forwards the returned binding and computes Unicode titles using canonical code points", async () => {
  const calls: RequestInit[] = [];
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
    calls.push(init); return Response.json({ run: { id: "r" } });
  }));
  const directive = "🚀".repeat(74);
  const hash = "1".repeat(64), token = `123.${hash}.${"3".repeat(32)}.${"2".repeat(64)}`;
  expect((await build.POST(request({ project_id: "p1", directive,
    agreement_content_hash: hash, agreement_binding_token: token }))).status).toBe(200);
  expect(JSON.parse(String(calls[0].body))).toMatchObject({ work_class: "project.new",
    input: { title: "🚀".repeat(69) + "…", directive }, agreement_content_hash: hash, agreement_binding_token: token });
});
it("rejects malformed JSON object shapes without starting", async () => {
  const send = vi.fn(); vi.stubGlobal("fetch", send);
  for (const body of [null, [], "text"]) expect((await build.POST(request(body))).status).toBe(400);
  expect(send).not.toHaveBeenCalled();
});
