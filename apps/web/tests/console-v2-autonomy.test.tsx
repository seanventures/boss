import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AutonomyControl, type AutonomyState } from "../components/v2/AutonomyControl";

/**
 * The autonomy switch.
 *
 * This is the one control in the console that REDUCES human oversight of a system that spends
 * money and deploys code, so it is held to different rules from every other control: say what
 * will change before changing it, ask twice in the unsafe direction only, and never claim an
 * owner it does not have.
 */

const source = () =>
  readFileSync(resolve(__dirname, "../components/v2/AutonomyControl.tsx"), "utf8");

const supervised: AutonomyState = {
  mode: "Supervised",
  detail: "Spend & deploys pause for you.",
  modes_available: ["Supervised", "Full auto"],
  threshold_usd: 500,
};

const full: AutonomyState = {
  mode: "Full auto",
  detail:
    "Reversible gates under $500 approve themselves. Deploys, merges, sends and anything irreversible still wait for you.",
  modes_available: ["Supervised", "Full auto"],
  threshold_usd: 500,
  set_by: "ceo",
};

const html = (initial: AutonomyState) =>
  renderToStaticMarkup(
    <AutonomyControl projectId="boss-dogfood" actor="executive_console" initial={initial} />,
  );

describe("what it says", () => {
  it("describes the consequence, not just the mode name", () => {
    const out = html(full);
    expect(out).toContain('data-testid="v2-autonomy-detail"');
    expect(out).toMatch(/still wait for you/);
  });

  it("names who turned oversight off, while it is off", () => {
    expect(html(full)).toContain('data-testid="v2-autonomy-setby"');
  });

  it("shows no owner line under supervision", () => {
    expect(html(supervised)).not.toContain('data-testid="v2-autonomy-setby"');
  });

  it("marks Full auto visually as the exceptional state", () => {
    expect(html(full)).toContain('data-mode="Full auto"');
  });
});

describe("the unsafe direction has friction and the safe one does not", () => {
  it("switching TO full auto offers a confirm step, not an immediate change", () => {
    const out = html(supervised);
    expect(out).toContain('data-testid="v2-autonomy-toggle"');
    expect(out).toMatch(/Switch to Full auto/);
  });

  it("returning to supervised is immediate", () => {
    // The safe direction should never have friction: a confirm dialogue between an operator
    // and re-enabling oversight is a dialogue that will eventually be dismissed by habit.
    const src = source();
    expect(src).toMatch(/isAuto \? void change\("supervised"\) : setConfirming\(true\)/);
  });

  it("the confirmation spells out the threshold and what still waits", () => {
    // At the moment of the decision, not in a tooltip somewhere.
    const src = source();
    expect(src).toMatch(/approve themselves with no human\s*\n?\s*review/);
    expect(src).toMatch(/irreversible will still wait for you/);
  });
});

describe("attribution", () => {
  it("the actor is passed and never defaulted", () => {
    // An autonomy change with no owner is the act that makes every later act unattributable.
    const src = source();
    expect(src).toMatch(/actor,/);
    expect(src).not.toMatch(/actor = "|actor \?\? "/);
  });

  it("the route handler refuses a change with no actor", () => {
    // Refused at the handler as well as at the API, so a caller cannot reach the write path
    // without one.
    const handler = readFileSync(resolve(__dirname, "../app/api/autonomy/route.ts"), "utf8");
    expect(handler).toMatch(/actor is required/);
    expect(handler).toMatch(/status: 400/);
  });

  it("goes through a route handler, never a Server Action", () => {
    // A Server Action re-renders the whole route to reply — the defect that made "Set target"
    // appear to hang for 115 seconds.
    const src = source();
    expect(src).toContain('fetch("/api/autonomy"');
    expect(src).not.toMatch(/useActionState|from "\.\.\/\.\.\/app\/actions"/);
  });
});

describe("what it reports", () => {
  it("reports what the API said, not what it hoped", () => {
    // "Retry started" printed regardless of the response is how a control lies about work
    // that never began.
    const src = source();
    expect(src).toMatch(/if \(!res\.ok\)/);
    expect(src).toMatch(/data as \{ error\?: string \}/);
  });
});
