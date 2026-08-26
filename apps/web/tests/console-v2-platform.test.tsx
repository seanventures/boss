import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { InfraScreen } from "../components/v2/InfraScreen";
import { SettingsScreen } from "../components/v2/SettingsScreen";
import { WorkflowsScreen } from "../components/v2/WorkflowsScreen";
import type { InfraView, SettingsView, WorkflowsView } from "../lib/v2/platform";

/**
 * Workflows, Infra & Models, Settings.
 *
 * The reference promises more than this deployment has: workflow versions, fleet load, SSO,
 * human seats, listable API keys. Every one of those is reported as absent WITH a reason.
 * A settings page full of controls that cannot act teaches the operator the console is
 * decorative — which then applies to the controls that DO work.
 */

const wf = (over: Partial<WorkflowsView> = {}): WorkflowsView => ({
  project_id: "boss-dogfood",
  workflows: [{
    id: "tpl_1", key: "prd_to_ship", name: "PRD to ship",
    description: "Panel, build, review, ship.", status: "active", created_by: "ceo",
    runs_30d: 4, running_now: 1, last_run_at: "2026-08-26T00:00:00Z",
    stages: [
      { key: "intake", action: "accept_prd", role: "cpo", gated: false },
      { key: "ship", action: "deploy", role: "devops", gated: true },
    ],
  }],
  versioned: false,
  version_note: "BOSS keeps one active spec per process. There are no versions to deploy.",
  apiReachable: true,
  ...over,
});

const infra = (over: Partial<InfraView> = {}): InfraView => ({
  project_id: "boss-dogfood",
  hosts: [{ id: "h1", name: "goldeneye", address: "10.0.0.1", arch: "arm64", status: "verified", verified: true }],
  targets: [],
  models: [{ model: "claude-opus-5", calls: 12, tokens: 90000, spend_usd: 4.5, estimated: false }],
  integrations: [],
  load_recorded: false,
  load_note: "BOSS records host verification, not utilisation. There is no load to show.",
  apiReachable: true,
  ...over,
});

const settings = (over: Partial<SettingsView> = {}): SettingsView => ({
  project_id: "boss-dogfood",
  roles: [{ key: "cto", name: "CTO", autonomy_level: 2, trust_score: 60 }],
  repos: [{ name: "paramg/boss-sandbox", runs: 40, prs: 12, last_run_at: "2026-08-20T00:00:00Z" }],
  unavailable: [
    { id: "sso", label: "SSO & identity", reason: "This deployment authenticates with an API key." },
    { id: "members", label: "Members & seats", reason: "BOSS models AGENT roles, not human seats." },
  ],
  apiReachable: true,
  ...over,
});

describe("Workflows", () => {
  const html = (v: WorkflowsView) => renderToStaticMarkup(<WorkflowsScreen view={v} />);

  it("marks the stages that stop for a human", () => {
    // That is the thing an operator needs from this screen: where the process will wait.
    const out = html(wf());
    expect(out).toContain('data-testid="v2-stage-gated-prd_to_ship-ship"');
    expect(out).toMatch(/waits for you/);
    expect(out).toMatch(/1 stage stops for you/);
  });

  it("says workflows are not versioned instead of drawing a version control", () => {
    // The reference offers "Deploy version". Here that button would do nothing.
    const out = html(wf());
    expect(out).toContain('data-testid="v2-workflows-versioning"');
    expect(out).toMatch(/no versions/i);
    expect(out).not.toMatch(/Deploy version/);
  });

  it("a process with no stages says so rather than rendering an empty list", () => {
    const out = html(wf({ workflows: [{ ...wf().workflows[0], stages: [] }] }));
    expect(out).toContain('data-testid="v2-workflow-nostages-prd_to_ship"');
  });

  it("a process that has never run says so", () => {
    // "0 runs" and "never run" read differently; the second is a fact about the process.
    const out = html(wf({
      workflows: [{ ...wf().workflows[0], runs_30d: 0, running_now: 0, last_run_at: null }],
    }));
    expect(out).toContain('data-testid="v2-workflow-neverrun-prd_to_ship"');
  });

  it("an unreachable API is not an empty company", () => {
    expect(html(wf({ apiReachable: false, workflows: [] })))
      .toContain('data-testid="v2-workflows-unreachable"');
  });
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

describe("Settings", () => {
  const html = (v: SettingsView) => renderToStaticMarkup(<SettingsScreen view={v} />);

  it("names what this deployment does not have, with reasons", () => {
    // An empty SSO form implies SSO is configurable here and merely unconfigured.
    const out = html(settings());
    expect(out).toContain('data-testid="v2-unavailable-sso"');
    expect(out).toContain('data-testid="v2-unavailable-members"');
    expect(out).toMatch(/authenticates with an API key/);
  });

  it("renders no form for anything it cannot configure", () => {
    // A settings page full of controls that cannot act teaches the operator the console is
    // decorative — which then applies to the controls that do work.
    const out = html(settings());
    expect(out).not.toMatch(/<input/);
    expect(out).not.toMatch(/<form/);
  });

  it("calls them agent roles, not members", () => {
    const out = html(settings());
    expect(out).toMatch(/Agent roles/);
    expect(out).toContain('data-testid="v2-role-cto"');
  });

  it("repos come from what work actually targeted", () => {
    const out = html(settings());
    expect(out).toContain("paramg/boss-sandbox");
    expect(out).toMatch(/12<\/b> PRs opened/);
  });

  it("an unreachable API does not imply an unconfigured company", () => {
    expect(html(settings({ apiReachable: false, roles: [], repos: [], unavailable: [] })))
      .toContain('data-testid="v2-settings-unreachable"');
  });
});
