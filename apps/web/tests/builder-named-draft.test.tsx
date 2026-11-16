// @vitest-environment happy-dom
/**
 * Opening a named draft shows its own chat and preview (BOSSAPP-1245).
 * The published URL and the commit list on the bench do not change.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { NamedDraft } from "../app/builder/named-draft";
import { $, cleanup, click, render, type } from "./_helpers/kit-render";

const PUBLISHED = "https://helpdesk.example/live";
const COMMITS = ["cafebabe"];

afterEach(() => { cleanup(); });

async function openNamed(name: string) {
  await type("[data-testid=named-draft-name]", name);
  await click("[data-testid=named-draft-open]");
}

function published() {
  return $("[data-testid=named-draft-published-url]")!.textContent;
}

function commits() {
  return $("[data-testid=named-draft-commits]")!.getAttribute("data-commits");
}

describe("a named draft stays off git and off the published URL", () => {
  it("opens its own chat and preview without a commit", async () => {
    await render(<NamedDraft appId="helpdesk" publishedUrl={PUBLISHED} commits={COMMITS} />);
    expect(published()).toBe(PUBLISHED);
    expect(commits()).toBe("cafebabe");

    await openNamed("evening-copy");

    expect(published()).toBe(PUBLISHED);
    expect(commits()).toBe("cafebabe");
    expect($("[data-testid=named-draft-git-sync]")!.textContent).toBe("Git sync off");
    expect($("[data-testid=named-draft-commit-flag]")!.textContent).toBe("No commit");
    expect($("[data-testid=named-draft-preview-url]")!.textContent)
      .toBe("https://draft.boss.test/helpdesk/evening-copy");
    expect($("[data-testid=named-draft-preview]")!.getAttribute("data-preview-url"))
      .toBe("https://draft.boss.test/helpdesk/evening-copy");
    expect($("[data-testid=named-draft-chat]")!.getAttribute("data-chat-id"))
      .toBe("chat_helpdesk_evening-copy");
    expect($("[data-testid=named-draft-transcript]")!.textContent).toBe("");

    await type("[data-testid=named-draft-message]", "Move the price under the title");
    await click("[data-testid=named-draft-send]");
    expect($("[data-testid=named-draft-transcript]")!.textContent)
      .toContain("Move the price under the title");
    expect(published()).toBe(PUBLISHED);
    expect(commits()).toBe("cafebabe");

    await openNamed("morning-copy");
    expect($("[data-testid=named-draft-transcript]")!.textContent).toBe("");
    expect($("[data-testid=named-draft-preview-url]")!.textContent)
      .toBe("https://draft.boss.test/helpdesk/morning-copy");
    expect($("[data-testid=named-draft-chat]")!.getAttribute("data-chat-id"))
      .toBe("chat_helpdesk_morning-copy");
    expect(published()).toBe(PUBLISHED);
    expect(commits()).toBe("cafebabe");

    await openNamed("evening-copy");
    expect($("[data-testid=named-draft-transcript]")!.textContent)
      .toContain("Move the price under the title");
    expect($("[data-testid=named-draft-preview-url]")!.textContent)
      .toBe("https://draft.boss.test/helpdesk/evening-copy");
    expect(published()).toBe(PUBLISHED);
    expect(commits()).toBe("cafebabe");
  });

  it("a blank name is said, and the live URL stays", async () => {
    await render(<NamedDraft appId="helpdesk" publishedUrl={PUBLISHED} commits={COMMITS} />);
    await openNamed("   ");
    expect($("[data-testid=named-draft-error]")!.textContent).toBe("Name the draft.");
    expect($("[data-testid=named-draft-preview]")).toBeNull();
    expect(published()).toBe(PUBLISHED);
    expect(commits()).toBe("cafebabe");
  });

  it("a draft preview is not the published URL when the two would match", async () => {
    const colliding = "https://draft.boss.test/helpdesk/evening-copy";
    await render(<NamedDraft appId="helpdesk" publishedUrl={colliding} commits={COMMITS} />);
    await openNamed("evening-copy");
    expect(published()).toBe(colliding);
    expect($("[data-testid=named-draft-preview-url]")!.textContent)
      .toBe("https://draft.boss.test/helpdesk/evening-copy/proof");
    expect(commits()).toBe("cafebabe");
  });
});
