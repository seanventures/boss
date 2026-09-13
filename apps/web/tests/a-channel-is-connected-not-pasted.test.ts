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
    expect(ROUTE).toContain('if (action !== "save_client" && action !== "connect")');
    // No interpolation of caller input into the upstream path.
    expect(ROUTE).not.toMatch(/\$\{[^}]*body\.[^}]*\}`/);
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
