"use client";

import React from "react";

import { Markdown } from "./markdown";

/**
 * The architecture document on an Approvals card: open, rendered, with its version — a person
 * approves what they read, so it is not tucked behind a click the way a long plan is.
 */
export function ArchitectureDoc({ doc }: { doc: { markdown: string; version: number } }) {
  return (
    <details className="decision-plan-details" open data-testid="decision-architecture-doc">
      <summary>
        Architecture, version {doc.version}
        <span className="dq-doc-size">
          {" · "}
          {doc.markdown.length.toLocaleString("en-US")} characters
        </span>
      </summary>
      <div className="decision-plan-sec arch-doc">
        <Markdown text={doc.markdown} />
      </div>
    </details>
  );
}
