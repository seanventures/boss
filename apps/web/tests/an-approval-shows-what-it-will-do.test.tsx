// @vitest-environment happy-dom
/**
 * An approval card shows what approving will do — the email, the tickets, the video — not a
 * workflow key, a run id and "staging".
 *
 * Found running the bundled examples live: "Email the newsletter: weekly_newsletter · run
 * 4715a965" with "Approve and retry", and no recipient, subject or body anywhere. The engine now
 * stores a typed preview on the decision (services/action_preview.py); these mount the real card
 * and the real preview against each kind.
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
const { ActionPreview, actionPreviewOf, previewLead } = await import("../app/action-preview");

const BODY = "AI\nModels got cheaper this week (source: The Register).\n\nFootball\n" + "More. ".repeat(400);

const EMAIL = {
  kind: "email", skill_id: "email.send_real_guarded", step_label: "Email the newsletter",
  to: ["reader@example.com"], cc: [], subject: "Your weekly newsletter", body: BODY,
  body_truncated: false, attachments: ["report.pdf"], content_hash: "sha256:abc123",
  redactions: 0,
};

function gate(preview: Record<string, unknown>): DecisionFeedItem {
  return {
    id: "dec_gate", status: "pending", riskTier: 2,
    question: "Email the newsletter — E2E: Weekly newsletter",
    recommendation: {
      capability: "email_the_newsletter", step_id: "send", workflow_run_id: "wfr_4715a965",
      step_label: "Email the newsletter", run_title: "E2E: Weekly newsletter",
      gate_completes_step: false, action_preview: preview,
    },
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

describe("the Approvals card shows the email it would send", () => {
  it("reads as an email: To, Subject, attachments, then the full body in a scroll box", () => {
    const page = mount(
      <DecisionInbox feed={{ projectId: "p", apiReachable: true, items: [gate(EMAIL)] }}
                     tasks={[]} roles={[]} projectId="p" actorId="ceo" />);
    const card = page.querySelector('[data-testid="decision-row"]')!;
    expect(card.querySelector(".dq-title")!.textContent)
      .toBe("Email the newsletter — E2E: Weekly newsletter");
    expect(card.querySelector(".dq-rationale")!.textContent).toBe("Approving sends this email.");
    expect(card.querySelector('[data-testid="ap-to"] dd')!.textContent).toBe("reader@example.com");
    expect(card.querySelector('[data-testid="ap-subject"] dd')!.textContent)
      .toBe("Your weekly newsletter");
    expect(card.textContent).toContain("report.pdf");
    const body = card.querySelector('[data-testid="ap-body"]')!;
    expect(body.textContent).toBe(BODY);
    expect(body.className).toContain("ap-body");
    // The paragraph about the step is replaced by the email, and nothing says "staging".
    expect(card.querySelector(".dq-effect")).toBeNull();
    expect(card.textContent).not.toContain("staging");
    expect(card.textContent).not.toContain("Approve and retry");
    // The approval carries the content it is of.
    const hash = card.querySelector('input[name="content_hash"]') as HTMLInputElement;
    expect(hash.value).toBe("sha256:abc123");
    const labels = [...card.querySelectorAll("button")].map((b) => b.textContent);
    expect(labels).toEqual(expect.arrayContaining(["Approve", "Reject"]));
  });

  it("a card with no preview keeps its old sentence and sends no hash", () => {
    const plain = gate({});
    delete (plain.recommendation as Record<string, unknown>).action_preview;
    const page = mount(<DecisionInbox feed={{ projectId: "p", apiReachable: true, items: [plain] }}
                                      tasks={[]} roles={[]} projectId="p" actorId="ceo" />);
    expect(page.querySelector('[data-testid="action-preview"]')).toBeNull();
    expect(page.querySelector('input[name="content_hash"]')).toBeNull();
    expect(page.querySelector(".dq-effect")).not.toBeNull();
  });
});

describe("each kind of preview draws as itself", () => {
  it("tickets as a compact list with owner and timing", () => {
    const p = actionPreviewOf({ kind: "issues", count: 3, tracker: "Plane", items: [
      { title: "Ship the onboarding fix", owner: "Priya", due: "Friday", excerpt: "Step 3 drops." },
      { title: "Book the venue", owner: null, excerpt: "" },
    ] })!;
    const page = mount(<ActionPreview preview={p} />);
    const items = page.querySelectorAll('[data-testid="action-preview-issues"] li');
    expect(items[0].textContent).toContain("Ship the onboarding fix");
    expect(items[0].textContent).toContain("Priya");
    expect(items[0].getAttribute("title")).toBe("Step 3 drops.");
    expect(items[2].textContent).toBe("and 1 more");
    expect(previewLead(p)).toBe("Approving creates these 3 tickets in Plane.");
  });

  it("a publish as a small card: title, visibility, file and description", () => {
    const p = actionPreviewOf({ kind: "publish", platform: "YouTube", title: "Rain",
      description: "A short film about rain.", visibility: "private",
      media: { id: "art_1", name: "rain_final.mp4", link: "https://drive.example/rain" } })!;
    const page = mount(<ActionPreview preview={p} />);
    const card = page.querySelector('[data-testid="action-preview-publish"]')!;
    expect(card.querySelector('[data-testid="ap-title"] dd')!.textContent).toBe("Rain");
    expect(card.textContent).toContain("private");
    expect((card.querySelector("a") as HTMLAnchorElement).href).toBe("https://drive.example/rain");
    expect(card.querySelector('[data-testid="ap-description"]')!.textContent)
      .toBe("A short film about rain.");
    expect(previewLead(p)).toBe("Approving publishes this to YouTube.");
  });

  it("anything else as labelled fields, with hidden credentials said and tooltipped", () => {
    const p = actionPreviewOf({ kind: "fields", redactions: 1, fields: [
      { name: "channel", label: "Channel", value: "#launch" },
      { name: "api_key", label: "Api key", value: "[hidden]", redacted: true },
    ] })!;
    const page = mount(<ActionPreview preview={p} />);
    const rows = page.querySelectorAll('[data-testid="action-preview-fields"] .ap-row');
    expect(rows[0].textContent).toBe("Channel#launch");
    expect(rows[1].querySelector("dt")!.getAttribute("title")).toContain("credential");
    expect(page.querySelector('[data-testid="ap-redacted"]')!.textContent)
      .toBe("1 credential-like value hidden");
  });

  it("ignores something that is not a preview", () => {
    expect(actionPreviewOf(null)).toBeNull();
    expect(actionPreviewOf({ kind: "spaceship" })).toBeNull();
  });
});
