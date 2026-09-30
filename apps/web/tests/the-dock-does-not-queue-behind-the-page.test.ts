import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { codeOnly } from "./_helpers/source";

/**
 * "Loading conversation… is stuck when I click Ask for Engineering Captain."
 *
 * It was not stuck. It was QUEUED.
 *
 * Every read the dock performs on open — the thread list, the conversation, the role's own
 * starters — lived in `app/actions.ts`, which is `"use server"`. Next serialises Server Actions
 * against each other AND against `router.refresh()`. The console refreshes itself on a live
 * interval, that refresh re-renders a 6 MB page taking eight to twenty seconds, and a dock
 * opened during that window sat behind it holding "Loading conversation…".
 *
 * The same queue is why the screenshot still showed the generic starter chips — "What changed
 * today?", "Which runs failed and why?" — under a panel titled "Ask Engineering Captain". The
 * role's real starters were waiting in it too, so the evidence for the diagnosis was on screen
 * next to the symptom.
 *
 * The delivery-workflow screen already had a route handler for exactly this reason, written
 * when a Server Action reply was re-rendering the whole console route. The dock needed the same
 * and did not have it.
 */
const UI = codeOnly("app/agent-ui.tsx");
const ROUTE = readFileSync(join(process.cwd(), "app/api/agent/route.ts"), "utf8");

describe("the dock reads through a route handler", () => {
  it("it does not call the Server Action layer to open", () => {
    // These are the four that blocked the panel. A Server Action for a READ on this screen is
    // the bug, not a style preference.
    for (const action of ["loadAgentWorkspaceAction", "loadThreadMessagesAction",
                          "loadRoleStartersAction", "loadAgentProgressAction",
                          "loadTaskContextAction"]) {
      expect(UI, `${action} queues behind router.refresh()`).not.toContain(action);
    }
  });

  it("every read it needs is served", () => {
    for (const view of ["workspace", "messages", "starters", "progress", "task-context"]) {
      expect(ROUTE, `no '${view}' view`).toContain(`view === "${view}"`);
      expect(UI, `nothing asks for '${view}'`).toContain(`view: "${view}"`);
    }
  });

  it("the handler is dynamic, so nothing is cached into staleness", () => {
    expect(ROUTE).toContain('export const dynamic = "force-dynamic"');
    expect(UI).toContain('cache: "no-store"');
  });

  it("sending a message still goes through the action layer", () => {
    // The write belongs there. This change is about reads that must not wait.
    expect(UI).toContain("askBossAction");
    expect(UI).toContain("runAgentActionAction");
  });
});

describe("a read that never answers cannot hold the panel", () => {
  it("every read is bounded", () => {
    // The second half of the fix. Moving off the queue makes the common case fast; the timeout
    // is what stops the uncommon one from being a spinner with no end.
    expect(UI).toContain("new AbortController()");
    expect(UI).toContain("controller.abort()");
    expect(UI).toMatch(/ms = 12_000/);
  });

  it("the progress poll gets a shorter bound than the panel's own reads", () => {
    // It fires every 1.5s while a turn runs; a 12s hang there would stack requests.
    expect(UI).toContain("4_000");
  });

  it("a failed read opens the panel empty rather than leaving it loading", () => {
    const mount = UI.slice(UI.indexOf('view: "workspace", project_id: projectId, actor'));
    expect(mount).toContain("if (!d) { setLoadingThread(false); return; }");
  });

  it("and an aborted read is not an error the operator has to see", () => {
    const helper = UI.slice(UI.indexOf("async function read<T>"));
    expect(helper.slice(0, 900)).toContain("return null;");
  });
});

describe("the handler fails softly where the panel must still open", () => {
  it("a workspace half that does not answer does not sink the other", () => {
    // Holding "Loading conversation…" because a task list did not answer is the same bug in
    // another costume.
    const workspace = ROUTE.slice(ROUTE.indexOf('view === "workspace"'));
    expect(workspace.slice(0, 700)).toContain("catch(() => ({}))");
  });

  it("it refuses a request with no project rather than guessing one", () => {
    expect(ROUTE).toContain('{ error: "missing project_id" }');
  });

  it("an unknown view says what it knows", () => {
    expect(ROUTE).toContain("unknown view");
  });
});
