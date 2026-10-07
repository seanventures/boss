import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { DOC_END, DOC_START, GUIDES, guidesMarkdown } from "../lib/identity/sso-guides";
import { KINDS } from "../app/identity/sign-in/provider-form";

/**
 * ONE SOURCE FOR "HOW TO SET THIS UP". The Add sheet reads lib/identity/sso-guides.ts; the doc
 * carries the same steps between its sso-guides markers. SSO_GUIDES_WRITE=1 rewrites the doc.
 */
const DOC = resolve(__dirname, "../../../docs/community/SIGN_IN_SETUP.md");

describe("the sign-in provider guides", () => {
  it("the doc carries exactly the steps the Add sheet shows", () => {
    const doc = readFileSync(DOC, "utf8");
    const a = doc.indexOf(DOC_START);
    const b = doc.indexOf(DOC_END);
    expect(a, "doc markers").toBeGreaterThan(-1);
    const want = `${DOC_START}\n\n${guidesMarkdown()}\n${DOC_END}`;
    if (process.env.SSO_GUIDES_WRITE === "1") writeFileSync(DOC, doc.slice(0, a) + want + doc.slice(b + DOC_END.length));
    else expect(doc.slice(a, b + DOC_END.length)).toBe(want);
  });

  it("every kind the sheet offers has a guide, short and in plain steps", () => {
    for (const k of KINDS) expect(GUIDES[k.kind as keyof typeof GUIDES], k.kind).toBeDefined();
    for (const [kind, g] of Object.entries(GUIDES)) {
      expect(g.steps.length, kind).toBeGreaterThanOrEqual(3);
      expect(g.steps.length, kind).toBeLessThanOrEqual(5);
      expect(g.steps.some((s) => s.text.includes("{redirect}")), `${kind}: the redirect step`).toBe(true);
      expect(g.copyBack.length, kind).toBeGreaterThan(0);
      for (const s of g.steps) if (s.link) expect(s.link.href, kind).toMatch(/^https:\/\//);
    }
  });

  it("names the gotchas that trip people up", () => {
    expect(GUIDES.google.gotcha).toMatch(/Test users/);
    expect(GUIDES.microsoft.gotcha).toMatch(/tenant/);
    expect(GUIDES.github.gotcha).toMatch(/one callback/);
    expect(GUIDES.saml.gotcha).toMatch(/unsigned assertions/);
    expect(GUIDES.google.steps.map((s) => s.link?.href)).toContain("https://console.cloud.google.com/auth/clients/create");
    expect(GUIDES.github.steps[0].link?.href).toBe("https://github.com/settings/applications/new");
  });
});
