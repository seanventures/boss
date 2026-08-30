import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentChat } from "../app/agent-ui";
import { codeOnly } from "./_helpers/source";

/**
 * The suggestions stay on screen once the conversation starts.
 *
 * They were rendered inside the empty state — the `messages.length === 0` branch — so they
 * existed only until you said anything. That is backwards. On the first turn you do not yet
 * know what to ask; by the third you have read an answer and want the obvious follow-up, and
 * a follow-up is precisely what a grounded suggestion is good at. "The recommendations are not
 * there once you chat… it is coming only for first time."
 *
 * They now live in one strip above the composer, rendered for every state.
 */

// app/agent-ui.tsx imports named hooks but no React default, and this suite compiles JSX with
// the classic runtime, which needs `React` in scope at call time.
(globalThis as Record<string, unknown>).React = React;

const html = renderToStaticMarkup(
  <AgentChat projectId="boss-dogfood" actor="ceo" initialTaskId="crun_x" />,
);

const SRC = codeOnly("app/agent-ui.tsx");

describe("the suggestion strip", () => {
  it("renders", () => {
    expect(html).toContain("agent-suggests-bar");
    expect((html.match(/class="agent-suggest"/g) ?? []).length).toBeGreaterThan(0);
  });

  it("is NOT inside the empty state, so a sent message cannot take it away", () => {
    // The regression, stated exactly: the chips used to live between `messages.length === 0 ?`
    // and the `:` that begins the transcript branch.
    const from = SRC.indexOf("messages.length === 0 ?");
    const to = SRC.indexOf(") : messages.map(", from);
    expect(from).toBeGreaterThan(-1);
    expect(to).toBeGreaterThan(from);
    expect(SRC.slice(from, to)).not.toContain("agent-suggest");
  });

  it("is guarded only by having suggestions — never by the transcript being empty", () => {
    const guard = SRC.slice(SRC.indexOf("{suggestions.length > 0 ?") - 40,
                            SRC.indexOf("agent-suggests-bar"));
    expect(guard).toContain("suggestions.length > 0");
    expect(guard).not.toContain("messages.length");
  });

  it("sits at the BOTTOM: after the transcript, above the composer", () => {
    // "please display in the bottom". Position is the requirement, not mere presence — a strip
    // rendered above the log would scroll away and be no better than what it replaced.
    const log = html.indexOf("agent-chat-log");
    const strip = html.indexOf("agent-suggests-bar");
    const composer = html.indexOf("agent-input");
    expect(log).toBeGreaterThan(-1);
    expect(composer).toBeGreaterThan(-1);
    expect(strip).toBeGreaterThan(log);
    expect(strip).toBeLessThan(composer);
  });

  it("shows them exactly once on the first turn", () => {
    // Leaving the empty-state copy in place while adding the strip would double every chip
    // before the first message and single them after — worse than either.
    const ids = html.match(/data-testid="agent-suggest-[^"]*"/g) ?? [];
    expect(ids.length).toBe(new Set(ids).size);
  });

  it("holds them still while a turn is in flight", () => {
    // Clicking one mid-turn only fills the composer, but a live-looking control during a turn
    // that cannot be sent reads as a dropped click.
    const strip = SRC.slice(SRC.indexOf("agent-suggests-bar"), SRC.indexOf("A TEXTAREA"));
    expect(strip).toContain("disabled={busy}");
  });

  it("keeps one row rather than growing upward into the conversation", () => {
    // Four long chips wrapped onto four lines would push the composer down every time a new
    // suggestion arrived, moving the input out from under the cursor.
    const css = codeOnly("app/globals.css");
    const rule = css.slice(css.indexOf(".agent-suggests-bar {"),
                           css.indexOf(".agent-suggests-bar::-webkit-scrollbar"));
    expect(rule).toContain("flex-wrap: nowrap");
    expect(rule).toContain("overflow-x: auto");
  });
});
