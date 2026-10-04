// @vitest-environment happy-dom
/**
 * A pasted token, key or auth.json is sent once and never kept: the field is empty after
 * success AND after failure, and no secret reaches the URL or browser storage.
 */
import { afterEach, describe, expect, it } from "vitest";

import { click, fakeApi, flush, mountBoard, press, q, type, type Mounted } from "./_helpers/setup-board";
import { board, firstRun, repoDone } from "./_helpers/setup-fixtures";

const TOKEN = "github_pat_11AAAAAA_secretsecretsecretsecret";
const KEY = "sk-or-v1-secretsecretsecretsecret";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

function noTrace(secret: string) {
  expect(window.location.href).not.toContain(secret.slice(-12));
  const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
  expect(stored).not.toContain(secret.slice(-12));
}

describe("the token field", () => {
  for (const outcome of ["accepted", "refused"] as const) {
    it(`is empty after the token is ${outcome}`, async () => {
      const api = fakeApi(firstRun());
      api.answer = (call) => call.op === "repo_token" ? (outcome === "accepted"
        ? { status: 200, body: { account_login: "param", expires_at: null, connection_id: "c",
                                 repos: [{ full_name: "owner/app", private: true,
                                           default_branch: "main", owner_type: "User" }] } }
        : { status: 422, body: { error: "x", refusal: { state: "token_rejected",
                                 headline: "Token rejected", fix: "GitHub refused this token." } } })
        : null;
      mounted = await mountBoard(firstRun());
      const input = q(document.body, "su-token-input") as HTMLInputElement;
      expect(input.type).toBe("password");
      await type(input, TOKEN);
      await press(input, "Enter");
      await flush();
      const sent = api.calls.find((c) => c.op === "repo_token");
      expect(sent?.body.token).toBe(TOKEN);
      expect(sent?.url).not.toContain(TOKEN);
      const after = q(document.body, "su-token-input") as HTMLInputElement | null;
      expect(after?.value ?? "").toBe("");
      if (outcome === "accepted") expect(q(document.body, "su-repo-picker")).not.toBeNull();
      else expect(q(document.body, "su-refusal")?.textContent).toBe("Token rejected");
      noTrace(TOKEN);
    });
  }
});

describe("the model key field", () => {
  for (const outcome of ["accepted", "refused"] as const) {
    it(`is empty after the key is ${outcome}`, async () => {
      const api = fakeApi(repoDone());
      api.answer = (call) => call.op === "models" ? (outcome === "accepted"
        ? { status: 200, body: board() }
        : { status: 422, body: { error: "x", refusal: { state: "key_rejected",
                                 headline: "Key rejected", fix: "The provider refused this key." } } })
        : null;
      mounted = await mountBoard(repoDone(), "?area=setup&add=openrouter");
      await type(q(document.body, "su-model-key"), KEY);
      await click(q(document.body, "su-model-connect"));
      const sent = api.calls.find((c) => c.op === "models");
      expect(sent?.body.api_key).toBe(KEY);
      const field = q(document.body, "su-model-key") as HTMLInputElement | null;
      expect(field?.value ?? "").toBe("");
      noTrace(KEY);
    });
  }
});
