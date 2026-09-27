import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Onboarding a channel, without the credential ever coming back out.
 *
 * Two steps, and they are two because OAuth cannot be one: entering the Google client is an
 * ordinary form, and authorising it leaves for Google's consent screen and returns. A wizard
 * that pretended otherwise would stall on the redirect.
 */
const UI = codeOnly("app/channels-ui.tsx");
const ROUTE = codeOnly("app/api/channels/route.ts");

describe("connecting a channel", () => {
  it("the client secret is write-only on screen", () => {
    // It cannot be read back from the API, so leaving it in the field after saving only
    // creates somewhere for it to be shoulder-read.
    expect(UI).toContain('type="password"');
    expect(UI).toContain('setForm((f) => ({ ...f, client_secret: "" }));');
    // And the browser must not offer to remember it.
    expect(UI).toContain('autoComplete="off"');
  });

  it("the redirect URI is prefilled from what the API expects", () => {
    // A mismatch between what is registered with Google and what BOSS listens on is the
    // commonest failure in this flow, and Google reports it as redirect_uri_mismatch — which
    // names neither value.
    expect(UI).toContain("function defaultRedirect");
    expect(UI).toContain("callback_path");
    // The API host, not the console's: the callback is an API route, and sending Google to the
    // web origin 404s AFTER a successful consent, which is the most confusing failure available.
    expect(UI).toContain("boss-api.local.sean.network");
  });

  it("connect is disabled until a client exists", () => {
    // Otherwise the first press produces Google's `invalid_client`, which says nothing about
    // the thing the operator has not done yet.
    const button = UI.slice(UI.indexOf('data-testid="channel-connect"'));
    expect(button.slice(0, 400)).toContain("disabled={busy || !status?.configured}");
  });

  it("consent opens in a new tab", () => {
    // The console holds unsaved state on other screens, and Google's consent ends on a page
    // that is not this app.
    expect(UI).toContain('window.open(String(data.authorization_url), "_blank", "noopener")');
  });

  it("the route takes its action from an allowlist, never from a path", () => {
    // An allowlist checked against a fixed tuple — it grew from two actions to three, so the
    // assertion names the property rather than the exact condition.
    expect(ROUTE).toContain('const ALLOWED = ["save_client", "connect", "disconnect"] as const;');
    expect(ROUTE).toContain("if (!ALLOWED.includes(action as (typeof ALLOWED)[number]))");
    // The upstream PATH is never built from caller input — only query values are, and those
    // are encodeURIComponent'd.
    expect(ROUTE).not.toMatch(/\$\{[^}]*body\.action[^}]*\}/);
  });

  it("nothing on this screen renders a credential", () => {
    // The status endpoint returns a hint, never either half of the client.
    expect(UI).not.toContain("client_secret}</");
    expect(UI).toContain("client_id_hint");
  });
});

describe("the page notices a connection made in another tab", () => {
  it("refetches when the operator comes back", () => {
    // Consent happens on Google's page. The operator switches back to a console that fetched
    // its channel list before any of that happened, so it still says "No channel connected
    // yet" about a channel that IS connected — which is what happened the first time anyone
    // used this, and cost a hard refresh to discover.
    expect(UI).toContain('window.addEventListener("focus", onReturn)');
    expect(UI).toContain('document.addEventListener("visibilitychange", onReturn)');
    // Cleaned up: a listener per mount on a long-lived console is a leak.
    expect(UI).toContain('window.removeEventListener("focus", onReturn)');
  });

  it("and polls briefly after Connect, for the case where focus never left", () => {
    // A second monitor, or a popup the browser dismissed: consent can finish without this tab
    // ever blurring.
    expect(UI).toContain("setAwaiting(10)");
    // BOUNDED. A poll that never stops is a poll that hammers the API for the rest of the
    // session when the operator abandons the consent screen.
    expect(UI).toContain("setAwaiting((n) => n - 1)");
    expect(UI).toContain("if (awaiting <= 0) return;");
    // And it stops the moment it lands, rather than running out the clock.
    expect(UI).toContain('if (found.some((x) => x.status === "active")) setAwaiting(0);');
  });

  it("no longer tells the operator to refresh", () => {
    expect(UI).not.toContain("come back and refresh");
  });
});

/**
 * Reconnecting, which is the weekly path.
 *
 * A testing-mode refresh token expires after seven days. Without a way to re-authorise, the
 * first symptom an operator sees is a publish failing with an auth error — which looks like a
 * bug rather than like a credential that did what it always said it would.
 */
describe("a channel can be reconnected and disconnected", () => {
  it("reconnect re-authorises THIS channel rather than adding another", () => {
    // Same call, differing only in the key: passing the existing one re-authorises in place.
    expect(UI).toContain("const connect = useCallback(async (connectionKey?: string) => {");
    // The channel pane calls it with the CHANNEL'S OWN key; the header button calls it with a
    // fresh one. Same function, and which key is passed is the whole difference.
    expect(UI).toContain("onReconnect={() => void connect(current.connection_key)}");
  });

  it("and connect-another gets a key nobody is using", () => {
    // connection_key is unique per project. Reusing it would make "connect another" overwrite
    // the channel already there.
    expect(UI).toContain("const nextKey = useCallback(");
    expect(UI).toContain("connected.length > 0 ? nextKey() : undefined");
  });

  it("disconnect asks first", () => {
    // It revokes the token at Google as well as locally: recoverable, but it stops every
    // publish until somebody re-consents, which should not happen on a misclick.
    const fn = UI.slice(UI.indexOf("const disconnect = useCallback"));
    expect(fn.slice(0, 600)).toContain("window.confirm");
    expect(fn.slice(0, 600)).toContain("revoke the token at Google");
  });

  it("the route allows exactly the three actions it implements", () => {
    expect(UI.length).toBeGreaterThan(0);
    expect(ROUTE).toContain('const ALLOWED = ["save_client", "connect", "disconnect"] as const;');
  });
});

/**
 * One OAuth client, shared by every channel in the project.
 *
 * "The google oauth client configured is for same existing channel or for new one? The UI is
 * still confusing here."
 *
 * It sat under a card that names a connected channel, so "Google OAuth client" read as
 * belonging to THAT channel — and an operator connecting a second one could not tell whether
 * they also needed a second client. They do not: the client is the Cloud APPLICATION, and every
 * channel authorises against it.
 *
 * FIXED IN PROSE FIRST, AND THE PROSE DID NOT WORK. The card was retitled "Google Cloud client
 * for this project" and given a bold line reading "Shared by every Google channel in this
 * project, not one per channel". The operator then asked the same question a third time — "the
 * client ID I'm providing is for same client or different client" — because LAYOUT OUTRANKS
 * PROSE: a form underneath a heading belongs to that heading, whatever the words say.
 *
 * So the client is now an ITEM IN THE LIST beside the channels, at the same level, selected
 * like they are. Being visibly one object among several, shared, is a claim the layout makes
 * by itself. These tests pin the structure rather than the sentence.
 */
describe("the client is not presented as belonging to a channel", () => {
  it("it is a selectable object in the list, beside the channels", () => {
    expect(UI).toContain('data-testid="channel-pick-client"');
    expect(UI).toContain('className={selected === "client" ? "as-object active" : "as-object"}');
  });

  it("and the list entry says its scope where the eye already is", () => {
    expect(UI).toContain("<b>Google Cloud client</b>");
    expect(UI).toContain("shared by every channel");
  });

  it("the detail pane still says it outright, for whoever reads", () => {
    expect(UI).toContain("One client, every channel");
    expect(UI).toContain("connecting a second channel needs");
  });

  it("and it is no longer nested under a channel card", () => {
    // The `<details>` that used to wrap it sat inside the connections card. A form under a
    // heading that names one channel is the whole defect.
    expect(UI).not.toContain('<details className="stack-card" data-testid="channel-client"');
  });
});
