// @vitest-environment happy-dom
/**
 * THE ONE FORM KIT (BOSSAPP-1039). Breaks covered: a checkbox detached from its words (clicking
 * the label must toggle the box, so the two can never sit apart); a control left with the
 * browser's default look (Field must hand it the console's `.input`/`.textarea` classes); and
 * the stuck drawer tooltip (tip-layer must ignore focus that code moved to a tabindex="-1"
 * heading, the MCP drawer's server description over the URL field).
 */
import React, { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { CheckGroup, CheckRow, Field, FormGrid } from "../components/form-kit";
import { useTips } from "../app/workflows/flow/tip-layer";
import { $, cleanup, render, settle } from "./_helpers/kit-render";

afterEach(cleanup);

function Checks() {
  const [on, setOn] = useState<string[]>([]);
  const toggle = (a: string) => setOn((now) => (now.includes(a) ? now.filter((x) => x !== a) : [...now, a]));
  return (
    <FormGrid label="Add rule" testId="fk-under-test">
      <Field label="Name"><input aria-label="Rule name" defaultValue="" /></Field>
      <Field label="Notes"><textarea aria-label="Rule notes" defaultValue="" /></Field>
      <CheckGroup label="Actions">
        {["import", "run"].map((a) => (
          <CheckRow key={a} label={a} checked={on.includes(a)} testId={`fk-check-${a}`} onChange={() => toggle(a)} />
        ))}
      </CheckGroup>
    </FormGrid>
  );
}

describe("the one form kit", () => {
  it("toggles the box when its words are clicked: the label wraps the input", async () => {
    await render(<Checks />);
    const box = $('[data-testid="fk-check-import"]') as HTMLInputElement;
    expect(box.checked).toBe(false);
    // The words, not the box: the walker found boxes half a row from their labels (890, 873).
    const words = [...document.querySelectorAll("label.check-row")]
      .find((l) => l.textContent?.trim() === "import")!.querySelector("span") as HTMLElement;
    words.click();
    await settle();
    expect(box.checked).toBe(true);
    expect(($('[data-testid="fk-check-run"]') as HTMLInputElement).checked).toBe(false);
  });

  it("gives every control the console's input look, never the browser default", async () => {
    await render(<Checks />);
    expect(($('[aria-label="Rule name"]') as HTMLElement).className).toContain("input");
    expect(($('[aria-label="Rule notes"]') as HTMLElement).className).toContain("textarea");
    expect($('[data-testid="fk-under-test"]')?.className).toContain("fk-form");
  });

  it("never shows a tip for focus that code moved to a tabindex=-1 heading", async () => {
    function Drawerish() {
      useTips();
      return (
        <div>
          <h3 tabIndex={-1} data-tip="The server's description" data-testid="fk-title">srv</h3>
          <button type="button" data-tip="A real control's tip" data-testid="fk-btn">Save</button>
        </div>
      );
    }
    await render(<Drawerish />);
    const title = $('[data-testid="fk-title"]')!;
    title.focus();
    title.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    await settle();
    expect(document.getElementById("wf-tip")?.hidden ?? true).toBe(true);
    const btn = $('[data-testid="fk-btn"]')!;
    btn.focus();
    btn.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    await settle();
    expect(document.getElementById("wf-tip")?.hidden).toBe(false);
    expect(document.getElementById("wf-tip")?.textContent).toBe("A real control's tip");
  });
});
