// @vitest-environment happy-dom
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { PreviewsTab, restartWhySentence } from "../app/infra/previews";
import { mockInfraFetch, previewRow, type Route } from "./_helpers/infra-fixtures";
import { byTestId, mount, settle, TabHarness, type Mounted } from "./_helpers/infra-tab-render";

/**
 * A disabled Restart already puts restartWhy on the title and aria-description.
 * The same reason has to be readable on the row. A trimmed reason is the sentence.
 * Blank is nothing. A sentence longer than six words is a paragraph. The button stays.
 */

const LONG = "Its build no longer exists, so it cannot start again";
const PADDED = `  ${LONG}  `;
const SIX = "The old build is gone now";
const SHORT = "Build is gone";

let m: Mounted | null = null;
let restore: (() => void) | null = null;
afterEach(async () => { await m?.unmount(); m = null; restore?.(); restore = null; });

function preview(id: string, base: string, over: Record<string, unknown>) {
  const row = previewRow(base);
  return { ...row, id, app: id, ...over, can: { ...(row.can as object), ...(over.can as object) } };
}

const routes = (): Route[] => [{
  match: /^\/v1\/infra\/previews$/,
  etag: true,
  reply: () => ({
    counts: { live: 1, failed_24h: 5, ended_24h: 0 },
    live: [preview("pv_on", "pv_live", { can: { restart: true }, restart_why: LONG })],
    failed_24h: [
      preview("pv_long", "pv_fail", { can: { restart: false }, restart_why: PADDED }),
      preview("pv_six", "pv_fail", { can: { restart: false }, restart_why: SIX }),
      preview("pv_short", "pv_fail", { can: { restart: false }, restart_why: SHORT }),
      preview("pv_blank", "pv_fail", { can: { restart: false }, restart_why: "   " }),
      preview("pv_none", "pv_fail", { can: { restart: false }, restart_why: null }),
    ],
    ended_24h: [],
    partial: [],
  }),
}];

async function open() {
  const f = mockInfraFetch({ routes: routes() });
  restore = f.install();
  m = await mount(<TabHarness Tab={PreviewsTab} />, "?area=infra&view=previews");
  await settle(() => !!byTestId("inf-previews"), "previews");
  return f;
}

const rowOf = (id: string) => document.querySelector<HTMLElement>(`[data-testid="inf-preview-row"][data-id="${id}"]`);

function sentenceEl(scope: ParentNode, sentence: string): Element | undefined {
  return [...scope.querySelectorAll("p, span")].find((el) => el.textContent === sentence);
}

describe("the preview says why restart is off", () => {
  it("shows the trimmed reason beside the disabled Restart, as a paragraph when it is longer than six words", async () => {
    await open();
    const long = rowOf("pv_long");
    expect(long).not.toBeNull();
    const btn = long!.querySelector<HTMLButtonElement>('[data-testid="inf-preview-restart"]');
    expect(btn).not.toBeNull();
    expect(btn!.disabled).toBe(true);
    expect(btn!.getAttribute("title")).toBe(PADDED);
    expect(btn!.getAttribute("aria-description")).toBe(PADDED);
    expect(btn!.textContent).toContain("Restart");
    expect(long!.textContent).toContain(LONG);
    expect(sentenceEl(long!, LONG)?.tagName).toBe("P");

    const six = rowOf("pv_six")!;
    expect(six.textContent).toContain(SIX);
    expect(sentenceEl(six, SIX)?.tagName).toBe("SPAN");
    expect(six.querySelector('[data-testid="inf-preview-restart"]')).not.toBeNull();

    const short = rowOf("pv_short")!;
    expect(short.textContent).toContain(SHORT);
    expect(sentenceEl(short, SHORT)?.tagName).toBe("SPAN");

    const blank = rowOf("pv_blank")!;
    expect(blank.querySelector('[data-testid="inf-preview-restart"]')).not.toBeNull();
    expect(blank.textContent ?? "").not.toContain(LONG);
    expect([...blank.querySelectorAll("p")]).toHaveLength(0);

    const none = rowOf("pv_none")!;
    expect(none.querySelector('[data-testid="inf-preview-restart"]')).toBeNull();
    expect(none.textContent ?? "").not.toContain(LONG);

    const on = rowOf("pv_on")!;
    const onBtn = on.querySelector<HTMLButtonElement>('[data-testid="inf-preview-restart"]')!;
    expect(onBtn.disabled).toBe(false);
    expect(onBtn.getAttribute("title")).toBe("Start it again with the same build");
    expect(on.textContent ?? "").not.toContain(LONG);
  });
});

describe("restartWhySentence", () => {
  it("is the trimmed reason, or nothing when the reason is blank", () => {
    expect(restartWhySentence(PADDED)).toBe(LONG);
    expect(restartWhySentence(SHORT)).toBe(SHORT);
    expect(restartWhySentence(SIX)).toBe(SIX);
    expect(restartWhySentence(null)).toBeNull();
    expect(restartWhySentence(undefined)).toBeNull();
    expect(restartWhySentence("")).toBeNull();
    expect(restartWhySentence(" \n\t ")).toBeNull();
  });
});
