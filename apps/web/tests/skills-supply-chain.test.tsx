// @vitest-environment happy-dom
/**
 * The skill drawer's History and Tests tabs, and Import › From a source (BOSSAPP-841..844).
 * Clicked through: the tabs switch, Restore/Publish/Run/Grade send the actions the proxy allows,
 * and Import previews before it imports the commit it showed.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, type } from "./_helpers/kit-render";
import { clone, installKitApi, KITS, SERVERS } from "./_helpers/kit-api";

const { SkillDrawer } = await import("../app/skills/skill-drawer");
const { ImportSheet } = await import("../app/skills/import-sheet");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const crew = clone(KITS.fresh).crew ?? [];
const SKILL = {
  skill_key: "house_tone", label: "House tone", summary: "How we write", behaviour_kind: "guide",
  knowledge: "# Tone", inputs: [], produces: [], config: {}, risk_tier: 0, trusted: false, enabled: true,
  references: { custom_steps: [], agents: [], workflows: [] },
};
const VERSIONS = {
  versions: [
    { id: "skv_2", version_no: 2, author_kind: "agent", author_id: "agent:writer", source_note: "approved agent change",
      created_at: "2026-11-12T10:00:00Z", current: true, restored_from: null },
    { id: "skv_1", version_no: 1, author_kind: "person", author_id: "ceo", source_note: "",
      created_at: "2026-11-11T10:00:00Z", current: false, restored_from: null },
  ],
  holders: [{ role_key: "editor", pinned_version_no: 1 }],
};
const TESTS = {
  version_no: 2, previous_no: 1, gate: { allowed: false, reason: "this version failed its latest test run (greets)." },
  cases: [{ id: "stc_1", name: "greets", input: {},
            current: { id: "str_2", status: "ran", output: { text: "Guide: v2" }, grade: "fail", notes: "" },
            previous: { id: "str_1", status: "ran", output: { text: "Guide: v1" }, grade: "pass", notes: "" } }],
};

function supply(url: string, body: Record<string, unknown> | null, method: string) {
  if (!url.startsWith("/api/skill-supply")) return undefined;
  if (method === "GET") {
    if (url.includes("view=diff")) return { diff: "--- v1\n+++ v2\n+  Never use jargon." };
    if (url.includes("view=tests")) return TESTS;
    return VERSIONS;
  }
  if (body?.action === "preview_source") {
    return { sha: "a".repeat(40), files: ["scripts/check.sh"],
             skill: { skill_key: "release_notes", label: "Release notes", summary: "How we write them", knowledge: "Lead with the change." } };
  }
  if (body?.action === "import_source") return { skill: { label: "Release notes" }, source: {} };
  return {};
}

const drawer = () => (
  <SkillDrawer projectId="p1" actor="ceo" skill={SKILL as never} builtIn={null} creating={false} crew={crew} holders={[]}
               servers={SERVERS as never} builtIns={[]} ability={undefined} onClose={() => {}} onSaved={() => {}} />
);

describe("the skill drawer's History tab", () => {
  it("lists versions with who and why, shows a diff, and restores as a new version", async () => {
    const calls = installKitApi(undefined, supply);
    await render(drawer());
    expect($$('[data-testid^="kit-skill-tab-"]').map((b) => b.textContent)).toEqual(["Skill", "History", "Tests"]);
    await click('[data-testid="kit-skill-tab-history"]');
    expect($('[data-testid="skill-version-2"]')?.textContent).toContain("Agent (approved)");
    expect($('[data-testid="skill-history-pins"]')?.textContent).toContain("editor on v1");
    await click('[data-testid="skill-version-diff-2"]');
    expect($('[data-testid="skill-version-diff-text"]')?.textContent).toContain("Never use jargon");
    await click('[data-testid="skill-version-restore-1"]');
    const restore = calls.find((c) => c.method === "POST" && c.body?.action === "restore");
    expect(restore?.body).toMatchObject({ skill_key: "house_tone", version: 1, actor: "ceo" });
    // The current version has no Restore: restoring it would copy itself.
    expect($('[data-testid="skill-version-restore-2"]')).toBeNull();
  });
});

describe("the skill drawer's Tests tab", () => {
  it("shows this version's output beside the last one, the held gate, and runs and grades", async () => {
    const calls = installKitApi(undefined, supply);
    await render(drawer());
    await click('[data-testid="kit-skill-tab-tests"]');
    expect($('[data-testid="skill-tests-gate"]')?.textContent).toBe("Publish held");
    expect($('[data-testid="skill-case-now-greets"]')?.textContent).toContain("Guide: v2");
    expect($('[data-testid="skill-case-greets"]')?.textContent).toContain("Guide: v1");
    await click('[data-testid="skill-tests-run"]');
    await click('[data-testid="skill-case-pass-greets"]');
    expect(calls.some((c) => c.body?.action === "run_tests")).toBe(true);
    expect(calls.find((c) => c.body?.action === "grade")?.body).toMatchObject({ run_id: "str_2", grade: "pass" });
    await type('[data-testid="skill-case-name"]', "says hello");
    await click('[data-testid="skill-case-add"]');
    expect(calls.find((c) => c.body?.action === "add_case")?.body).toMatchObject({ name: "says hello", input: {} });
  });
});

describe("Import › From a source", () => {
  it("previews first, then imports pinned to the commit it showed", async () => {
    const calls = installKitApi(undefined, supply);
    await render(<ImportSheet projectId="p1" actor="ceo" onClose={() => {}} onImported={() => {}} />);
    await click('[data-testid="kit-import-mode-source"]');
    expect(($('[data-testid="kit-source-import-btn"]') as HTMLButtonElement).disabled).toBe(true);
    await type('[data-testid="kit-source-locator"]', "acme/playbooks/skills/release-notes");
    await click('[data-testid="kit-source-preview-btn"]');
    expect($('[data-testid="kit-source-preview"]')?.textContent).toContain("Release notes");
    await click('[data-testid="kit-source-import-btn"]');
    const made = calls.find((c) => c.body?.action === "import_source");
    expect(made?.body).toMatchObject({ kind: "github", pin: "a".repeat(40), skill_key: "release_notes" });
  });
});
