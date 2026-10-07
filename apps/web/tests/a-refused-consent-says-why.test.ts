/**
 * A REFUSED CONSENT SAYS WHY (round 3, real-stack build walk). A person with no GitHub access of
 * their own granted an agent push on a repo; the API answered 403 github_access_lacking and the
 * sheet said only "That didn't work". The refusal now names the repo and the fix, and no bare
 * code ever reaches the screen.
 */
import { describe, expect, it } from "vitest";

import { consentRefusal } from "../components/identity/consent-sheet";
import { IdentityError, errorSentence } from "../lib/identity/api";

describe("consentRefusal", () => {
  it("names the repo and the fix for github_access_lacking", () => {
    const e = new IdentityError(403, "github_access_lacking", { body: { error: "github_access_lacking", repo: "acme/app" } });
    const words = consentRefusal(e);
    expect(words).toContain("acme/app");
    expect(words).toContain("Connect your GitHub");
    expect(words).not.toMatch(/didn't work|github_access_lacking/);
  });

  it("keeps the API's own sentence and never shows a bare code", () => {
    const said = new IdentityError(403, "policy_refused", { body: { error: "policy_refused", detail: "Longer than your org allows." } });
    expect(consentRefusal(said)).toBe("Longer than your org allows.");
    const bare = new IdentityError(403, "policy_refused", { body: { error: "policy_refused", detail: "policy_refused" } });
    expect(consentRefusal(bare)).toBe("Not allowed by policy");
  });

  it("has short words for the toast too", () => {
    expect(errorSentence(403, "github_access_lacking")).toBe("Connect your GitHub first");
  });
});
