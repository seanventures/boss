// @vitest-environment happy-dom
/**
 * BOSSAPP-572: One rules book console page (Govern › More › Rules book).
 * Read-only, grouped by category, searchable, filterable by category, role and check type,
 * showing where each rule lives.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RulesBookTab } from "../app/govern/rules-book-tab";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SAMPLE_BOOK = {
  read_only: true,
  total: 3,
  categories: ["autonomy", "build", "security"],
  enforcement_kinds: ["setting", "gate", "policy", "prompt_only"],
  roles: ["coder", "qa", "reviewer"],
  counts_by_category: { autonomy: 1, build: 1, security: 1 },
  counts_by_enforcement: { setting: 1, gate: 1, policy: 1, prompt_only: 0 },
  rules: [
    {
      id: "autonomy.dev_act",
      title: "Agents act autonomously in development",
      text: "Outside production, agents execute development work without interrupting a person.",
      why: "Avoids unnecessary mid-build approvals.",
      category: "autonomy",
      roles: ["all"],
      enforcement: {
        kind: "policy",
        ref: "boss_engine.services.autonomy_policy:DEFAULT",
        where: "engine/boss_engine/services/autonomy_policy.py · DEFAULT",
      },
      source: "autonomy_policy",
      universal: true,
      priority: 5,
    },
    {
      id: "build.open_pr_when_checks_fail",
      title: "Open the pull request even when checks fail",
      text: "Open a draft PR labelled checks failing instead of discarding work.",
      why: "Preserves visible progress.",
      category: "build",
      roles: ["coder", "qa"],
      enforcement: {
        kind: "setting",
        ref: "open_pr_when_checks_fail",
        where: "engine/boss_engine/services/project_settings.py · KNOBS['open_pr_when_checks_fail']",
      },
      source: "project_setting",
      universal: false,
      priority: 12,
      effective_value: 1,
    },
    {
      id: "security.never_print_credential",
      title: "Never print any credential",
      text: "Every output passes redaction before storage or display.",
      why: "Prevents permanent credential leaks.",
      category: "security",
      roles: ["all"],
      enforcement: {
        kind: "gate",
        ref: "boss_engine.services.redaction:redact_secrets",
        where: "engine/boss_engine/services/redaction.py · redact_secrets()",
      },
      source: "operator_rule",
      universal: true,
      priority: 1,
    },
  ],
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function setInput(sel: string, val: string) {
  const el = $(sel) as HTMLInputElement | HTMLSelectElement;
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, val);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  await settle();
}

afterEach(() => {
  unmount();
  vi.unstubAllGlobals();
});

describe("Govern › Rules book (BOSSAPP-572)", () => {
  it("renders rules grouped by category with enforcement location and supports search + filters", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(SAMPLE_BOOK)));
    await mount(<RulesBookTab projectId="prj_1" />);

    expect($('[data-testid="gv-rules-book"]')).not.toBeNull();
    expect($('[data-testid="gv-rb-total"]')?.textContent).toBe("3 of 3");
    expect($('[data-testid="gv-rb-group-autonomy"]')).not.toBeNull();
    expect($('[data-testid="gv-rb-group-build"]')).not.toBeNull();
    expect($('[data-testid="gv-rb-group-security"]')).not.toBeNull();
    expect($('[data-testid="gv-rb-badge-build.open_pr_when_checks_fail"]')?.textContent).toContain("Project setting");
    expect($('[data-testid="gv-rb-rule-security.never_print_credential"]')?.textContent).toContain("redaction.py");

    // Filter by category
    await click($('[data-testid="gv-rb-cat-build"]'));
    expect($$('[data-testid^="gv-rb-rule-"]').length).toBe(1);
    expect($('[data-testid="gv-rb-rule-build.open_pr_when_checks_fail"]')).not.toBeNull();

    // Clear category and search
    await click($('[data-testid="gv-rb-cat-all"]'));
    await setInput('[data-testid="gv-rb-search"]', "credential");
    expect($$('[data-testid^="gv-rb-rule-"]').length).toBe(1);
    expect($('[data-testid="gv-rb-rule-security.never_print_credential"]')).not.toBeNull();

    // Clear search and filter by enforcement type
    await setInput('[data-testid="gv-rb-search"]', "");
    await setInput('[data-testid="gv-rb-enforcement"]', "setting");
    expect($$('[data-testid^="gv-rb-rule-"]').length).toBe(1);
    expect($('[data-testid="gv-rb-rule-build.open_pr_when_checks_fail"]')).not.toBeNull();
  });
});
