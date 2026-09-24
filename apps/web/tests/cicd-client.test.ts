import { describe, expect, it } from "vitest";

import { createBossApiClient } from "@boss/api-client";


describe("CI/CD and promotion API client", () => {
  it("uses the governed config, rollout, and promotion routes", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify({ project_id: "boss-dogfood", items: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };
    const client = createBossApiClient({
      baseUrl: "http://127.0.0.1:8000",
      fetchImpl,
    });

    await client.proposeCIConfig({
      project_id: "boss-dogfood",
      repo: "example-org/boss-sandbox",
      language: "typescript",
      framework: "nextjs",
      reservation_id: "res_1",
      dedupe_key: "ci:boss-sandbox",
      branch_name: "boss/ci-config",
    });
    await client.startDeploymentRollout("deprun_1", { actor: "ceo" });
    await client.advanceDeploymentRollout("deprun_1", {
      actor: "ceo",
      observation: { health: "healthy" },
    });
    await client.rollbackDeploymentRollout("deprun_1", {
      actor: "ceo",
      reason: "error-rate breach",
    });
    await client.listDeploymentPromotions("boss-dogfood", 10);
    await client.getDeploymentPromotion("prom_1");
    await client.createDeploymentPromotion({
      project_id: "boss-dogfood",
      staging_target_id: "dt_staging",
      production_target_id: "dt_production",
      commit_ref: "abc123",
      actor: "ceo",
    });
    await client.advanceDeploymentPromotion("prom_1", { actor: "ceo" });

    expect(calls.map((call) => `${call.init?.method} ${call.url}`)).toEqual([
      "POST http://127.0.0.1:8000/v1/pipeline-config/proposals",
      "POST http://127.0.0.1:8000/v1/deployment-runs/deprun_1/rollout/start",
      "POST http://127.0.0.1:8000/v1/deployment-runs/deprun_1/rollout/advance",
      "POST http://127.0.0.1:8000/v1/deployment-runs/deprun_1/rollout/rollback",
      "GET http://127.0.0.1:8000/v1/deployment-promotions?project_id=boss-dogfood&limit=10",
      "GET http://127.0.0.1:8000/v1/deployment-promotions/prom_1",
      "POST http://127.0.0.1:8000/v1/deployment-promotions",
      "POST http://127.0.0.1:8000/v1/deployment-promotions/prom_1/advance",
    ]);
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      repo: "example-org/boss-sandbox",
      language: "typescript",
      reservation_id: "res_1",
    });
  });
});
