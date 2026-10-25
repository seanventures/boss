/**
 * /api/setup's sign-in ops reach the BOSSAPP-769 routes exactly: start, poll, code, cancel —
 * and a refusal keeps its {state, headline, fix} whole for the drawer.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  authHeaders: (extra?: Record<string, string>) => ({ Authorization: "Bearer test-key", ...(extra ?? {}) }),
}));
vi.mock("../lib/api-write", () => ({
  apiWrite: (url: string, init: RequestInit) => fetch(url, init),
}));

import { GET, POST } from "../app/api/setup/route";

afterEach(() => vi.unstubAllGlobals());

type Up = { url: string; method: string; body: unknown };
function upstream(answer: { status?: number; body: unknown } = { body: { sign_in_id: "si_1", state: "waiting" } }) {
  const calls: Up[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    calls.push({ url: String(input), method: (init?.method ?? "GET").toUpperCase(),
                 body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return Response.json(answer.body, { status: answer.status ?? 200 });
  }));
  return calls;
}
const post = (body: Record<string, unknown>) => POST(new Request("http://console/api/setup", {
  method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } }));

describe("the sign-in ops", () => {
  it("sign_in_start → POST /v1/setup/sign-in with the provider", async () => {
    const calls = upstream();
    const r = await post({ op: "sign_in_start", project_id: "p 1", provider: "codex" });
    expect(r.status).toBe(200);
    expect(calls[0]).toEqual({ url: "http://boss-api/v1/setup/sign-in", method: "POST",
                               body: { project_id: "p 1", provider: "codex" } });
  });

  it("GET ?sign_in= → GET /v1/setup/sign-in/{id}?project_id=", async () => {
    const calls = upstream();
    const r = await GET(new Request("http://console/api/setup?project_id=p%201&sign_in=si%2F1"));
    expect(await r.json()).toEqual({ sign_in_id: "si_1", state: "waiting" });
    expect(calls[0].url).toBe("http://boss-api/v1/setup/sign-in/si%2F1?project_id=p%201");
    expect(calls[0].method).toBe("GET");
  });

  it("sign_in_code → POST /v1/setup/sign-in/{id}/code with the project and code only", async () => {
    const calls = upstream();
    await post({ op: "sign_in_code", project_id: "p", sign_in_id: "si_1", code: "abc" });
    expect(calls[0]).toEqual({ url: "http://boss-api/v1/setup/sign-in/si_1/code", method: "POST",
                               body: { project_id: "p", code: "abc" } });
  });

  it("sign_in_cancel → DELETE /v1/setup/sign-in/{id}?project_id=, no body", async () => {
    const calls = upstream({ body: { sign_in_id: "si_1", state: "cancelled" } });
    await post({ op: "sign_in_cancel", project_id: "p", sign_in_id: "si_1" });
    expect(calls[0]).toEqual({ url: "http://boss-api/v1/setup/sign-in/si_1?project_id=p",
                               method: "DELETE", body: undefined });
  });

  it("a refusal arrives whole: {state, headline, fix}", async () => {
    const detail = { state: "cli_missing", headline: "Grok is not on the BOSS server", fix: "Install it." };
    upstream({ status: 409, body: { detail } });
    const r = await post({ op: "sign_in_start", project_id: "p", provider: "grok" });
    expect(r.status).toBe(409);
    expect((await r.json()).refusal).toEqual(detail);
  });
});
