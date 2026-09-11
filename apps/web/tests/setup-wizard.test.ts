import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The one screen a first-time self-hosted operator sees.
 *
 * Setup was documentation: set two environment variables on the API container, restart it, then
 * call four endpoints in the right order. Every endpoint existed; the order did not.
 */
const UI = codeOnly("app/setup-ui.tsx");
const ROUTE = codeOnly("app/api/setup/route.ts");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const EDITION = codeOnly("lib/edition.ts");

describe("setup is reachable before anything else makes sense", () => {
  it("it is an area, in Settings", () => {
    expect(SHELL).toContain('id: "setup"');
    expect(SHELL).toContain('| "setup"');
  });

  it("and the community edition shows it — that is who it is for", () => {
    expect(EDITION).toContain('"setup"');
  });
});

describe("connecting a repository", () => {
  it("takes the token itself, not the name of an env var", () => {
    expect(UI).toContain('data-testid="setup-github-token"');
    expect(UI).toContain("token: token.trim()");
  });

  it("the field is a password field and is wiped after the attempt", () => {
    // A token left in a form survives every screenshot and session restore after it.
    const at = UI.indexOf('data-testid="setup-github-token"');
    expect(UI.slice(at - 200, at)).toContain('type="password"');
    expect(UI).toContain("setToken(\"\")");
  });

  it("and the connection is tested rather than assumed", () => {
    expect(UI).toContain("test_after_link: true");
    expect(UI).toContain("Connect and test");
  });

  it("it says which permissions the token needs", () => {
    // "It didn't work" over a token missing one scope is a support ticket.
    expect(UI).toContain("Contents: read and write");
    expect(UI).toContain("Pull requests: read and write");
  });
});

describe("connecting a model", () => {
  it("offers keys, anything OpenAI-compatible, and subscriptions", () => {
    expect(UI).toContain('data-testid="setup-model-provider"');
    expect(UI).toContain('data-testid="setup-model-key"');
    expect(UI).toContain('data-testid="setup-model-base"');
  });

  it("a subscription gets an explanation, not a key box that would be rejected", () => {
    expect(UI).toContain('data-testid="setup-model-subscription"');
    expect(UI).toContain("{subscription ? null : (");
  });

  it("and the key is wiped after the attempt too", () => {
    expect(UI).toContain("setApiKey(\"\")");
  });
});

describe("the route does not become a way to reach any endpoint", () => {
  it("the two steps are named server-side", () => {
    // Taking a path from the client would let this reach any API endpoint with the console's
    // credentials.
    expect(ROUTE).toContain('step === "github" ? "/v1/github/easy-connect"');
    expect(ROUTE).toContain('step === "model" ? "/v1/setup/model"');
    expect(ROUTE).toContain("step must be 'github' or 'model'");
  });

  it("and the token is posted, never put in a query string", () => {
    expect(ROUTE).toContain("method: \"POST\"");
    expect(ROUTE).not.toContain("token=");
  });
});
