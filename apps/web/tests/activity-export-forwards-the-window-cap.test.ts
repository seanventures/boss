/**
 * The activity export route forwards the API's window header as sent.
 * X-Truncated stays the 50,000-row cap. A cut window does not flip it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/client", () => ({
  API_URL: "http://boss-api",
  authHeaders: () => ({ Authorization: "Bearer test-key" }),
}));

import { GET } from "../app/api/activity/export/route";

afterEach(() => vi.unstubAllGlobals());

function upstream(headers: Record<string, string>) {
  return new Response("created_at,level\n", { status: 200, headers });
}

describe("activity export forwards the window header", () => {
  it("passes x-window-capped through and leaves x-truncated as the row cap", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => upstream({
      "content-type": "text/csv; charset=utf-8",
      "content-disposition": 'attachment; filename="boss-log.csv"',
      "x-truncated": "0",
      "x-window-capped": "1",
      "x-row-count": "12",
    })));

    const response = await GET(new Request("http://console.test/api/activity/export?project_id=p1&format=csv"));

    expect(response.status).toBe(200);
    expect(response.headers.get("x-window-capped")).toBe("1");
    expect(response.headers.get("x-truncated")).toBe("0");
    expect(response.headers.get("x-row-count")).toBe("12");
  });

  it("forwards a window that was not cut without turning the row cap on", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => upstream({
      "content-type": "application/x-ndjson",
      "x-truncated": "1",
      "x-window-capped": "0",
      "x-row-count": "50000",
    })));

    const response = await GET(new Request("http://console.test/api/activity/export?project_id=p1&format=ndjson"));

    expect(response.headers.get("x-window-capped")).toBe("0");
    expect(response.headers.get("x-truncated")).toBe("1");
  });
});
