/**
 * A failed agent turn shows a sentence; the technical line waits under a collapsed Details.
 *
 * goldeneye: "Ask Program Manager" showed the operator "BOSS could not complete that turn:
 * IntegrityError: (psycopg.errors.UniqueViolation) duplicate key value … [SQL: INSERT INTO
 * agent_threads (…" — fenced as code in the dock. The API now says the turn in one sentence
 * (`detail`) and keeps the technical line apart (`failure.technical`,
 * model_failures.explain_turn_failure). These hold both chat surfaces to that split.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { apiErrorTechnical, apiErrorText } from "../lib/api-error";

vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  API_KEY: "",
  authHeaders: (extra: Record<string, string>) => ({ Authorization: "Bearer t", ...extra }),
}));
vi.mock("../lib/api-write", () => ({
  apiWrite: (url: string, init: RequestInit) => fetch(url, init),
  pressToken: async () => "press",
}));

afterEach(() => vi.unstubAllGlobals());

const FAILED = {
  detail: "BOSS couldn't save this conversation. Try again. If it keeps happening, the details "
    + "are in the server log.",
  failure: {
    kind: "storage", retry: true,
    technical: "IntegrityError: duplicate key value violates unique constraint "
      + "\"idx_agent_threads_surface_external\"",
  },
};

const src = (path: string) => readFileSync(join(process.cwd(), path), "utf8");

describe("a failed turn", () => {
  it("reads the sentence and the technical line apart", () => {
    expect(apiErrorText(FAILED, 500)).toBe(FAILED.detail);
    expect(apiErrorTechnical(FAILED)).toContain("duplicate key value");
    expect(apiErrorTechnical({ detail: "x" })).toBe("");
  });

  it("the chat route forwards the sentence as the error and the technical line beside it",
    async () => {
      vi.stubGlobal("fetch", vi.fn(async () =>
        new Response(JSON.stringify(FAILED), { status: 500 })));
      const { POST } = await import("../app/api/agent-chat/route");
      const res = await POST(new Request("http://console/api/agent-chat", {
        method: "POST",
        body: JSON.stringify({ project_id: "p", actor: "ceo", message: "hi" }),
      }));
      const body = await res.json();
      expect(res.status).toBe(500);
      expect(body.error).toBe(FAILED.detail);
      expect(body.error).not.toMatch(/SQL|psycopg|IntegrityError/);
      expect(body.technical).toBe(FAILED.failure.technical);
    });

  it("AgentChat puts the technical line under a collapsed Details", () => {
    const s = src("components/v2/AgentChat.tsx");
    expect(s).toMatch(/<details className="v2-chat-technical"/);
    expect(s).toMatch(/technical/);
  });

  it("the dock shows the sentence, not a fenced error, with Details below", () => {
    const s = src("app/agent-ui.tsx");
    expect(s).not.toContain('"⚠ \\n\\n```\\n" + (r.error');
    expect(s).toMatch(/<details className="agent-technical"/);
    expect(src("app/actions.ts")).toMatch(/technical = apiErrorTechnical\(body\)/);
  });
});
