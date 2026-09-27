/**
 * "I'm trying to click on Reconnect but it isn't doing anything."
 *
 * It was doing exactly what it should. The API answered 422 with a good sentence — "No Google
 * OAuth client for this project. Add one in the console…" — the console stored it, and rendered
 * it at the BOTTOM of the page inside a collapsible section about OAuth clients. The button is
 * near the top. A press produced a visible change a screenful away, under a heading the
 * operator had no reason to be looking at.
 *
 * The same page already knew: "Connect another channel" is disabled when no client is
 * configured, and the section heading read "Google Cloud client for this project — not set up
 * yet". One button was guarded and the one beside it was not.
 */
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

const UI = codeOnly("app/channels-ui.tsx");

describe("a control that cannot succeed does not pretend otherwise", () => {
  it("Reconnect obeys the same precondition as Connect", () => {
    // Both mint a token through the OAuth client. Neither can work without one.
    const guards = UI.split("disabled={busy || !status?.configured}").length - 1;
    expect(guards).toBeGreaterThanOrEqual(2);
  });

  it("and says why, before it is pressed", () => {
    expect(UI).toContain("Add your Google OAuth client below first");
    expect(UI).toContain("cannot be used or");
  });

  it("the row says it, not just the button's tooltip", () => {
    // A tooltip is invisible on a touch screen and to anyone not hovering.
    expect(UI).toContain("channel-needs-client-");
  });
});

describe("the answer appears where the question was asked", () => {
  it("a note renders beside the buttons, not only at the foot of the page", () => {
    expect(UI).toContain('data-testid="channel-note-top"');
    // The original, at the bottom by the client form, stays — it is the right place for the
    // result of SAVING a client.
    expect(UI).toContain('data-testid="channel-note"');
  });

  it("and the API's own sentence is what gets shown", () => {
    // `data.error` carries the proxied `detail`; falling back to "HTTP 422" would throw away
    // the one part of the response that tells the operator what to do.
    expect(UI).toContain("String(data?.error ?? `HTTP ${r.status}`)");
  });
});
