// @vitest-environment happy-dom
/**
 * "Retry with a note". A build step's failure says "retry the build with a note on what to fix",
 * and the step's Try again sent nothing — the builder re-ran blind. Try again now opens one
 * optional field first; the note goes with the retry, and a retry without one still works.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";
import { FailedStepActions, RETRY_NOTE_LIMIT, retryExample, type RunAction, type RunStep } from "../app/delivery-workflows-ui";
import { codeOnly } from "./_helpers/source";

const step = { step_id: "build", kind: "capability", status: "failed",
               capability: "build" } as unknown as RunStep;
const retry: RunAction = { key: "retry_step", step_id: "build", label: "Try Build again",
                           enabled: true, reason: null };

afterEach(() => cleanup());

async function open(onRetry: (note?: string) => void) {
  await render(<FailedStepActions step={step} busy={null} actions={{ retry }}
                                  onRetry={onRetry} onSkip={() => {}} />);
  expect($('[data-testid="dw-step-retry-form"]')).toBeNull();
  await click('[data-testid="dw-step-retry"]');
  expect($('[data-testid="dw-step-retry-form"]')).not.toBeNull();
}

describe("Retry with a note", () => {
  it("asks what should change before sending, and sends the note", async () => {
    const onRetry = vi.fn();
    await open(onRetry);
    const label = $('[data-testid="dw-step-retry-form"] label')!;
    expect(label.textContent).toContain("What should change? (optional)");
    expect(label.getAttribute("title")).toBeTruthy();
    expect($('[data-testid="dw-step-retry-note"]')!.getAttribute("maxlength"))
      .toBe(String(RETRY_NOTE_LIMIT));
    expect(onRetry).not.toHaveBeenCalled();
    await type('[data-testid="dw-step-retry-note"]', "  fix the login redirect  ");
    await click('[data-testid="dw-step-retry-confirm"]');
    expect(onRetry).toHaveBeenCalledWith("fix the login redirect");
    expect($('[data-testid="dw-step-retry-form"]')).toBeNull();
  });

  it("retries without a note when the field is left empty", async () => {
    const onRetry = vi.fn();
    await open(onRetry);
    await click('[data-testid="dw-step-retry-confirm"]');
    expect(onRetry).toHaveBeenCalledWith(undefined);
  });

  it("the console sends the note in the retry_step body, and only when there is one", () => {
    const UI = codeOnly("app/delivery-workflows-ui.tsx");
    // (codeOnly blanks an empty `{}`, so the assertion stops before it.)
    expect(UI).toContain('act("Retry", "retry_step", { run_id: runId, step_id: stepId, ...(note ? { note } :');
  });
});

describe("One Retry on screen (operator, 16 Oct)", () => {
  const deploy = { step_id: "deploy", kind: "capability", status: "failed",
                   capability: "deploy" } as unknown as RunStep;
  const retryDeploy: RunAction = { key: "retry_step", step_id: "deploy", label: "Retry",
                                   enabled: true, reason: null };
  const skipDeploy: RunAction = { key: "skip_step", step_id: "deploy", label: "Skip",
                                  enabled: true, reason: null };

  it("the panel replaces the Retry and Skip buttons, and Cancel brings them back", async () => {
    await render(<FailedStepActions step={deploy} busy={null}
                                    actions={{ retry: retryDeploy, skip: skipDeploy }}
                                    onRetry={() => {}} onSkip={() => {}} />);
    await click('[data-testid="dw-step-retry"]');
    expect($('[data-testid="dw-step-retry"]')).toBeNull();
    expect($('[data-testid="dw-step-skip"]')).toBeNull();
    const retries = Array.from(document.querySelectorAll("button"))
      .filter((b) => /retry/i.test(b.textContent ?? ""));
    expect(retries).toHaveLength(1);
    expect($('[data-testid="dw-step-retry-confirm"]')!.textContent).toContain("Retry deploy");
    await click('[data-testid="dw-step-retry-form"] button.linklike');
    expect($('[data-testid="dw-step-retry"]')).not.toBeNull();
    expect($('[data-testid="dw-step-skip"]')).not.toBeNull();
  });

  it("the example fits the step: a deploy is not fixed by a login test", async () => {
    expect(retryExample("deploy")).toBe("e.g. deploy on port 8080");
    expect(retryExample("build")).toContain("login test");
    expect(retryExample("research")).toBe("e.g. what to do differently this time");
    await render(<FailedStepActions step={deploy} busy={null} actions={{ retry: retryDeploy }}
                                    onRetry={() => {}} onSkip={() => {}} />);
    await click('[data-testid="dw-step-retry"]');
    expect($('[data-testid="dw-step-retry-note"]')!.getAttribute("placeholder"))
      .toBe("e.g. deploy on port 8080");
  });

  it("the skip panel replaces the buttons the same way", async () => {
    await render(<FailedStepActions step={deploy} busy={null}
                                    actions={{ retry: retryDeploy, skip: skipDeploy }}
                                    onRetry={() => {}} onSkip={() => {}} />);
    await click('[data-testid="dw-step-skip"]');
    expect($('[data-testid="dw-step-skip-form"]')).not.toBeNull();
    expect($('[data-testid="dw-step-retry"]')).toBeNull();
    expect($('[data-testid="dw-step-skip"]')).toBeNull();
  });
});
