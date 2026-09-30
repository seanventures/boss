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

describe("selecting something in the list actually selects it", () => {
  it("the opening choice runs once, not on every render", () => {
    // REPORTED AS "when I click on Google cloud client nothing happens".
    //
    // Landing on a channel when one exists is a sensible OPENING. Written as an effect keyed on
    // `selected`, it stopped being an opening and became an enforcement: the click set the
    // selection and the effect put it straight back, before a frame was painted. A ref makes it
    // happen at most once.
    expect(UI).toContain("const landed = useRef(false)");
    expect(UI).toContain("if (landed.current) return;");
  });

  it("and the selection is not in the effect's dependencies", () => {
    // If it were, the effect would run again on every click — which is the whole defect.
    const effect = UI.slice(UI.indexOf("const landed = useRef(false)"));
    const deps = effect.slice(0, effect.indexOf("]);") + 3);
    expect(deps).not.toContain("selected]");
    expect(deps).toContain("[status?.configured, channels, connected]");
  });

  it("nothing is decided before the channels have loaded", () => {
    // `channels === null` means "not loaded", not "none" — opening on the client because a
    // fetch had not returned yet would be a guess presented as a decision.
    expect(UI).toContain("if (!channels) return;");
  });
});

describe("finishing one step shows the next one", () => {
  it("a configured client offers the connect action in its own pane", () => {
    // REPORTED AS "after saving the client I'm clueless what to do — who knows to click
    // + connect a channel". Saving put a success line at the top of the page and left the
    // operator looking at a form whose only button now read "Replace client". The real next
    // step was a header button at the far corner of the screen.
    expect(UI).toContain('data-testid="client-next-step"');
    expect(UI).toContain('data-testid="client-connect-next"');
    expect(UI).toContain("This client is ready.");
  });

  it("and it is worded for whether a channel already exists", () => {
    expect(UI).toContain('channelCount > 0 ? "Connect another channel" : "Connect a channel"');
  });

  it("replacing the client stops being the loudest control once one is set", () => {
    // It is the rare action — swapping the Cloud application every channel authorises against
    // — and as the only primary button it read as the thing to do next.
    expect(UI).toContain('className={ready ? "as-button secondary" : "as-button"}');
    expect(UI).toContain('ready ? "Replace this client" : "Save client"');
  });

  it("the success message names a button that is actually in reach", () => {
    // "You can connect the channel now" described an action without saying where it lives.
    expect(UI).toContain("Connect a channel” below");
  });

  it("and says what replacing it would cost", () => {
    // Every channel authorises against this client; swapping it invalidates all of them.
    expect(UI).toContain("every channel would then have to be");
  });
});
