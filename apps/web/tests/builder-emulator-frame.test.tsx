// @vitest-environment happy-dom
/**
 * BOSSAPP-1260: the builder preview streams one local frame and will not
 * claim a device farm or a real store. An empty signing key never uploads.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmulatorFrame, type UploadResult } from "../app/builder/emulator-frame";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLElement | null = null;
let root: Root | null = null;

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  host?.remove();
  host = null;
  root = null;
  vi.useRealTimers();
});

async function render(upload: (body: unknown) => Promise<UploadResult>, interval = 500) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <EmulatorFrame artifactId="art_preview" frameIntervalMs={interval} upload={upload} />,
    );
  });
}

function glass(): HTMLElement {
  const node = host!.querySelector("[data-frame-index]") as HTMLElement | null;
  if (!node) throw new Error("preview glass is missing");
  return node;
}

describe("emulator preview frame", () => {
  it("is a local preview with a platform switch, not a device farm", async () => {
    vi.useFakeTimers();
    await render(vi.fn());
    const surface = host!.querySelector("[data-surface='preview']") as HTMLElement;
    expect(surface.getAttribute("data-device-farm")).toBe("false");
    expect(surface.getAttribute("data-real-store")).toBe("false");
    expect(surface.querySelector("p")?.textContent).toMatch(/not a device farm/i);
    expect(surface.textContent).toMatch(/not a real store/i);
    expect(surface.textContent).not.toMatch(/device farm is (ready|connected|live)/i);
    expect(surface.textContent).not.toMatch(/uploaded to (the )?(App Store|Google Play)/i);
    expect(glass().getAttribute("data-frame-platform")).toBe("ios");
    expect(glass().getAttribute("data-frame-index")).toBe("1");
    expect(glass().getAttribute("data-bars")).toBe("5,4,3,2,10");

    const android = host!.querySelector("[data-platform-choice='android']") as HTMLButtonElement;
    await act(async () => {
      android.click();
    });
    expect(host!.querySelector("[data-platform]")?.getAttribute("data-platform")).toBe("android");
    expect(glass().getAttribute("data-frame-platform")).toBe("android");
    expect(glass().getAttribute("data-frame-index")).toBe("1");
    expect(glass().getAttribute("data-bars")).toBe("9,8,7,6,5");
  });

  it("streams the next painted frame on the selected platform", async () => {
    vi.useFakeTimers();
    await render(vi.fn(), 500);
    await act(async () => {
      vi.advanceTimersByTime(500);
    });
    expect(glass().getAttribute("data-frame-index")).toBe("2");
    expect(glass().getAttribute("data-bars")).toBe("8,7,6,5,4");
    expect(host!.querySelectorAll(".emf-cell").length).toBe(2);
  });
});

describe("signed upload", () => {
  it("refuses an empty signing key and writes no success receipt", async () => {
    const upload = vi.fn();
    await render(upload);
    const button = host!.querySelector("button[type='submit']") as HTMLButtonElement;
    await act(async () => {
      button.click();
    });
    expect(upload).not.toHaveBeenCalled();
    expect(host!.querySelector("[role='alert']")?.textContent).toMatch(/signing key/i);
    expect(host!.querySelector("[data-receipt]")).toBeNull();
  });

  it("sends one signed upload and shows the receipt it got back", async () => {
    const upload = vi.fn(async () => ({
      ok: true as const,
      receipt: {
        id: "rcpt_bench",
        dedupe_key: "store-upload:ios:art_preview:abcd",
        status: "accepted" as const,
        device_farm: false,
        real_store: false,
      },
    }));
    await render(upload);
    const input = host!.querySelector("input") as HTMLInputElement;
    const button = host!.querySelector("button[type='submit']") as HTMLButtonElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      set?.call(input, "test-signing-key");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      button.click();
    });
    expect(upload).toHaveBeenCalledTimes(1);
    expect(upload.mock.calls[0][0]).toMatchObject({
      platform: "ios",
      artifactId: "art_preview",
      signingKey: "test-signing-key",
    });
    const receipt = host!.querySelector("[data-receipt]") as HTMLElement;
    expect(receipt.getAttribute("data-receipt")).toBe("store-upload:ios:art_preview:abcd");
    expect(receipt.textContent).toMatch(/fake store accepted/i);
  });

  it("shows a store refusal and does not invent a success receipt", async () => {
    const upload = vi.fn(async () => ({
      ok: false as const,
      error: "The fake store refused the upload.",
    }));
    await render(upload);
    const input = host!.querySelector("input") as HTMLInputElement;
    const button = host!.querySelector("button[type='submit']") as HTMLButtonElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      set?.call(input, "test-signing-key");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      button.click();
    });
    expect(upload).toHaveBeenCalledTimes(1);
    expect(host!.querySelector("[role='alert']")?.textContent).toMatch(/refused the upload/i);
    expect(host!.querySelector("[data-receipt]")).toBeNull();
  });
});
