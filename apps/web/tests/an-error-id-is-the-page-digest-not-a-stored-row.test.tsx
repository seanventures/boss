/**
 * The error page shows Next's digest. That id is not an activity row, so the link into the
 * Activity log must not search for it as if a stored line would match.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { logHref } from "../lib/log-url";
import { codeOnly } from "./_helpers/source";

(globalThis as unknown as { React: typeof React }).React = React;

describe("the error page", () => {
  it("says the id is the page digest, not a stored activity row, and links the Activity log", async () => {
    const { default: ConsoleError } = await import("../app/error");
    const html = renderToStaticMarkup(
      <ConsoleError
        error={Object.assign(new Error("An error occurred in the Server Components render. The specific message is omitted in production builds"), { digest: "123abc" })}
        reset={() => {}}
      />,
    );
    expect(html).toContain("123abc");
    expect(html).toContain("page digest");
    expect(html).toContain("not a stored activity row");
    expect(html).toContain("The same id is in the container log");
    expect(html).toContain("Open the Activity log");
    const href = /<a[^>]*href="([^"]*)"[^>]*>Open the Activity log<\/a>/.exec(html)?.[1]
      ?? /<a[^>]*>Open the Activity log<\/a>/.exec(html)?.[0];
    expect(href).toBe(logHref({}));
    expect(href).not.toContain("123abc");
    expect(new URLSearchParams(String(href).split("?")[1] || "").get("q")).toBeNull();
    const src = codeOnly("app/error.tsx");
    expect(src).toContain('from "../lib/log-url"');
    expect(src).toContain("logHref(");
    expect(src).not.toMatch(/logHref\(\{[^}]*digest/);
  });
});
