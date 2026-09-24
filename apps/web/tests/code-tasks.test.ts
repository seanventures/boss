import type { CodeTaskRun } from "@boss/api-client";
import { describe, expect, it } from "vitest";

import {
  emptyPreviewFeed,
  isLoopbackPreviewUrl,
  operatorPreviewOpenHref,
  structuredCodeTaskTests,
} from "../lib/code-tasks";

describe("structuredCodeTaskTests", () => {
  it("keeps structured test evidence and rejects legacy scalar payloads", () => {
    expect(
      structuredCodeTaskTests({
        tests: [{ command: "pnpm test", exit_code: 0, duration_ms: 420 }],
      }),
    ).toEqual([{ command: "pnpm test", exit_code: 0, duration_ms: 420 }]);

    expect(
      structuredCodeTaskTests({ tests: "pnpm test" } as unknown as CodeTaskRun["state"]),
    ).toEqual([]);
    expect(structuredCodeTaskTests()).toEqual([]);
  });
});

describe("operatorPreviewOpenHref", () => {
  it("uses open_url when it is a public gateway path", () => {
    expect(
      operatorPreviewOpenHref({
        id: "prv_1",
        status: "verified",
        open_url: "https://boss-api.local.example.test/v1/previews/prv_1/web/",
        url: "http://127.0.0.1:41635",
      }),
    ).toBe("https://boss-api.local.example.test/v1/previews/prv_1/web/");
  });

  it("does not invent View for verified-test without a URL", () => {
    expect(
      operatorPreviewOpenHref({
        id: "prv_verified",
        status: "verified",
        open_url: null,
        url: null,
      }),
    ).toBeNull();
  });

  it("allows a gateway path for running previews without open_url", () => {
    expect(
      operatorPreviewOpenHref({
        id: "prv_run",
        status: "running",
        open_url: null,
        url: null,
      }),
    ).toBe("/v1/previews/prv_run/web/");
  });

  it("fail-closes expired historical open_url so View is not offered", () => {
    expect(
      operatorPreviewOpenHref({
        id: "prv_exp",
        status: "expired",
        open_url: "https://boss-api.local.example.test/v1/previews/prv_exp/web/",
        url: "http://127.0.0.1:41635",
      }),
    ).toBeNull();
  });

  it("rejects loopback as an operator View URL", () => {
    expect(isLoopbackPreviewUrl("http://127.0.0.1:43053")).toBe(true);
    expect(
      operatorPreviewOpenHref({
        id: "prv_lb",
        status: "verified",
        open_url: "http://127.0.0.1:43053",
        url: "http://127.0.0.1:43053",
      }),
    ).toBeNull();
  });

  it("surfaces preview list failures instead of an empty success", () => {
    const feed = emptyPreviewFeed("API returned 503", "boss-dogfood");
    expect(feed.apiReachable).toBe(false);
    expect(feed.items).toEqual([]);
    expect(feed.blockedReason).toContain("503");
  });
});
