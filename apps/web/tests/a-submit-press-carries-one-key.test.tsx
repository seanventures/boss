// @vitest-environment happy-dom

import React, { act } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * A server-action form carries ONE key per press.
 *
 * "Stop and run again" pressed twice started two workflows (2026-10-02): `disabled` lands only
 * after React re-renders, so a fast second click submits again. Every SubmitButton now puts the
 * press's key in its form; middleware.ts lifts it and lib/api-write.ts derives the API's
 * Idempotency-Key from it. The key must stay the same for everything submitted while the press is
 * pending, change once it has finished, and never be drawn during render (a server-rendered
 * random value would not match the client's and break hydration).
 */

const status = { pending: false, data: null as FormData | null, method: "post", action: null };
vi.mock("react-dom", async () => {
  const actual = await vi.importActual<typeof import("react-dom")>("react-dom");
  return { ...actual, useFormStatus: () => status };
});
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { SubmitButton } = await import("../app/submit-button");

let root: Root | null = null;
let host: HTMLElement | null = null;

function mount() {
  host = document.createElement("form");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<SubmitButton>Run again</SubmitButton>));
}

function rerender() {
  act(() => root!.render(<SubmitButton>Run again</SubmitButton>));
}

const intent = () =>
  (host!.querySelector('input[name="__boss_intent"]') as HTMLInputElement | null)?.value;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  status.pending = false;
});

describe("a submit press carries one key", () => {
  it("renders no key on the server, so hydration has nothing to disagree about", () => {
    expect(renderToStaticMarkup(<SubmitButton>Run again</SubmitButton>))
      .not.toContain("__boss_intent");
  });

  it("has a key once mounted", () => {
    mount();
    expect(intent()).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("keeps the key while the press is pending, and draws a new one when it finishes", () => {
    mount();
    const before = intent();
    status.pending = true;
    rerender();
    expect(intent()).toBe(before); // a second submit while pending is the same press
    status.pending = false;
    rerender();
    expect(intent()).toMatch(/^[0-9a-f-]{36}$/);
    expect(intent()).not.toBe(before); // the next press is a new intent
  });
});
