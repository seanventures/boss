import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { stripAppFacts } from "../app/agent-ui";

/**
 * The operator reported: "why my messages are disappearing".
 *
 * The console prepends a machine-addressed `[app context]` block so the tools know which run
 * the conversation is about, and strips it for display. The strip returned `""` for two real
 * inputs — a facts block with no message after it, and one followed by whitespace — and the
 * chat rendered an empty "You" bubble. From the operator's side their message was gone.
 *
 * A chat that loses what you typed cannot be trusted with anything you type next, so the rule
 * is absolute: a DISPLAY transform must never be able to erase its input.
 */

const FACTS = [
  "[app context] You are talking about ONE app in BOSS.",
  "run_id: crun_abc",
  "app: Shorty",
  "repo: paramg/boss-sandbox",
  "branch: boss/code/crun_abc",
  "status: completed (done)",
  "pull_request: https://github.com/x/y/pull/1",
  "Use your tools (inspect_app, diagnose_preview, diagnose_deployment) with run_id crun_abc when you need more.",
].join("\n");

describe("stripAppFacts", () => {
  it("hides the context block and keeps the message", () => {
    expect(stripAppFacts(`${FACTS}\n\nfix and retry the deployment`))
      .toBe("fix and retry the deployment");
  });

  it("keeps a message that itself contains blank lines", () => {
    expect(stripAppFacts(`${FACTS}\n\npara one\n\npara two`)).toBe("para one\n\npara two");
  });

  it("leaves a message with no context block untouched", () => {
    expect(stripAppFacts("fix and retry the deployment")).toBe("fix and retry the deployment");
  });

  // ---- the two inputs that produced an empty bubble ----
  it("never returns empty for a facts-only turn", () => {
    expect(stripAppFacts(FACTS)).not.toBe("");
  });

  it("never returns empty when only whitespace follows the block", () => {
    expect(stripAppFacts(`${FACTS}\n\n   `)).not.toBe("");
  });

  it("shows SOMETHING for every input that had content", () => {
    // The invariant, stated once: non-empty in, non-empty out.
    for (const input of [
      FACTS,
      `${FACTS}\n\n`,
      `${FACTS}\n\n \t `,
      `${FACTS}\n\nreal question`,
      "plain",
    ]) {
      expect(stripAppFacts(input).trim(), `erased: ${JSON.stringify(input.slice(0, 40))}`)
        .not.toBe("");
    }
  });

  it("handles null and undefined without throwing", () => {
    expect(stripAppFacts(undefined as unknown as string)).toBe("");
    expect(stripAppFacts(null as unknown as string)).toBe("");
  });
});

describe("the composer", () => {
  const src = readFileSync(resolve(__dirname, "../app/agent-ui.tsx"), "utf8");

  it("refuses a blank turn before it costs anything", () => {
    // An empty send produced an empty bubble AND a billed model call with nothing to answer.
    expect(src).toContain('const typed = String(fd.get("message") ?? "").trim();');
    expect(src).toContain("if (!typed) return;");
  });

  it("sends the operator's words and NOTHING else", () => {
    // The strongest form of "never loses your message": nothing is added to it.
    //
    // The console used to prepend ~20 lines of run context to every question. The operator
    // could see it — their sentence buried under a wall of text they did not write — and it
    // did not even work, because a summary is not a PRD: asked to read the PRD or the diff,
    // BOSS answered that they were "not present in the searchable data" while the PRD sat in
    // the database. Scope now travels as external_ref and the server puts it in the SYSTEM
    // prompt, which the operator never sees and the model always gets.
    expect(src).toContain('fd.set("message", typed);');
    expect(src).not.toContain("APP_FACTS_END}");   // no longer composed into the message
    expect(src).not.toMatch(/appFacts\s*\?/);      // and no branch that could bring it back
  });

  it("still hands the agent the run id, so scope is not lost with the block", () => {
    // Deleting the context block only works because something else carries the subject.
    expect(src).toContain('fd.set("external_ref", sel)');
  });
});


// --------------------------------------------------------------------------- //
// BOSS answers in markdown; the chat was the one place printing the source
// --------------------------------------------------------------------------- //
describe("chat formatting", () => {
  const UI = readFileSync(resolve(__dirname, "../app/agent-ui.tsx"), "utf8");
  const CSS = readFileSync(resolve(__dirname, "../app/globals.css"), "utf8");

  it("renders an assistant reply as markdown", () => {
    // "**What I found**", GFM evidence tables, `run_id` in backticks — printed raw, the table
    // that matters most became unreadable.
    expect(UI).toContain('import { Markdown } from "./markdown"');
    expect(UI).toContain('<Markdown className="agent-md"');
  });

  it("reuses the renderer the rest of the console already uses", () => {
    // Three other views import it. A second markdown implementation is two to keep correct.
    const md = readFileSync(resolve(__dirname, "../app/markdown.tsx"), "utf8");
    expect(md).toContain("md-table");
  });

  it("leaves the operator's own message as plain text", () => {
    // They typed characters, not markup; turning their underscores into italics rewrites what
    // they said.
    expect(UI).toContain('m.role === "assistant" ? (');
    expect(UI).toContain('whiteSpace: "pre-wrap"');
  });

  it("lets a wide table scroll instead of widening the dock", () => {
    // .md-table-wrap has overflow-x:auto, which does nothing unless the bubble may shrink.
    const bubble = CSS.slice(CSS.indexOf(".agent-message {"), CSS.indexOf(".agent-message.user"));
    expect(bubble).toContain("min-width: 0");
    expect(CSS).toContain(".agent-message > * { min-width: 0; }");
  });

  it("keeps long code lines inside the panel", () => {
    expect(CSS).toContain(".agent-md pre");
    const pre = CSS.slice(CSS.indexOf(".agent-md pre {"));
    expect(pre.slice(0, 320)).toContain("overflow-x: auto");
  });
});
