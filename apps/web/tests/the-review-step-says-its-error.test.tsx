/**
 * A review step says its error in the step, not only in the title.
 * A trimmed error is the sentence. Blank is nothing. A sentence over six
 * words is a paragraph. The step name and the step word stay.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppSecurity, stepErrorSentence } from "../app/factory/security/app-security";
import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the scanner could not read the lockfile today";
const SIX = "one two three four five six";
const SHORT = "disk full";

const REVIEW = {
  run_id: "wfr_err", status: "failed", app_key: "vulnapp", created_at: "2026-11-16T00:00:00Z",
  steps: [
    { id: "scan", status: "succeeded", error: "   " },
    { id: "panel", status: "failed", error: SIX },
    { id: "fix", status: "failed", note: "round one", error: `  ${LONG}  ` },
    { id: "rescan", status: "failed", error: SHORT },
    { id: "report", status: "skipped", error: "" },
  ],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).replace(/^https?:\/\/[^/]+/, "");
    if (path.startsWith("/v1/security/scans?")) return json({ scans: [] });
    if (path.startsWith("/v1/security/reviews?")) return json({ reviews: [REVIEW] });
    return json({ error: "not_stubbed", path }, 404);
  }));
});

const ME = {
  principal_id: "usr_x", kind: "user", display_name: "Vee", email: "v@acme.dev",
  org: { id: "org_1", name: "Acme" }, status: "active", access_roles: ["viewer"],
  sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c",
  epoch: 1, network_bound: false, banners: [], avatar_seed: null,
} as MeResponse;

function perms(): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS.viewer], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
});

async function show(): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <IdentityProvider me={ME} org={perms()}>
        <AppSecurity projectId="p1" appKey="vulnapp" />
      </IdentityProvider>,
    );
  });
  for (let i = 0; i < 8; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  return host;
}

function stepNamed(name: string): HTMLElement {
  const li = [...host!.querySelectorAll("li.sec-step")].find((el) =>
    el.querySelector(".sec-step-name")?.textContent === name);
  expect(li, name).toBeTruthy();
  return li as HTMLElement;
}

function sentenceEl(li: HTMLElement, sentence: string): Element | undefined {
  return [...li.querySelectorAll("p, span")].find((el) => el.textContent === sentence);
}

describe("stepErrorSentence", () => {
  it("is the trimmed error, or nothing", () => {
    expect(stepErrorSentence(`  ${LONG}  `)).toBe(LONG);
    expect(stepErrorSentence(SHORT)).toBe(SHORT);
    expect(stepErrorSentence(SIX)).toBe(SIX);
    expect(stepErrorSentence(null)).toBeNull();
    expect(stepErrorSentence(undefined)).toBeNull();
    expect(stepErrorSentence("")).toBeNull();
    expect(stepErrorSentence(" \n\t ")).toBeNull();
  });
});

describe("the review step says its error", () => {
  it("shows the sentence in the step, as a paragraph when it is longer than six words", async () => {
    await show();
    const fix = stepNamed("Fix");
    expect(fix.querySelector(".sec-step-name")?.textContent).toBe("Fix");
    expect(fix.querySelector(".sec-step-word")?.textContent).toBe("Failed");
    expect(fix.getAttribute("title")).toContain(LONG);
    expect(sentenceEl(fix, LONG)?.tagName).toBe("P");

    const six = stepNamed("Panel");
    expect(six.querySelector(".sec-step-word")?.textContent).toBe("Failed");
    expect(sentenceEl(six, SIX)?.tagName).toBe("SPAN");

    const short = stepNamed("Re-scan");
    expect(short.querySelector(".sec-step-name")?.textContent).toBe("Re-scan");
    expect(short.querySelector(".sec-step-word")?.textContent).toBe("Failed");
    expect(sentenceEl(short, SHORT)?.tagName).toBe("SPAN");

    const blank = stepNamed("Scan");
    expect(blank.querySelector(".sec-step-name")?.textContent).toBe("Scan");
    expect(blank.querySelector(".sec-step-word")?.textContent).toBe("Done");
    expect(blank.querySelector("p")).toBeNull();
    expect(sentenceEl(blank, "   ")).toBeUndefined();

    const skipped = stepNamed("Report");
    expect(skipped.querySelector(".sec-step-word")?.textContent).toBe("Skipped");
    expect(skipped.querySelector("p")).toBeNull();
  });
});
