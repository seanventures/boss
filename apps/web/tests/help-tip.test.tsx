// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { HelpTip } from "../app/help-tip";

let host: HTMLDivElement;
let root: Root;

function trigger() {
  return host.querySelector<HTMLButtonElement>(".help-tip-trigger")!;
}

function tooltip() {
  return document.querySelector<HTMLElement>("[role=tooltip]");
}

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root.render(<HelpTip label="Agents at once"
    text="How many coding agents can run together." />));
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

describe("HelpTip", () => {
  it("has a named button and describes it only while help is open", async () => {
    expect(trigger().getAttribute("aria-label")).toBe("Help: Agents at once");
    expect(trigger().getAttribute("aria-expanded")).toBe("false");
    expect(tooltip()).toBeNull();

    await act(async () => trigger().dispatchEvent(
      new PointerEvent("pointerover", { bubbles: true, pointerType: "mouse" }),
    ));
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    expect(tooltip()?.textContent).toBe("How many coding agents can run together.");
    expect(trigger().getAttribute("aria-describedby")).toBe(tooltip()?.id);

    await act(async () => trigger().dispatchEvent(
      new PointerEvent("pointerout", { bubbles: true, pointerType: "mouse" }),
    ));
    expect(tooltip()).toBeNull();
    expect(trigger().hasAttribute("aria-describedby")).toBe(false);
  });

  it("opens on keyboard focus and closes with Escape", async () => {
    await act(async () => trigger().focus());
    expect(tooltip()).not.toBeNull();
    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Escape", bubbles: true,
    })));
    expect(tooltip()).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("toggles on click or touch and closes when pressed outside", async () => {
    await act(async () => trigger().click());
    expect(tooltip()).not.toBeNull();
    await act(async () => document.body.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true, pointerType: "touch",
    })));
    expect(tooltip()).toBeNull();

    await act(async () => trigger().click());
    expect(tooltip()).not.toBeNull();
    await act(async () => trigger().click());
    expect(tooltip()).toBeNull();
  });
});
