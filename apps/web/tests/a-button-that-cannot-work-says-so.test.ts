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
    // Both mint a token through the OAuth client. Neither can work without one. Connect is
    // guarded on the client status directly; Reconnect is guarded on the same fact passed into
    // the channel pane as `clientReady`.
    expect(UI).toContain("disabled={busy || !status?.configured}");
    expect(UI).toContain("disabled={busy || !clientReady}");
    expect(UI).toContain("clientReady={Boolean(status?.configured)}");
  });

  it("and says why, before it is pressed", () => {
    expect(UI).toContain("Add the Google Cloud client first");
    expect(UI).toContain("cannot publish and cannot be");
  });

  it("the reason is on the channel itself, not just the button's tooltip", () => {
    // A tooltip is invisible on a touch screen and to anyone not hovering.
    expect(UI).toContain("channel-needs-client-");
    // And it points at where the fix is, because the client is a separate item in the list now.
    expect(UI).toContain("Google Cloud client</b> on the left");
  });
});

describe("the answer appears where the question was asked", () => {
  it("a note renders under the header, above both panes", () => {
    // Whatever was pressed, the answer is in one place and it is above the fold.
    expect(UI).toContain('data-testid="channel-note-top"');
    expect(UI).toContain('className={note.ok ? "setup-ok as-note" : "setup-bad as-note"}');
  });

  it("and the API's own sentence is what gets shown", () => {
    // `data.error` carries the proxied `detail`; falling back to "HTTP 422" would throw away
    // the one part of the response that tells the operator what to do.
    expect(UI).toContain("String(data?.error ?? `HTTP ${r.status}`)");
  });
});
