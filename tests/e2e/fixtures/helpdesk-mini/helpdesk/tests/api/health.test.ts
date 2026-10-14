import { describe, expect, it } from "vitest";
import request from "supertest";
import { createApp } from "../../src/server/app";

describe("health", () => {
  it("P0 the app starts and /healthz answers 200", async () => {
    const res = await request(await createApp()).get("/healthz");
    expect(res.status).toBe(200);
  });

  it("P1 /readyz is 200 once the migrations ran", async () => {
    const res = await request(await createApp()).get("/readyz");
    expect(res.status).toBe(200);
  });
});
