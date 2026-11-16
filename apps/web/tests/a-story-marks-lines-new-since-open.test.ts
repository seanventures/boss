/**
 * Lines newer than the last time this build was opened.
 *
 * No stamp: nothing is new, and the newest line time is what gets stored.
 * A later open marks only lines strictly after that stamp. One sentence names
 * the group. The stamp key includes the run id.
 */
import { describe, expect, it } from "vitest";

import { sinceOpen, storyOpenedKey } from "../lib/story-opened";

const EARLY = "2026-10-04T09:12:00.000Z";
const MID = "2026-10-04T09:18:00.000Z";
const LATER = "2026-10-04T09:30:00.000Z";

const lines = [
  { id: "boss", at: EARLY },
  { id: "plan", at: EARLY },
  { id: "build", at: MID },
  { id: "blank", at: null },
];

function memory(): Pick<Storage, "getItem" | "setItem"> {
  const box = new Map<string, string>();
  return {
    getItem: (key) => box.get(key) ?? null,
    setItem: (key, value) => { box.set(key, value); },
  };
}

describe("story lines newer than the last open", () => {
  it("marks nothing the first time and stores the newest line time", () => {
    const storage = memory();
    const key = storyOpenedKey("crun_inv2");
    expect(key).toContain("crun_inv2");
    expect(storyOpenedKey("crun_other")).not.toBe(key);
    expect(storage.getItem(key)).toBeNull();

    const first = sinceOpen(lines, storage.getItem(key));
    expect(first.ids).toEqual([]);
    expect(first.count).toBe(0);
    expect(first.note).toBeNull();
    expect(first.stamp).toBe(MID);
    if (first.stamp) storage.setItem(key, first.stamp);

    const again = sinceOpen(lines, storage.getItem(key));
    expect(again.ids).toEqual([]);
    expect(again.note).toBeNull();
    expect(again.stamp).toBe(MID);
    expect(storage.getItem(storyOpenedKey("crun_other"))).toBeNull();
  });

  it("marks lines after the stored stamp with one sentence for the group", () => {
    const storage = memory();
    const key = storyOpenedKey("crun_inv2");
    storage.setItem(key, MID);
    const opened = [
      ...lines,
      { id: "same", at: MID },
      { id: "review", at: LATER },
      { id: "ship", at: "2026-10-04T09:40:00.000Z" },
      { id: "not-a-time", at: "not-a-time" },
    ];
    const mark = sinceOpen(opened, storage.getItem(key));
    expect(mark.ids).toEqual(["review", "ship"]);
    expect(mark.count).toBe(2);
    expect(mark.note).toBe("New since you opened this build.");
    expect(mark.note?.split(/\s+/).length).toBe(6);
    expect(JSON.stringify(mark).split("New since you opened this build.").length - 1).toBe(1);
    expect(mark.stamp).toBe("2026-10-04T09:40:00.000Z");

    if (mark.stamp) storage.setItem(key, mark.stamp);
    const next = sinceOpen(opened, storage.getItem(key));
    expect(next.ids).toEqual([]);
    expect(next.count).toBe(0);
    expect(next.note).toBeNull();
  });

  it("keeps a stamp that is already newer than every line", () => {
    const mark = sinceOpen(lines, LATER);
    expect(mark.ids).toEqual([]);
    expect(mark.note).toBeNull();
    expect(mark.stamp).toBe(LATER);
  });

  it("treats a stamp that is not a time as a first open", () => {
    const mark = sinceOpen(lines, "yesterday");
    expect(mark.ids).toEqual([]);
    expect(mark.note).toBeNull();
    expect(mark.stamp).toBe(MID);
  });
});
