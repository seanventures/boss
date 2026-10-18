import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The BOSS agent acts on the person's behalf, so a turn that needs a grant answers 409
 * consent_required with the consent request. The dock showed "⚠ HTTP 409" instead (D2: asked to
 * retry a stopped build, twice), because postJsonReturn read only `detail`. The request now
 * reaches the dock, which opens the consent sheet and sends the same turn once more.
 */
const ACTIONS = codeOnly("app/actions.ts");
const DOCK = codeOnly("app/agent-ui.tsx");

describe("the BOSS agent dock asks for consent instead of failing", () => {
  it("returns a consent request from the chat call, not a bare status", () => {
    const fn = ACTIONS.slice(ACTIONS.indexOf("async function postJsonReturn"),
                             ACTIONS.indexOf("async function", ACTIONS.indexOf("async function postJsonReturn") + 10));
    expect(fn).toContain('body.error === "consent_required"');
    expect(fn).toContain("data: { consent }");
  });

  it("opens the consent sheet and sends the turn again once granted", () => {
    const send = DOCK.slice(DOCK.indexOf("const turn = () =>"), DOCK.indexOf("if (r === stalled)"));
    expect(send).toContain("consentFromBody(");
    expect(send).toMatch(/await askConsent\(asked\)\) r = await turn\(\)/);
  });
});
