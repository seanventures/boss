import { describe, expect, it } from "vitest";
import request from "supertest";
import { createApp } from "../../src/server/app";

describe("tickets", () => {
  it("P1 listing tickets needs a signed-in user", async () => {
    const res = await request(await createApp()).get("/api/tickets");
    expect(res.status).toBe(401);
  });

  it("P1 creating a ticket needs a signed-in user", async () => {
    const res = await request(await createApp()).post("/api/tickets").send({ title: "Refund please" });
    expect(res.status).toBe(401);
  });
});
