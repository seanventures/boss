// @vitest-environment happy-dom
/**
 * BOSSAPP-946, finding 9 (and the change set's held merge, finding 6).
 *
 *  * The inline review note on a change set: its author ("Boss") sat on top of the note's words,
 *    and the note's Markdown showed raw ("MINOR** — `ALLOW_DEMO_SEED`…"). The author keeps its
 *    own width (flex: none) and the note renders as Markdown.
 *  * Add project pre-filled a name already taken ("HelpDesk AI"): every name BOSS pre-fills is
 *    asked of the engine first (GET …/add-project/name), and "HelpDesk AI 2" is filled instead.
 *  * A change set whose merge CI still holds reads "Checks running…", disabled, with the reason.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { Decide, InlineNote, Review } from "../app/changes/[projectId]/[number]/change-set-view";
import { addProjectApi } from "../lib/add-project";
import { parseChangeSet } from "../lib/repo-links";
import { declarations } from "./_helpers/css-declarations";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

afterEach(() => unmount());

const NOTE = "**MINOR** — `DEMO_PASSWORD` is never read.";

describe("the change set's review note", () => {
  it("renders its Markdown beside its author, never raw asterisks", async () => {
    stubFetch(() => undefined);
    await mountAs(<div><InlineNote author="boss" body={NOTE} />
      <Review notes={[{ id: 1, kind: "review", event: "COMMENT", author: "boss", body: "**Round 1** — first review",
                        comments: [{ path: ".env.example", line: 2, body: NOTE }] }]} /></div>);
    const note = tid("change-set-inline-note")!;
    expect(note.querySelector(".cs-who")?.textContent).toBe("boss");
    expect(note.querySelector(".cs-inline-b strong")?.textContent).toBe("MINOR");
    expect(note.querySelector(".cs-inline-b code")?.textContent).toBe("DEMO_PASSWORD");
    expect(note.textContent).not.toContain("**");
    expect(tid("change-set-review")?.textContent ?? "").not.toContain("**");
  });

  it("the author keeps its own width, the note takes the rest", () => {
    const css = readFileSync(join(__dirname, "../app/changes/[projectId]/[number]/change-set.css"), "utf8");
    const d = declarations(css);
    expect(d.some((x) => x.selector === ".cs-inline-c > .cs-who" && x.prop === "flex" && x.value === "none")).toBe(true);
    expect(d.some((x) => x.selector === ".cs-inline-b" && x.prop === "min-width" && x.value === "0")).toBe(true);
  });

  it("a merge CI still holds reads Checks running…, disabled", async () => {
    const held = { state: "waiting", label: "Checks running…", reason: "CI is still running on this change." };
    const cs = parseChangeSet({ number: 1, state: "open", base_branch: "main",
                                merge: { how: "approve", decision_id: "dec_m1", held },
                                decision: { id: "dec_m1", status: "pending", question: "Approve code change (change #1)" } });
    const calls = stubFetch(() => undefined);
    await mountAs(<Decide cs={cs} projectId="hd" number="1" reread={async () => cs} />);
    const merge = tid("change-set-merge") as HTMLButtonElement;
    expect(merge.textContent).toContain("Checks running…");
    expect(merge.disabled).toBe(true);
    expect(tid("change-set-held")?.textContent).toContain("CI is still running");
    merge.click();
    expect(calls.filter((c) => c.method === "POST")).toEqual([]);
  });
});

describe("a pre-filled project name", () => {
  it("is asked of the engine, which answers the first free one", async () => {
    const calls = stubFetch((c) => (c.path === "/v1/projects/p1/add-project/name"
      ? { body: { name: "HelpDesk AI", free: "HelpDesk AI 2", taken: true } } : undefined));
    const r = await addProjectApi.freeName("p1", "HelpDesk AI");
    expect(r).toEqual({ name: "HelpDesk AI", free: "HelpDesk AI 2", taken: true });
    expect(calls[0].search).toBe("?name=HelpDesk%20AI");
  });
});
