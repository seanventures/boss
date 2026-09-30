import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { codeOnly } from "./_helpers/source";

/**
 * "Introduce a clear all button that would clear only bounded to that agent's chat and not
 * other chats."
 *
 * The bounding is the whole requirement, so it is enforced by SHAPE rather than by care: every
 * layer takes one thread id and there is no way to express "every conversation". A wildcard
 * that does not exist cannot be reached by a bug.
 *
 * And it archives rather than deletes. The messages stay readable by id; what makes the panel
 * look clear is the server freeing the find-or-create key (`role:Engineering Captain`) so the
 * next message opens a fresh thread. A chat cleared by mistake is then a lookup, not a loss.
 */
const UI = codeOnly("app/agent-ui.tsx");
const ROUTE = readFileSync(join(process.cwd(), "app/api/agent/route.ts"), "utf8");
const SERVICE = readFileSync(
  join(process.cwd(), "../../engine/boss_engine/services/agent_service.py"), "utf8");

describe("it clears one conversation", () => {
  it("the client sends the thread it is showing, and nothing else", () => {
    const fn = UI.slice(UI.indexOf("const clearChat = useCallback"));
    expect(fn).toContain('action: "clear"');
    expect(fn).toContain("thread_id: threadId");
    // No project-wide or role-wide form exists to be reached by accident.
    expect(fn).not.toContain("clear_all");
    expect(fn).not.toContain("all: true");
  });

  it("it does nothing when there is no conversation to clear", () => {
    const fn = UI.slice(UI.indexOf("const clearChat = useCallback"));
    expect(fn).toContain("if (!threadId || clearing) return;");
  });

  it("the route handler takes a single id and refuses without one", () => {
    expect(ROUTE).toContain("clear needs project_id and thread_id");
    expect(ROUTE).toContain("/v1/agent/threads/${encodeURIComponent(thread)}/clear");
  });

  it("and knows only that one action", () => {
    expect(ROUTE).toContain("unknown action '${action}'. Known: clear");
  });
});

describe("it cannot reach another project or actor", () => {
  it("the service checks the project fence", () => {
    expect(SERVICE).toContain("that conversation belongs to another project");
  });

  it("and the actor, for a private conversation", () => {
    // Task threads are shared by design (external_ref set); a general chat is not.
    expect(SERVICE).toContain("that conversation belongs to another actor");
  });

  it("it locks the row it is about to change", () => {
    const fn = SERVICE.slice(SERVICE.indexOf("def clear_thread"));
    expect(fn.slice(0, 1500)).toContain("FOR UPDATE");
  });
});

describe("archived, not deleted", () => {
  it("no messages are removed", () => {
    // `DELETE FROM`, not `DELETE`: the docstring says "ARCHIVED, NOT DELETED", and a bare
    // grep matches the sentence promising the opposite of what it would be testing.
    const fn = SERVICE.slice(SERVICE.indexOf("def clear_thread"),
                             SERVICE.indexOf("def list_threads"));
    expect(fn).not.toContain("DELETE FROM");
    expect(fn).toContain("UPDATE agent_threads");
  });

  it("the thread is closed", () => {
    const fn = SERVICE.slice(SERVICE.indexOf("def clear_thread"),
                             SERVICE.indexOf("def list_threads"));
    expect(fn).toContain("status = 'closed'");
  });

  it("and its find-or-create key is freed, which is what makes the chat look clear", () => {
    // Closing alone would not do it: the external_ref lookup does not filter on status, so the
    // next message would find the closed thread and carry on in it.
    const fn = SERVICE.slice(SERVICE.indexOf("def clear_thread"),
                             SERVICE.indexOf("def list_threads"));
    expect(fn).toContain("'#cleared-'");
  });

  it("a general chat with no key stays keyless rather than being stamped", () => {
    const fn = SERVICE.slice(SERVICE.indexOf("def clear_thread"),
                             SERVICE.indexOf("def list_threads"));
    // CAST, not a bare parameter: psycopg 3 cannot infer a type from a NULL comparison and
    // fails with AmbiguousParameter in production, while psycopg2 passes locally.
    expect(fn).toContain("CASE WHEN CAST(:ref AS TEXT) IS NULL THEN NULL");
  });
});

describe("a cleared conversation leaves the list", () => {
  it("closed threads are not listed", () => {
    // Without this, clearing a GENERAL chat archived it correctly and left it in the rail —
    // press Clear, the conversation is still there, which reads as the button doing nothing.
    // A cleared ROLE chat disappears either way because its key is stamped, so the gap was
    // invisible on the surface it was built for.
    const fn = SERVICE.slice(SERVICE.indexOf("def list_threads"));
    expect(fn.slice(0, 2000)).toContain("t.status <> 'closed'");
  });
});

describe("the panel only clears if the server agreed", () => {
  it("local state is emptied after the response, not before", () => {
    // Clearing optimistically shows an empty conversation that comes back on the next load,
    // which reads as the button having failed silently.
    const fn = UI.slice(UI.indexOf("const clearChat = useCallback"));
    const ok = fn.indexOf("if (!r.ok) return;");
    expect(ok).toBeGreaterThan(-1);
    expect(fn.indexOf("setMessages([])")).toBeGreaterThan(ok);
  });

  it("the thread id is released so the next message starts fresh", () => {
    const fn = UI.slice(UI.indexOf("const clearChat = useCallback"));
    expect(fn).toContain("setThreadId(null)");
  });

  it("a failure leaves the conversation and the button usable", () => {
    const fn = UI.slice(UI.indexOf("const clearChat = useCallback"));
    expect(fn).toContain("finally");
    expect(fn).toContain("setClearing(false)");
  });

  it("the control is absent when there is nothing to clear", () => {
    expect(UI).toContain("{threadId && messages.length ? (");
  });
});

describe("the suggestion chips are readable", () => {
  it("the bar is never squashed by the column", () => {
    // It is a flex item of the chat column, so without this it defaults to flex-shrink: 1 and
    // the column takes space from it — the chips rendered as a sliver with the top of each one
    // visible above the composer and the text unreadable.
    const css = readFileSync(join(process.cwd(), "app/globals.css"), "utf8");
    const rule = css.slice(css.indexOf(".agent-suggests-bar {"));
    expect(rule.slice(0, rule.indexOf("}"))).toContain("flex: 0 0 auto");
  });

  it("as is the composer, for the same reason", () => {
    const css = readFileSync(join(process.cwd(), "app/globals.css"), "utf8");
    const rule = css.slice(css.indexOf(".agent-composer {"));
    expect(rule.slice(0, rule.indexOf("}"))).toContain("flex: 0 0 auto");
  });
});
