// @vitest-environment happy-dom
/**
 * A held tool decision shows the arguments that were actually shown, with secrets already
 * replaced, and the digest of that call. The raw arguments stay off the card.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DecisionFeedItem } from "../lib/decisions";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));
vi.mock("../app/actions", () => ({
  createDecisionDigestAction: vi.fn(), resolveDecisionAction: vi.fn(),
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", { ...props, type: props.type ?? "submit" }, props.children),
}));
vi.mock("../app/client-relative-time", () => ({
  ClientRelativeTime: ({ iso }: { iso?: string }) => React.createElement("time", null, iso ?? ""),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { DecisionInbox } = await import("../app/decision-inbox-ui");

const DIGEST = "520fdccd4b173d629f76cc8d2c29a597b897e4edfb2fdf09f3e7b4151094a589";

function held(preview: Record<string, unknown> | undefined): DecisionFeedItem {
  const recommendation: Record<string, unknown> = {
    action: "resume_agent_run",
    tool: "write_file",
    args: { path: "a.py", password: "hunter2" },
  };
  if (preview) {
    recommendation.args_preview = preview;
    recommendation.args_sha256 = DIGEST;
  }
  return {
    id: "dec_held", status: "pending", riskTier: 2,
    question: "Approve write_file?",
    recommendation,
    options: [{ action: "approve", label: "Approve" }, { action: "reject", label: "Reject" }],
    allowedActions: [
      { action: "approve", label: "Approve", tone: "approve", effect: "", needs: null },
      { action: "reject", label: "Reject", tone: "reject", effect: "", needs: null },
    ],
    costSummary: {}, createdAt: "2026-10-03T00:00:00Z", updatedAt: "2026-10-03T00:00:00Z",
  } as unknown as DecisionFeedItem;
}

let host: HTMLElement;
let root: Root;
beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function mount(node: React.ReactElement) {
  act(() => root.render(node));
  return host;
}

function valueOf(list: Element, key: string): string | null {
  const label = [...list.querySelectorAll("dt")].find((dt) => dt.textContent === key);
  return label?.nextElementSibling?.textContent ?? null;
}

describe("a held tool decision shows the redacted arguments", () => {
  it("shows each preview value and the digest, not the secret that was replaced", () => {
    const page = mount(
      <DecisionInbox
        feed={{ projectId: "p", apiReachable: true, items: [held({ path: "a.py", password: "[redacted]" })] }}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" />);
    const list = page.querySelector('[data-testid="held-arguments"]');
    expect(list).not.toBeNull();
    expect(list!.tagName).toBe("DL");
    expect(valueOf(list!, "path")).toBe("a.py");
    expect(valueOf(list!, "password")).toBe("[redacted]");
    expect(list!.textContent).not.toContain("hunter2");
    expect(page.querySelector('[data-testid="held-args-sha"]')!.textContent)
      .toContain(DIGEST);
  });

  it("a decision with no args_preview does not render the held arguments", () => {
    const page = mount(
      <DecisionInbox
        feed={{ projectId: "p", apiReachable: true, items: [held(undefined)] }}
        tasks={[]} roles={[]} projectId="p" actorId="ceo" />);
    expect(page.querySelector('[data-testid="held-arguments"]')).toBeNull();
  });
});
