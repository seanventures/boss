import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { InfraScreen } from "../components/v2/InfraScreen";
import type { InfraView, V2Target } from "../lib/v2/platform";

/**
 * Infra & Models, Settings. (The v2 Workflows screen is retired: /v2/boss opens the console
 * Library — tests/legacy-areas.test.ts.)
 *
 * The reference promises more than this deployment has: workflow versions, fleet load, SSO,
 * human seats, listable API keys. Every one of those is reported as absent WITH a reason.
 * A settings page full of controls that cannot act teaches the operator the console is
 * decorative — which then applies to the controls that DO work.
 */

const infra = (over: Partial<InfraView> = {}): InfraView => ({
  project_id: "boss-dogfood",
  hosts: [{ id: "h1", name: "reference host", address: "10.0.0.1", arch: "arm64", status: "verified", verified: true }],
  targets: [],
  models: [{ model: "claude-opus-5", calls: 12, tokens: 90000, spend_usd: 4.5, estimated: false }],
  integrations: [],
  load_recorded: false,
  load_note: "BOSS records host verification, not utilisation. There is no load to show.",
  apiReachable: true,
  ...over,
});

describe("Infra & Models", () => {
  const html = (v: InfraView) => renderToStaticMarkup(<InfraScreen view={v} />);

  it("never shows a fleet load, and says why", () => {
    // A fabricated 62% is indistinguishable from a real one. This console has already shipped
    // one number that was quietly made up.
    const out = html(infra());
    expect(out).toContain('data-testid="v2-infra-load-note"');
    expect(out).toMatch(/not utilisation/);
    expect(out).not.toMatch(/\d+% load/);
  });

  it("an unverified host says it cannot take a deployment", () => {
    // Showing them alike offers a destination that fails at deploy time, far from the choice.
    const out = html(infra({
      hosts: [{ id: "h2", name: "spare", address: "10.0.0.2", arch: null, status: "pending", verified: false }],
    }));
    expect(out).toContain('data-testid="v2-host-unverified-h2"');
    expect(out).toMatch(/cannot take a deployment/);
  });

  it("no hosts says nothing can be deployed, rather than nothing at all", () => {
    const out = html(infra({ hosts: [] }));
    expect(out).toContain('data-testid="v2-infra-no-hosts"');
    expect(out).toMatch(/will not invent one/);
  });

  it("shows model spend, which IS attributable", () => {
    // An LLM call records its own model. Per-agent spend is not shown because nothing records
    // which agent a call belongs to — the same rule, applied both ways.
    expect(html(infra())).toContain('data-testid="v2-model-claude-opus-5"');
  });

  it("an estimated price is flagged rather than shown as measured", () => {
    const out = html(infra({
      models: [{ model: "new-model", calls: 1, tokens: 10, spend_usd: 0.01, estimated: true }],
    }));
    expect(out).toContain('data-testid="v2-model-estimated-new-model"');
    expect(out).toMatch(/not in the price map/);
  });

  it("an integration shows its risk tier and data boundary", () => {
    // The name alone makes a tier-3 external server look like a tier-1 internal one.
    const out = html(infra({
      integrations: [{ id: "s1", name: "github", url: "https://x", transport: "http", enabled: true, risk_tier: 2, data_boundary: "external" }],
    }));
    expect(out).toContain('data-testid="v2-integration-risk-s1"');
    expect(out).toContain('data-testid="v2-integration-boundary-s1"');
    expect(out).toMatch(/external/);
  });
});

// ---------------------------------------------------------------------------
// Apps and where they deploy
//
// The operator compared this screen to v1's CI/CD, which puts each app on a card with its host
// and a link to the running thing. v2 listed a target and an ssh string, so there was no way to
// reach the app -- while the app was plainly up at an address sitting unread in the database.
// ---------------------------------------------------------------------------
describe("deployed apps", () => {
  const html = (v: InfraView) => renderToStaticMarkup(<InfraScreen view={v} />);
  const target = (over: Partial<V2Target> = {}): V2Target => ({
    id: "deptgt_1", app_name: "Invoice-Chaser", repo: "boss-sandbox", environment: "staging",
    kind: "ssh", ref: "root@192.0.2.202:22", status: "active", retired: false,
    domains: ["192.0.2.202:3013"], url: "http://192.0.2.202:3013", host: "hetzner-ci-01",
    last_deploy: { status: "succeeded", commit: "19d5806", finished_at: null,
                   failure_reason: null },
    ...over,
  });

  it("links to the running app", () => {
    const out = html(infra({ targets: [target()] }));
    expect(out).toContain('href="http://192.0.2.202:3013"');
    expect(out).toContain("open app");
  });

  it("names the machine rather than making the reader parse an ssh string", () => {
    expect(html(infra({ targets: [target()] }))).toContain("on hetzner-ci-01");
  });

  it("says so rather than guessing when the host is not registered", () => {
    // An app filed under the wrong machine is worse than one filed under none.
    const out = html(infra({ targets: [target({ host: null })] }));
    expect(out).toContain("unregistered host");
  });

  it("shows how the last deploy went, and why it failed", () => {
    const out = html(infra({ targets: [target({
      last_deploy: { status: "failed", commit: null, finished_at: null,
                     failure_reason: "port 3014 already bound" } })] }));
    expect(out).toContain("last deploy failed");
    expect(out).toContain("port 3014 already bound");
  });

  it("distinguishes never-deployed from deployed-and-failed", () => {
    expect(html(infra({ targets: [target({ last_deploy: null })] })))
      .toContain("never deployed");
  });

  it("offers no link when there is no address", () => {
    const out = html(infra({ targets: [target({ url: null, domains: [] })] }));
    expect(out).toContain("no address recorded");
    expect(out).not.toContain('data-testid="v2-target-url-deptgt_1"');
  });

  it("marks a retired target without colouring it as a failure", () => {
    const out = html(infra({ targets: [target({ retired: true, status: "retired" })] }));
    expect(out).toContain('data-retired="true"');
    expect(out).toContain("retired");
  });
});
