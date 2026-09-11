import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * "Some agent window has clear chat while others don't … we have added BOSS agent in multiple
 * screens, we have to ensure it behaves consistently."
 *
 * One component serves every surface — the dock, the BOSS Agent area, a role chat, a chat opened
 * on a run — but it rendered two different headers, and "Clear this chat" lived inside the one
 * for a GENERAL conversation. So the dock's most common state, opened from a card or a workflow
 * step, had no way to start over, while the general one did. `clearChat` was never the
 * limitation: it takes a thread_id and has always worked for any of them.
 */
const DOCK = codeOnly("app/agent-ui.tsx");

describe("clear is on every conversation, not just the general one", () => {
  it("the control sits outside the branch that chose between two headers", () => {
    const at = DOCK.indexOf('data-testid="agent-clear"');
    expect(at).toBeGreaterThan(-1);
    // After both context blocks have closed: the general intro and the task block.
    expect(at).toBeGreaterThan(DOCK.indexOf("agent-chat-intro"));
    expect(at).toBeGreaterThan(DOCK.indexOf("No projects yet for this task"));
    // And before the log, so it reads as a control on the conversation.
    expect(at).toBeLessThan(DOCK.indexOf("agent-chat-log"));
  });

  it("and it is offered exactly once", () => {
    expect(DOCK.split('data-testid="agent-clear"').length - 1).toBe(1);
  });

  it("on the same condition everywhere: a thread with something in it", () => {
    const at = DOCK.indexOf("{threadId && messages.length ? (");
    expect(at).toBeGreaterThan(-1);
    expect(DOCK.slice(at, at + 200)).toContain("agent-clear");
  });
});

describe("what you send it", () => {
  it("a screenshot or a text file can be attached", () => {
    expect(DOCK).toContain('data-testid="agent-attach"');
    expect(DOCK).toContain('data-testid="agent-file-input"');
  });

  it("and pasted, which is how a screenshot actually arrives", () => {
    // It is already on the clipboard when the operator decides to report the bug.
    expect(DOCK).toContain("onPaste={(e) => {");
    expect(DOCK).toContain("e.clipboardData?.files");
  });

  it("images are read as base64 without blowing the call stack", () => {
    // String.fromCharCode(...megabyte) overflows — on exactly the large screenshot most worth
    // sending.
    expect(DOCK).toContain("bytes.subarray(i, i + 8192)");
  });

  it("the bound is said before the upload, not after it", () => {
    expect(DOCK).toContain("const MAX_FILES = 5");
    expect(DOCK).toContain("the limit is");
  });

  it("a file with no words is still sendable", () => {
    expect(DOCK).toContain("disabled={busy || (!input.trim() && !files.length)}");
  });

  it("and the transcript shows what was sent", () => {
    expect(DOCK).toContain('data-testid="agent-message-attached"');
  });

  it("the files are cleared once the turn carries them", () => {
    const at = DOCK.indexOf("const send = useCallback");
    const block = DOCK.slice(at, at + 700);
    expect(block).toContain('fd.set("attachments", JSON.stringify(files))');
    expect(block).toContain("setFiles([])");
  });
});
