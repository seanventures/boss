/**
 * The Kit's words (tester K6, K17, K21): a provider's raw error never reaches a red line, a tier-1
 * write is not called a read, and a count of one is singular.
 */
import { describe, expect, it } from "vitest";

import { plural, riskOf, shortError } from "../app/skills/kit-copy";

describe("shortError: six words at most, the same rules as the server's", () => {
  it.each([
    ["HTTP 401: Bad credentials", "Token rejected"],
    ["(535, b'5.7.8 Username and Password not accepted')", "Token rejected"],
    ['Linear: HTTP 502: {"message": "502 Bad Gateway"}', "Can't reach server"],
    ["502 Bad Gateway", "Can't reach server"],
    ["ConnectError: [Errno 61] Connection refused", "Can't reach server"],
    ["SMTPNotSupportedError: STARTTLS extension not supported by server.", "TLS setting mismatch"],
    ["[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1006)", "TLS setting mismatch"],
    ["HTTP 404: Not Found", "Not found"],
    ["No GitHub token for this repo", "No GitHub token for this repo"],
    ["GitHub Issues needs owner, repo, and a token to file issues", "Missing a setting"],
    ["Connect your GitHub repo in Setup first", "Missing a setting"],
    ['ValueError: {"detail": "something odd happened inside"}', "Test failed"],
    ["", "Test failed"],
  ])("%s → %s", (raw, said) => {
    expect(shortError(raw)).toBe(said);
    expect(shortError(raw).split(/\s+/).length).toBeLessThanOrEqual(6);
  });

  it("never passes a raw marker through", () => {
    for (const raw of ['HTTP 418: {"a":1}', "KeyError: 'x'", "Traceback (most recent call last)"]) {
      expect(shortError(raw)).not.toMatch(/[{}":]|error|http/i);
    }
  });
});

describe("risk words follow what a tool does", () => {
  it("tier 0 reads; tiers 1 and 2 write; tier 3 is public", () => {
    expect(riskOf(0).label).toBe("Reads");
    expect(riskOf(1).label).toBe("Writes"); // Open an issue, Update an issue
    expect(riskOf(2).label).toBe("Writes");
    expect(riskOf(3).label).toBe("Public");
  });
});

describe("plural", () => {
  it("one is singular", () => {
    expect(plural(1, "tool")).toBe("1 tool");
    expect(plural(3, "tool")).toBe("3 tools");
    expect(plural(1, "ability", "abilities")).toBe("1 ability");
    expect(plural(2, "ability", "abilities")).toBe("2 abilities");
  });
});
