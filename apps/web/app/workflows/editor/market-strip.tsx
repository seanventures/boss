"use client";

/**
 * FROM THE MARKETPLACE: one strip under the editor's bar on a workflow installed from a pack.
 * Where it came from and at which version; what it waits for when it cannot run yet; and, when
 * you changed it and a newer version shipped, Review update (the one amber thing here).
 * Uninstall is on the ⋯ menu.
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useState } from "react";

import type { EditorMeta, InstalledAct, InstalledFrom } from "../../../lib/workflow-editor-types";
import { UpdateReview } from "../update-review";

export function MarketStrip({ projectId, meta, blocked, onUpdated }: {
  projectId: string;
  meta: EditorMeta;
  /** Why the update cannot be answered now (unsaved changes, an older version open). */
  blocked: string | null;
  onUpdated: (after: InstalledFrom | null, act: InstalledAct) => void;
}): React.JSX.Element | null {
  const [open, setOpen] = useState(false);
  const m = meta.marketplace;
  if (!m || !meta.definition_id || !meta.workflow_key) return null;
  return (
    <section className="wf-std" aria-label="From the Marketplace" data-testid="wf-market-strip">
      {open && m.update ? (
        <UpdateReview projectId={projectId} workflowKey={meta.workflow_key} definitionId={meta.definition_id}
                      installed={m} blocked={blocked} onClose={() => setOpen(false)}
                      onDone={(after, act) => { setOpen(false); onUpdated(after, act); }} />
      ) : null}
      <div className="wf-std-row">
        <span className="wf-std-hint" data-testid="wf-market-line"
              data-tip={m.modified ? "You changed it: a newer version waits for you." : "Updates by itself when the pack does."}>
          From Marketplace · <span data-user-text>{m.pack_name}</span> v{m.version ?? "?"}{m.modified ? " · Modified" : ""}
        </span>
        {m.waiting ? (
          <span className="wf-chip" data-tone="amber" data-tip={m.waiting} data-testid="wf-market-waiting">Waiting</span>
        ) : null}
        <span className="wf-std-sp" />
        {m.update && !open ? (
          <button type="button" className="wf-btn wf-btn-amber" data-testid="wf-market-review"
                  onClick={() => setOpen(true)}>Review update v{m.update.version ?? "?"}</button>
        ) : null}
      </div>
    </section>
  );
}
