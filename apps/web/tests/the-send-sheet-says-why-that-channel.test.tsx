/**
 * The send sheet says why that channel, beside the channel line, not only
 * in the title. A blank or whitespace why says nothing. More than six words
 * is a paragraph. Six or fewer is a span. The channel line stays
 * channelSentence(send) and does not include channel_why. The title stays
 * the raw send.channel_why.
 *
 * Breaks if the why stays padded, a blank why still renders, a seven-word
 * why is a span, the channel line swallows channel_why, the title is
 * trimmed, or the sheet stops calling channelWhySentence(send.channel_why).
 */
import { join } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import type { Send } from "../lib/self-fix";
import { send } from "./self-fix/_fixtures";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LONG = "the fork already exists so this opens there";
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "your fork exists";
const CHANNEL = "Opens a pull request on github.com/seanventures/boss from your fork example-person/boss.";

type SendChannelProps = { send: Send };

const sheet = await import("../app/self-fix/send-sheet") as {
  channelSentence: (row: Send) => string;
  channelWhySentence?: (why: string | null | undefined) => string | null;
  SendChannel?: (props: SendChannelProps) => React.ReactNode;
};
const { channelSentence } = sheet;
const channelWhySentence = sheet.channelWhySentence;

let root: Root | null = null;
let host: HTMLElement | null = null;

function dropHost(): void {
  act(() => { root?.unmount(); });
  host?.remove();
  root = null;
  host = null;
}

afterEach(() => {
  dropHost();
  document.body.innerHTML = "";
});

async function show(row: Send): Promise<HTMLElement> {
  dropHost();
  channelWhySentence!(row.channel_why);
  const Line = sheet.SendChannel!;
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<Line send={row} />);
  });
  return host;
}

function channelEl(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='sfx-send-channel']");
}

function whyEl(el: HTMLElement): Element | null {
  return el.querySelector("[data-testid='sfx-send-channel-why']");
}

describe("channelWhySentence", () => {
  it("is the trimmed why, or nothing when the why is blank or whitespace", () => {
    expect(channelWhySentence!(`  ${LONG}  `)).toBe(LONG);
    expect(channelWhySentence!(`  ${SHORT}  `)).toBe(SHORT);
    expect(channelWhySentence!(SIX)).toBe(SIX);
    expect(channelWhySentence!(`  ${SEVEN}  `)).toBe(SEVEN);
    expect(channelWhySentence!("")).toBeNull();
    expect(channelWhySentence!("   ")).toBeNull();
    expect(channelWhySentence!(" \n\t ")).toBeNull();
    expect(channelWhySentence!(null)).toBeNull();
    expect(channelWhySentence!(undefined)).toBeNull();
  });
});

describe("the send sheet says why that channel", () => {
  it("shows more than six words as a paragraph beside the channel line", async () => {
    const padded = `  ${LONG}  `;
    const row = send({ channel_why: padded });
    const shown = await show(row);
    const channel = channelEl(shown);
    const said = whyEl(shown);
    expect(channelSentence(row)).toBe(CHANNEL);
    expect(channelSentence(row)).not.toContain(LONG);
    expect(channel?.textContent).toBe(CHANNEL);
    expect(channel?.getAttribute("title")).toBe(padded);
    expect(said?.tagName).toBe("P");
    expect(said?.textContent).toBe(LONG);
    expect(said?.hasAttribute("data-user-text")).toBe(true);
    expect(channel?.nextElementSibling).toBe(said);
    expect(said?.parentElement).not.toBe(channel);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);

    const seven = await show(send({ channel_why: `  ${SEVEN}  ` }));
    const sevenSaid = whyEl(seven);
    expect(channelEl(seven)?.textContent).toBe(CHANNEL);
    expect(channelEl(seven)?.getAttribute("title")).toBe(`  ${SEVEN}  `);
    expect(sevenSaid?.tagName).toBe("P");
    expect(sevenSaid?.textContent).toBe(SEVEN);
    expect(sevenSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);
  });

  it("shows six words or fewer as a span beside the channel line", async () => {
    const six = await show(send({ channel_why: SIX }));
    const sixSaid = whyEl(six);
    expect(channelEl(six)?.textContent).toBe(CHANNEL);
    expect(channelEl(six)?.getAttribute("title")).toBe(SIX);
    expect(sixSaid?.tagName).toBe("SPAN");
    expect(sixSaid?.textContent).toBe(SIX);
    expect(sixSaid?.hasAttribute("data-user-text")).toBe(true);
    expect(channelEl(six)?.nextElementSibling).toBe(sixSaid);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);

    const short = await show(send({ channel_why: `  ${SHORT}  ` }));
    const shortSaid = whyEl(short);
    expect(channelEl(short)?.textContent).toBe(CHANNEL);
    expect(channelEl(short)?.getAttribute("title")).toBe(`  ${SHORT}  `);
    expect(shortSaid?.tagName).toBe("SPAN");
    expect(shortSaid?.textContent).toBe(SHORT);
    expect(shortSaid?.hasAttribute("data-user-text")).toBe(true);
  });

  it("says nothing beside the channel line when the why is blank", async () => {
    const spaces = await show(send({ channel_why: " \n\t " }));
    expect(channelEl(spaces)?.textContent).toBe(CHANNEL);
    expect(channelEl(spaces)?.getAttribute("title")).toBe(" \n\t ");
    expect(whyEl(spaces)).toBeNull();
    expect(spaces.textContent).toBe(CHANNEL);

    const blank = await show(send({ channel_why: "" }));
    expect(channelEl(blank)?.textContent).toBe(CHANNEL);
    expect(channelEl(blank)?.hasAttribute("title")).toBe(false);
    expect(whyEl(blank)).toBeNull();
    expect(blank.textContent).toBe(CHANNEL);
  });

  it("calls channelWhySentence(send.channel_why) and keeps the channel line", () => {
    const src = codeOnly(join(__dirname, "../app/self-fix/send-sheet.tsx"));
    const sentenceStart = src.indexOf("export function channelSentence");
    const sentenceEnd = src.indexOf("export function channelWhySentence");
    expect(sentenceStart).toBeGreaterThan(-1);
    expect(sentenceEnd).toBeGreaterThan(sentenceStart);
    expect(src.slice(sentenceStart, sentenceEnd)).not.toContain("channel_why");
    expect(src).toContain("channelWhySentence(send.channel_why)");
    expect(src).toContain("<SendChannel send={send} />");
    expect(src).toContain('data-testid="sfx-send-channel"');
    expect(src).toContain('title={send.channel_why || undefined}');
    expect(src).toContain("channelSentence(send)");
    expect(src).toContain('data-testid="sfx-send-channel-why"');
    expect(src).toContain("data-user-text");
    const used = src.indexOf("<SendChannel send={send} />");
    const similar = src.indexOf('data-testid="sfx-send-similar"');
    expect(used).toBeGreaterThan(-1);
    expect(similar).toBeGreaterThan(used);
    expect(src).toContain('title={send.dedupe?.why || "Searched by the fix\'s class signature"}');
    expect(src).toContain('Similar: {DEDUPE[send.dedupe?.state ?? "skipped"]}');
  });
});
