import { describe, expect, it } from "vitest";

import { plainSentence } from "../lib/templates";
import { importUrlProblem } from "../components/v2/templates/kit-templates";
import { groupFindings, type Finding } from "../lib/security-review";
import { codeOnly } from "./_helpers/source";

/** Tester round 2: N8, N9, N10 and finding grouping, in the console. */

describe("N9: a key is never capitalised", () => {
  it("keeps a message that starts with a key as written", () => {
    expect(plainSentence("acme-greet-gh 1.0.0 is already waiting for review")).toBe(
      "acme-greet-gh 1.0.0 is already waiting for review");
    expect(plainSentence("stripe-checkout needs a test key")).toBe("stripe-checkout needs a test key");
    expect(plainSentence("hello 1.0.0 is already pending_review")).toBe("hello 1.0.0 is already waiting for review");
  });
  it("still capitalises a sentence that starts with a word", () => {
    expect(plainSentence("that build is not finished (D3)")).toBe("That build is not finished");
  });
});

describe("N10: Import says why it is off", () => {
  it("an http URL is told to use https", () => {
    expect(importUrlProblem("http://github.com/acme/tpl")).toMatch(/^Use an https:\/\/ URL/);
    expect(importUrlProblem("git@github.com:acme/tpl.git")).toMatch(/^Use an https:\/\/ URL/);
    expect(importUrlProblem("")).toMatch(/https:\/\//);
    expect(importUrlProblem("https://github.com/acme/tpl")).toBe("");
  });
  it("the reason is the button's tooltip and the footer's words", () => {
    const src = codeOnly("components/v2/templates/kit-templates.tsx");
    expect(src).toContain('title={urlWhy || "Fetch and check it. Nothing in it runs."}');
    expect(src).toContain("{urlWhy}</span>");
  });
});

describe("N8: a test key is written once", () => {
  it("one PUT names both envs, in the apply sheet and in Setup", () => {
    const sheet = codeOnly("components/v2/templates/template-sheet.tsx");
    expect(sheet).toContain('envs: ["build", "preview"], mode: "test"');
    expect(sheet).not.toMatch(/for \(const env of \["build", "preview"\]\)/);
    const setup = codeOnly("components/v2/templates/setup-sheet.tsx");
    expect(setup).toContain("{ envs, mode: editing.kind, value: value.trim() }");
    expect(setup).not.toMatch(/for \(const env of envs\)/);
  });
});

describe("one problem, one row", () => {
  const f = (id: string, groupKey: string): Finding => ({
    id, groupKey, fingerprint: id, tool: "zap", ruleId: "10036", title: "Server leaks version", severity: "low",
    confidence: "", cwe: "", owasp: "", location: { method: "GET", path: `/${id}` }, evidence: {}, status: "open",
    triagedBy: "", triageReason: "", firstSeenScanId: "",
  });
  it("findings sharing the server's group key are one row", () => {
    const g = groupFindings([f("a", "grp_1"), f("b", "grp_1"), f("c", "grp_1"), f("d", "grp_2")]);
    expect(g.map((x) => x.members.length)).toEqual([3, 1]);
  });
  it("a finding with no group key is its own row", () => {
    expect(groupFindings([f("a", ""), f("b", "")]).length).toBe(2);
  });
  it("the Findings list renders groups and triages the whole group", () => {
    const src = codeOnly("app/factory/security/app-security.tsx");
    expect(src).toContain("groupFindings(visible).map((g) =>");
    expect(src).toContain("securityApi.triage(f.id, status, reason.trim(), many)");
  });
});
