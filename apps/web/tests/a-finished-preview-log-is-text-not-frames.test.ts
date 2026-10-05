/**
 * A finished preview's log is its text, never raw SSE frames.
 *
 * The drawer read an ended preview's log from the streaming route, which answers
 * `text/event-stream`, and printed the frames verbatim: `event: log`, `data: "…\n"` with the JSON
 * escapes, heartbeats and the `end` frame. It reads the snapshot now (`?stream=false`), and any
 * SSE body that still arrives is decoded to the text its `log` frames carry.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { logText, sseText } from "../app/infra/preview-log";

const FRAMES = [
  'event: log\ndata: "> next start -p 4127\\n"\n\n',
  ": heartbeat\n\n",
  'event: log\ndata: "ready - started server on 0.0.0.0:4127\\n"\n\n',
  'event: end\ndata: "stopped"\n\n',
].join("");

describe("a finished preview's log", () => {
  it("decodes SSE frames to their text, dropping heartbeats and the end frame", () => {
    expect(sseText(FRAMES)).toBe("> next start -p 4127\nready - started server on 0.0.0.0:4127\n");
    const shown = logText(FRAMES);
    expect(shown).not.toMatch(/event:|data:|heartbeat|\\n|"/);
    expect(shown).toContain("ready - started server");
  });

  it("leaves a plain log and the snapshot shapes alone", () => {
    expect(logText("plain line\nsecond")).toBe("plain line\nsecond");
    expect(logText({ log: "from the snapshot" })).toBe("from the snapshot");
    expect(logText({ lines: ["a", "b"] })).toBe("a\nb");
  });

  it("reads the snapshot, not the streaming route, once the preview has ended", () => {
    const src = readFileSync(join(process.cwd(), "app", "infra", "preview-log.tsx"), "utf8");
    expect(src).toContain("/logs?stream=false");
  });
});
