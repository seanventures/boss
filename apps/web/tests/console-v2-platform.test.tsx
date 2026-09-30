import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { InfraScreen } from "../components/v2/InfraScreen";
import { SettingsScreen } from "../components/v2/SettingsScreen";
import { WorkflowsScreen } from "../components/v2/WorkflowsScreen";
import type { InfraView, V2Target, SettingsView, WorkflowsView } from "../lib/v2/platform";

/**
 * Workflows, Infra & Models, Settings.
 *
 * The reference promises more than this deployment has: workflow versions, fleet load, SSO,
 * human seats, listable API keys. Every one of those is reported as absent WITH a reason.
 * A settings page full of controls that cannot act teaches the operator the console is
 * decorative — which then applies to the controls that DO work.
 */

const stage = (over: Partial<import("../lib/v2/platform").V2Stage> = {}) => ({
  key: "intake", action: "accept_prd", role: "product_manager",
  kind: "SEQUENTIAL" as const, next: [], waits_for: [], unreachable: false, gated: null,
  ...over,
});

const wf = (over: Partial<WorkflowsView> = {}): WorkflowsView => ({
  project_id: "boss-dogfood",
  workflows: [{
    id: "tpl_1", key: "prd_to_ship", name: "PRD to ship",
    description: "Panel, build, review, ship.", status: "active", created_by: "ceo",
    runs_30d: 4, running_now: 1, last_run_at: "2026-08-26T00:00:00Z",
    stages: [
      stage({ key: "intake", action: "accept_prd", kind: "PARALLEL",
              next: ["rev_a", "rev_b", "rev_c"] }),
      stage({ key: "improve", action: "improve_prd", kind: "JOIN",
              waits_for: ["rev_a", "rev_b", "rev_c"], next: ["certify"] }),
      stage({ key: "certify", action: "certify_spec", role: "chief_architect", kind: "PANEL" }),
    ],
    panels: 1, parallel_points: 1,
    roles: ["chief_architect", "product_manager"],
  }],
  versioned: false,
  version_note: "BOSS keeps one active spec per process. There are no versions to deploy.",
  gates_recorded: false,
  gates_note: "These specs do not record which stages stop for a human.",
  editable: false,
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
  repos: [{ name: "example-user/boss-sandbox", runs: 40, prs: 12, last_run_at: "2026-08-20T00:00:00Z" }],
  unavailable: [
    { id: "sso", label: "SSO & identity", reason: "This deployment authenticates with an API key." },
    { id: "members", label: "Members & seats", reason: "BOSS models AGENT roles, not human seats." },
  ],
  apiReachable: true,
  ...over,
});

describe("Workflows", () => {
  const html = (v: WorkflowsView) => renderToStaticMarkup(<WorkflowsScreen view={v} />);

  it("puts the process list beside the selected process", () => {
    // A process is a thing you pick and then study. One column of `step_key role_name` was a
    // data dump, not a screen.
    const out = html(wf());
    expect(out).toContain('data-testid="v2-workflows-list"');
    expect(out).toContain('data-testid="v2-workflow-detail-prd_to_ship"');
  });

  it("numbers the stages", () => {
    const out = html(wf());
    expect(out).toMatch(/>01</);
    expect(out).toMatch(/>02</);
  });

  it("says what each stage DOES, derived from the spec's own graph", () => {
    // PARALLEL / JOIN / PANEL come from kind, next and wait_for. Reading the spec as a flat
    // list rendered eleven identical rows and lost the fact that three run at once.
    const out = html(wf());
    // The badge's TEXT, not the data-kind attribute. An unscoped match passes against
    // data-kind="PARALLEL" even when the badge renders empty — which it did, and which a
    // reader would see as an unlabelled stage.
    const badges = [...out.matchAll(/<span class="v2-wf-kind v2-mono"[^>]*>([^<]*)<\/span>/g)]
      .map((m) => m[1].trim())
      .filter(Boolean);
    expect(badges).toContain("PARALLEL");
    expect(badges).toContain("JOIN");
    expect(badges).toContain("PANEL");
  });

  it("describes the flow in words, not just a badge", () => {
    const out = html(wf());
    expect(out).toMatch(/starts 3 branches in parallel/);
    expect(out).toMatch(/waits for 3 branches/);
  });

  it("shows the owner of each stage as a chip", () => {
    const out = html(wf());
    expect(out).toContain('data-testid="v2-stage-owner-prd_to_ship-intake"');
    expect(out).toContain("product_manager");
  });

  it("marks a stage the entry point can never reach", () => {
    // A stage that cannot run is worth seeing, not hiding.
    const out = html(wf({
      workflows: [{ ...wf().workflows[0],
        stages: [stage({ key: "orphan", unreachable: true })] }],
    }));
    expect(out).toContain('data-testid="v2-stage-unreachable-prd_to_ship-orphan"');
  });

  it("draws no editing controls, because there is no editing API", () => {
    // The reference has "+ Add stage", "Deploy version" and an × per stage. Three buttons that
    // cannot act would teach the operator the console is decorative.
    const out = html(wf());
    expect(out).not.toMatch(/Add stage/);
    expect(out).not.toMatch(/Deploy version/);
    expect(out).toContain('data-testid="v2-workflows-versioning"');
  });

  it("says gating is unknown rather than claiming nothing stops for you", () => {
    // The live template has eleven steps and records gating on none of them.
    const out = html(wf());
    expect(out).toMatch(/do not record which stages stop for a human/);
    expect(out).not.toMatch(/HUMAN GATE/);
  });

  it("shows a human gate when the spec does record one", () => {
    const out = html(wf({
      gates_recorded: true,
      gates_note: "",
      workflows: [{ ...wf().workflows[0], stages: [stage({ key: "signoff", gated: true })] }],
    }));
    expect(out).toContain('data-testid="v2-stage-gated-prd_to_ship-signoff"');
    expect(out).toMatch(/HUMAN GATE/);
  });

  it("summarises each process with counted numbers", () => {
    const out = html(wf());
    expect(out).toMatch(/3 stages/);
    expect(out).toMatch(/1 panel/);
    expect(out).toMatch(/4 runs \/ 30d/);
  });

  it("a process that has never run says so", () => {
    const out = html(wf({
      workflows: [{ ...wf().workflows[0], runs_30d: 0, running_now: 0, last_run_at: null }],
    }));
    expect(out).toMatch(/never run/);
  });

  it("a process with no stages says so rather than rendering an empty list", () => {
    const out = html(wf({ workflows: [{ ...wf().workflows[0], stages: [] }] }));
    expect(out).toContain('data-testid="v2-workflow-nostages-prd_to_ship"');
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
    expect(out).toContain("example-user/boss-sandbox");
    expect(out).toMatch(/12<\/b> PRs opened/);
  });

  it("an unreachable API does not imply an unconfigured company", () => {
    expect(html(settings({ apiReachable: false, roles: [], repos: [], unavailable: [] })))
      .toContain('data-testid="v2-settings-unreachable"');
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
