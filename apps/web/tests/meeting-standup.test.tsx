import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

describe("typed standup meeting UI", () => {
  it("convene form offers prd_panel and standup types", () => {
    const source = readFileSync(join(ROOT, "app/meeting-room-ui.tsx"), "utf8");
    expect(source).toContain('name="meeting_type"');
    expect(source).toContain('{ key: "prd_panel", title: "PRD panel"');
    expect(source).toContain('{ key: "go_no_go", title: "Go / no-go"');
    expect(source).toContain('{ key: "standup", title: "Stand-up"');
    expect(source).toContain("no-spend fixture");
    const actions = readFileSync(join(ROOT, "app/actions.ts"), "utf8");
    expect(actions).toContain("/v1/meetings/standup");
    expect(actions).toContain("/v1/meetings/go-no-go");
  });

  it("binds consensus tally to a real meeting_id instead of an orphan calculator", () => {
    const room = readFileSync(join(ROOT, "app/meeting-room-ui.tsx"), "utf8");
    expect(room).toContain("<MeetingConsensusCard");
    expect(room).toContain("meetingId={meeting.id}");
    const card = readFileSync(join(ROOT, "app/phase3-ui.tsx"), "utf8");
    expect(card).toContain("requires meeting_id");
    expect(card).toContain("meetingId?: string");
  });
});
