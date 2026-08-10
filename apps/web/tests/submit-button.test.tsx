import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

/**
 * Every action in this console is a server action behind a `<form>`. A plain `<button>`
 * inside one looks identical before and after a press while the action runs for seconds,
 * so people press Approve two and three times and report that "nothing happens".
 *
 * These assert what a person can SEE — busy state, a disabled control, the pressed button
 * being the one that spins — not which hook produced it.
 */

const status = { pending: false, data: null as FormData | null, method: "post", action: null };
vi.mock("react-dom", async () => {
  const actual = await vi.importActual<typeof import("react-dom")>("react-dom");
  return { ...actual, useFormStatus: () => status };
});

const { SubmitButton } = await import("../app/submit-button");

function render(node: React.ReactElement) {
  return renderToStaticMarkup(node);
}

function idle() {
  status.pending = false;
  status.data = null;
}

function pressed(name?: string, value?: string) {
  status.pending = true;
  const data = new FormData();
  if (name && value) data.set(name, value);
  status.data = data;
}

describe("a pressed submit button shows that it was pressed", () => {
  it("is inert-looking before the press", () => {
    idle();
    const html = render(<SubmitButton>Approve</SubmitButton>);
    expect(html).toContain("Approve");
    expect(html).not.toContain("btn-spinner");
    expect(html).not.toContain("disabled");
  });

  it("reports busy to sighted and assistive users while the action runs", () => {
    pressed();
    const html = render(<SubmitButton>Approve</SubmitButton>);
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain("btn-spinner");
    expect(html).toContain("disabled");
  });

  it("can say what it is doing rather than what it does", () => {
    pressed();
    const html = render(<SubmitButton pendingLabel="Approving…">Approve</SubmitButton>);
    expect(html).toContain("Approving…");
  });

  it("cannot be pressed twice", () => {
    pressed();
    expect(render(<SubmitButton>Approve</SubmitButton>)).toContain("disabled");
  });

  it("respects a disabled state the page already decided on", () => {
    idle();
    expect(render(<SubmitButton disabled>Approve</SubmitButton>)).toContain("disabled");
  });
});

describe("one form, several actions (Approve / Revise / Reject)", () => {
  const row = (
    <>
      <SubmitButton name="action" value="approve">Approve</SubmitButton>
      <SubmitButton name="action" value="revise">Revise</SubmitButton>
    </>
  );

  it("spins only the button that was actually pressed", () => {
    pressed("action", "approve");
    // Split on the element boundary; slicing by label offset overlaps the sibling's markup.
    const [approve, revise] = render(row).split("</button>");
    expect(approve).toContain('value="approve"');
    expect(approve).toContain("btn-spinner");
    expect(revise).toContain('value="revise"');
    expect(revise).not.toContain("btn-spinner");
  });

  it("still locks the siblings, so a second action cannot start mid-flight", () => {
    pressed("action", "approve");
    const html = render(row);
    expect(html.match(/disabled/g) ?? []).toHaveLength(2);
  });

  it("keeps the name/value the server action dispatches on", () => {
    idle();
    const html = render(row);
    expect(html).toContain('name="action"');
    expect(html).toContain('value="approve"');
    expect(html).toContain('value="revise"');
  });
});
