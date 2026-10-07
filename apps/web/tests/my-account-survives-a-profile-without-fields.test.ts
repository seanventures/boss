/**
 * My account survives a profile without fields (round 3 upgrade walk): the shared console
 * password's session (usr_legacy_shared, no account row) got `{}` from /v1/identity/me/profile and
 * /account crashed "Cannot read properties of undefined (reading 'trim')". The API answers the
 * full shape now, and the page reads it through asProfile, which never hands on a nameless profile.
 */
import { describe, expect, it } from "vitest";

import { asProfile } from "../lib/identity/types-account";

describe("asProfile", () => {
  it("passes a real profile through", () => {
    expect(asProfile({ display_name: "Ada", email: "ada@x.test", avatar_seed: "3" })).toEqual({ display_name: "Ada", email: "ada@x.test", avatar_seed: "3" });
  });
  it("turns an empty or nameless answer into no profile (the page falls back to /me)", () => {
    expect(asProfile({})).toBeNull();
    expect(asProfile({ display_name: "", email: null })).toBeNull();
    expect(asProfile(null)).toBeNull();
  });
});
