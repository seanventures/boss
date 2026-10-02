import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { IdentityProvidersCatalogView } from "../app/identity-providers-ui";
import { communityNavCopy } from "../lib/edition";
import { API_AUTH_TRUTH, CONSOLE_SIGN_IN_TRUTH } from "../lib/sign-in-truth";

/**
 * Identity says what sign-in actually uses, the same way in v1 and v2, with no invented ratings.
 *
 * Console walk on boss-1, 2026-10-02: the area was subtitled "Who can sign in — OIDC, SAML, SCIM"
 * while sign-in is the installer's single password and never consults a provider; provider cards
 * showed "★ 4.8" ratings nobody could source and "GET /v1/identity-providers/okta" as their
 * setup step; v2 Settings said "authenticates with an API key. There is no SAML or SCIM to
 * configure" while v1 offered SAML and SCIM forms.
 */
(globalThis as unknown as { React: typeof React }).React = React;

const feed = {
  apiReachable: true,
  catalog: [{
    id: "okta", name: "Okta", stars: 4.8, protocol_support: ["oidc", "saml", "scim"],
    category: "workforce_idp", market_notes: "Common workforce IdP.", docs_url: "https://example.test/okta",
    status: "available",
  }],
  projectStatus: [],
  configuredCount: 0,
  standards: ["OIDC"],
} as never;

describe("the Identity area", () => {
  const html = renderToStaticMarkup(<IdentityProvidersCatalogView feed={feed} projectId="p" />);

  it("says how sign-in works today before offering any provider", () => {
    expect(html).toContain('data-testid="identity-sign-in-truth"');
    expect(html).toContain("one password the installer generated");
    expect(html.indexOf("identity-sign-in-truth")).toBeLessThan(html.indexOf("Okta"));
  });

  it("shows no rating and no endpoint, and offers the form instead", () => {
    expect(html).not.toContain("★");
    expect(html).not.toContain("4.8");
    expect(html).not.toContain("/v1/");
    expect(html).toContain('href="#identity-provider-form"');
    const page = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
    expect(page).toContain('id="identity-provider-form"');
  });

  it("is not subtitled as if providers decided who can sign in", () => {
    expect(communityNavCopy("enterprise").sub).not.toMatch(/Who can sign in/);
    expect(communityNavCopy("enterprise").sub).toMatch(/installer password/);
  });
});

describe("v1 and v2 give one answer", () => {
  it("the engine's v2 Settings reason is the same sentence", () => {
    const engine = readFileSync(
      resolve(__dirname, "../../../engine/boss_engine/services/console_v2_platform.py"), "utf8");
    // The Python literal is split over lines; join its pieces and compare the words.
    const block = engine.slice(engine.indexOf("SIGN_IN_TRUTH = ("), engine.indexOf(")", engine.indexOf("SIGN_IN_TRUTH = (")));
    const joined = [...block.matchAll(/"([^"]*)"/g)].map((m) => m[1]).join("");
    expect(joined).toBe(CONSOLE_SIGN_IN_TRUTH);
    expect(engine).not.toContain("There is no SAML or SCIM to configure");
  });

  it("names how the API authenticates", () => {
    expect(API_AUTH_TRUTH).toContain("BOSS_OIDC_ENABLED");
  });
});
