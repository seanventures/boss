import { describe, expect, it } from "vitest";

import { fromInteractiveControl } from "../app/code-tasks-ui";
import { codeOnly } from "./_helpers/source";

/**
 * Operating a control on an app card must not navigate away from the card.
 *
 * The card is a `role="button"` wrapping the whole tile, and the tile holds real controls: a
 * host dropdown, an app-name field, "Set target", "Run preview". Every click bubbled to the
 * card, so pressing "Set target" opened the detail page instead of setting the target.
 *
 * The keyboard case was worse because it was silent: the card answers Enter AND SPACE, so
 * typing a space into the app-name field navigated away mid-word — the operator lost what they
 * had typed and had no idea why.
 */

type FakeTarget = { closest: (sel: string) => unknown };
const event = (hit: unknown, currentTarget: unknown = { card: true }) =>
  ({ target: { closest: () => hit } as FakeTarget, currentTarget } as never);

describe("a click that came from a control", () => {
  it.each(["button", "a", "input", "select", "textarea", "label", "option"])(
    "is recognised when it originates in a <%s>", (tag) => {
      expect(fromInteractiveControl(event({ tag }))).toBe(true);
    });

  it("is not recognised when the click landed on the card body", () => {
    // Clicking the title, the badge or blank space still opens the app — that is the card's job.
    expect(fromInteractiveControl(event(null))).toBe(false);
  });

  it("does not count the card itself, which carries role=button", () => {
    // `closest` walks UP and would match the wrapper if the wrapper were ever a control; that
    // would disable the card's own click entirely.
    const card = { card: true };
    expect(fromInteractiveControl(event(card, card))).toBe(false);
  });

  it("survives an event whose target cannot be walked", () => {
    // Events from outside the DOM (synthetic, tests, some portals) have no closest().
    expect(fromInteractiveControl({ target: {}, currentTarget: {} } as never)).toBe(false);
    expect(fromInteractiveControl({ target: null, currentTarget: {} } as never)).toBe(false);
  });
});
