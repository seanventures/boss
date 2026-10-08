// @vitest-environment happy-dom
/**
 * A LINK'S TOKEN IS READ FROM THE FRAGMENT ONLY (id-sec-final2 residual). linkToken fell back to
 * `?t=` in the query string. BOSS never mints such a link, but a token there has already reached
 * an access log by the time the page reads it, so a query-string token is never honoured.
 */
import { afterEach, describe, expect, it } from "vitest";

import { linkToken } from "../app/(auth)/auth-parts";

afterEach(() => { window.history.replaceState(null, "", "/"); });

describe("linkToken", () => {
  it("reads the fragment", () => {
    window.history.replaceState(null, "", "/reset#t=frag_token");
    expect(linkToken("reset")).toBe("frag_token");
  });

  it("never reads the query string", () => {
    window.history.replaceState(null, "", "/reset?t=query_token");
    expect(linkToken("reset")).toBe("");
    window.history.replaceState(null, "", "/reset?t=query_token#x=1");
    expect(linkToken("reset")).toBe("");
  });
});
