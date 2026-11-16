// @vitest-environment happy-dom
/**
 * BOSSAPP-1261: one prompt, one model, the output, and the receipt id.
 * Not a side-by-side comparison (that is 1262).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { PlaygroundScreen } from "../app/playground/screen";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the prompt playground", () => {
  it("shows one prompt, one model, and no comparison columns", async () => {
    await render(
      <PlaygroundScreen projectId="prj_harbor" model="harbor-7b" run={async () => {
        throw new Error("not run");
      }} />,
    );
    expect(document.querySelectorAll("[data-testid='playground-prompt']")).toHaveLength(1);
    expect(document.querySelectorAll("[data-testid='playground-model']")).toHaveLength(1);
    expect($("[data-testid='playground-model']")!.getAttribute("value")
      ?? ($("[data-testid='playground-model']") as HTMLInputElement).value).toBe("harbor-7b");
    expect($("[data-testid='playground-screen']")!.getAttribute("data-layout")).toBe("stack");
    expect(document.querySelector("[data-testid='playground-compare']")).toBeNull();
    expect($("[data-testid='playground-output']")!.textContent).toMatch(/No output yet/);
    expect($("[data-testid='playground-receipt']")).toBeNull();
    expect(($("[data-testid='playground-run']") as HTMLButtonElement).disabled).toBe(true);
  });

  it("runs the prompt and shows the output and the receipt id", async () => {
    const run = vi.fn(async () => ({
      prompt: "Describe the harbor.",
      model: "harbor-7b",
      output: "The harbor is quiet.",
      receipt_id: "rcpt_harbor_1",
    }));
    await render(<PlaygroundScreen projectId="prj_harbor" model="harbor-7b" run={run} />);
    await type("[data-testid='playground-prompt']", "Describe the harbor.");
    await click("[data-testid='playground-run']");
    expect(run).toHaveBeenCalledTimes(1);
    expect(run.mock.calls[0][0]).toMatchObject({
      projectId: "prj_harbor",
      prompt: "Describe the harbor.",
      model: "harbor-7b",
    });
    expect($("[data-testid='playground-output']")!.textContent).toContain("The harbor is quiet.");
    expect($("[data-testid='playground-receipt']")!.textContent).toContain("rcpt_harbor_1");
    expect(document.querySelectorAll("[data-testid='playground-output']")).toHaveLength(1);
  });

  it("shows a gateway failure as a paragraph", async () => {
    await render(
      <PlaygroundScreen
        projectId="prj_harbor"
        model="harbor-7b"
        run={async () => {
          throw new Error("gateway refused the model");
        }}
      />,
    );
    await type("[data-testid='playground-prompt']", "Break.");
    await click("[data-testid='playground-run']");
    const error = $("[data-testid='playground-error']");
    expect(error!.tagName).toBe("P");
    expect(error!.textContent).toContain("gateway refused the model");
    expect($("[data-testid='playground-receipt']")).toBeNull();
  });
});
