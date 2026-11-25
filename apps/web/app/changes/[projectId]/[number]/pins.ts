import React from "react";

import type { DiffFile } from "../../../../lib/diff-view";

/**
 * NOTES PINNED ON LINES OF THE DIFF (validation findings, DESIGN decision 4), beside the review
 * comments. The diff asks for each file once; `row` answers the notes under one row of it (the
 * new side's line, or a deleted line's old one), `top` what has no row in this diff. `head` adds
 * to a file's header (Revert to original).
 */
export type Pins = {
  forFile: (file: DiffFile) => { row: (side: "new" | "old", line: number) => React.ReactNode; top: React.ReactNode };
  head: (path: string) => React.ReactNode;
  /** Files that open with the diff (the validated file), beside the first few. */
  open: string[];
};

export const PinsContext = React.createContext<Pins | null>(null);
