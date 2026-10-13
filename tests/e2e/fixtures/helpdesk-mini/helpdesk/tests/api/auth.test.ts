import { describe, expect, it } from "vitest";
import request from "supertest";
import { createApp } from "../../src/server/app";

describe("login-oauth wiring", () => {
  it("P0 GET /auth/providers lists the configured providers", async () => {
    const res = await request(await createApp()).get("/auth/providers");
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.providers)).toBe(true);
  });

  it("P0 GET /api/me is 401 signed out", async () => {
    const res = await request(await createApp()).get("/api/me");
    expect(res.status).toBe(401);
  });
});
