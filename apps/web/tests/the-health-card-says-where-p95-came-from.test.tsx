// @vitest-environment happy-dom
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { P95SourceLine } from "../app/projects/tab-health";

/** What production change this catches: the card hides an app or probe source, or invents one. */
function show(source?: string) {
  const e = source === undefined ? { key: "staging" } : { key: "staging", p95_source: source };
  document.body.innerHTML = renderToStaticMarkup(<P95SourceLine e={e} />);
  return document.querySelector('[data-testid="px-h-env-p95-source-staging"]');
}

describe("the health card says where p95 came from", () => {
  it("says the app's requests when the source is the app", () => {
    const node = show("app");
    expect(node).not.toBeNull();
    expect(node?.textContent).toBe("From the app's requests.");
  });

  it("says health probes when the source is a probe", () => {
    const node = show("probe");
    expect(node).not.toBeNull();
    expect(node?.textContent).toBe("From health probes.");
  });

  it("says nothing when the source is missing or anything else", () => {
    expect(show(undefined)).toBeNull();
    expect(document.body.textContent ?? "").not.toMatch(/From the app's requests|From health probes/);
    expect(show("")).toBeNull();
    expect(show("gateway")).toBeNull();
  });
});
