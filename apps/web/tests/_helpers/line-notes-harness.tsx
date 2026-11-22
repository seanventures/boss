/**
 * Files changed with validation notes pinned, for the tests: the target file's diff (every line
 * added, so each finding's branch line is a row), its header's Revert to original, and the notes
 * the change page pins (app/validation/line-notes.tsx useValidationPins) under their rows.
 */
import React, { useMemo } from "react";

import { parseUnifiedDiff } from "../../lib/diff-view";
import type { Validation } from "../../lib/validation-review";
import { useValidation, useValidationPins } from "../../app/validation/line-notes";
import { PinsContext } from "../../app/changes/[projectId]/[number]/pins";
import { DiffTable } from "../../app/changes/[projectId]/[number]/review-panes";

export function addedDiff(path: string, lines: number): string {
  return [`diff --git a/${path} b/${path}`, "new file mode 100644", "--- /dev/null", `+++ b/${path}`, `@@ -0,0 +1,${lines} @@`,
    ...Array.from({ length: lines }, (_, i) => `+line ${i + 1}`)].join("\n");
}

export function NotesOn({ v, projectId = "northgate-gateway", change = 9, onChanged = () => {}, originalCommits = 0 }: {
  v: Validation; projectId?: string; change?: number; onChanged?: () => void; originalCommits?: number;
}) {
  const pins = useValidationPins(v, projectId, change, onChanged, originalCommits);
  const n = Math.max(40, ...v.findings.map((f) => f.location.line ?? 0)) + 2;
  const file = useMemo(() => parseUnifiedDiff(addedDiff(v.target, n))[0], [v.target, n]);
  return (
    <PinsContext.Provider value={pins}>
      <div data-testid="harness-head">{pins?.head(v.target)}</div>
      <DiffTable file={file} inline={new Map()} />
    </PinsContext.Provider>
  );
}

/** The same, read from GET …/validation the way the change page reads it. */
export function NotesFromEngine({ projectId = "northgate-gateway", change = 9 }: { projectId?: string; change?: number }) {
  const { data, refresh } = useValidation(projectId, change);
  return data ? <NotesOn v={data} projectId={projectId} change={change} onChanged={refresh} /> : null;
}
