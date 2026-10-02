import { existsSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * A failed action says what happened and what to do — it never lands on Next's generic page.
 *
 * Console walk, 2026-10-02: the app had no error.tsx or global-error.tsx, and ~94 server actions
 * return void and throw when the API refuses them. Onboarding an organization with the API down
 * showed "Application error: a server-side exception has occurred" and nothing else; a duplicate
 * name was accepted; a success left the form open and filled.
 */
(globalThis as unknown as { React: typeof React }).React = React;

describe("the console has an error boundary at both levels", () => {
  it("exists", () => {
    expect(existsSync(resolve(__dirname, "../app/error.tsx"))).toBe(true);
    expect(existsSync(resolve(__dirname, "../app/global-error.tsx"))).toBe(true);
  });

  it("says what failed, gives the error id, and offers the ways forward", async () => {
    const { default: ConsoleError } = await import("../app/error");
    const html = renderToStaticMarkup(
      <ConsoleError error={Object.assign(new Error("An error occurred in the Server Components render. The specific message is omitted in production builds"), { digest: "123abc" })}
                    reset={() => {}} />,
    );
    expect(html).toContain("That action did not go through");
    expect(html).toContain("123abc");
    expect(html).toContain("Try again");
    expect(html).toContain("Back to Command Center");
    // The production placeholder sentence is not shown as if it were the reason.
    expect(html).not.toContain("omitted in production builds");
  });

  it("shows the real reason when there is one", async () => {
    const { default: ConsoleError } = await import("../app/error");
    const html = renderToStaticMarkup(
      <ConsoleError error={new Error("API returned 409: name taken")} reset={() => {}} />,
    );
    expect(html).toContain("API returned 409: name taken");
  });
});

describe("onboarding an organization", () => {
  const GATE = codeOnly("app/org-gate.tsx");
  const ACTION = codeOnly("app/org-gate-actions.ts");

  it("renders the action's result instead of throwing past the form", () => {
    expect(GATE).toContain("useActionState<OnboardOrgResult | null, FormData>");
    expect(GATE).toContain('data-testid="org-onboard-error"');
    expect(ACTION).toContain("Nothing was created");
  });

  it("refuses a duplicate name with the way forward", () => {
    expect(ACTION).toContain("already exists. Open it from the list above, or choose a different name.");
  });

  it("opens the new organization when it is done", () => {
    expect(GATE).toContain("if (onboarded?.ok) enter(onboarded.orgId);");
  });
});
