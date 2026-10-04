"use client";

import React, { useActionState } from "react";
import type { PreviewRec } from "@boss/api-client";

import { forgetPreviewGate, loadPreviewGate } from "../lib/preview-gates";
import { isFixturePreview } from "../lib/previews";
import { approvePreviewAction } from "./actions";
import { SubmitButton } from "./submit-button";

import { callAction } from "../lib/call-action";
import { browserHref } from "../lib/browser-links";
function resolveHref(href: string): string {
  return browserHref(href);
}

/**
 * The single human gate between "the preview looks right" and "it is live".
 *
 * Deliberately NOT a new console page. The judgement being made here is about what is on
 * screen in THIS preview, so the control belongs on the preview row; a separate Approvals
 * area would be a third place to look for one delivery.
 *
 * Approving does not deploy. It approves the run's merge decision, which merges, which fires
 * the delivery trigger that already existed — so there stays exactly one path to a deployment.
 * The gate is fetched BEFORE the button renders so the row can say what approving will do,
 * and refuse when it would do nothing.
 */
/**
 * Approve the merge, and ship what that merges.
 *
 * Exported because it now lives on the app's own detail bar, beside the PR it merges and the
 * preview it was judged from. It used to sit only in a separate "Previews" panel further down
 * the page — a second place to look for an action about the app you already had open.
 */
export function ApprovePreview(
  { preview, compact = false }: { preview: PreviewRec; compact?: boolean },
) {
  const [gate, setGate] = React.useState<any>(null);
  const [state, action] = useActionState(callAction(approvePreviewAction), null);

  // A BATCHED ROUTE HANDLER, NOT A PER-ROW SERVER ACTION.
  //
  // A Server Action re-renders the whole route to reply, so nine preview rows cost nine
  // renders of a page that takes tens of seconds to render. The gate still has to be read
  // before the button appears — the row says what approving will do — so it is batched rather
  // than dropped. Same fix as the app cards; see lib/batch-fetch.ts.
  React.useEffect(() => {
    let live = true;
    if (!preview.id) return;
    // After an approval the cached gate describes the world before it.
    if (state?.ok) forgetPreviewGate(preview.id);
    loadPreviewGate(preview.id).then((g) => {
      // `error` means the lookup failed, which is NOT "this row has no gate". Leaving `gate`
      // null renders nothing, which is the honest state: we have not found out yet.
      if (live && !g.error) setGate(g);
    });
    return () => {
      live = false;
    };
  }, [preview.id, state?.ok]);

  if (!preview.id || isFixturePreview(preview)) return null;
  if (!gate) return null;

  if (gate.decision_status && gate.decision_status !== "pending") {
    return (
      <span className="cap-badge tone-green" data-testid="preview-already-approved">
        merge {String(gate.decision_status)}
      </span>
    );
  }
  if (!gate.can_approve) {
    // Say WHY rather than rendering a dead button. "You cannot approve" without a reason is
    // the thing that sends people to the API to find out.
    //
    // In a button bar the reason goes in the tooltip: these strings run to a full sentence and
    // an action bar is not a place for a sentence.
    if (compact) return null;
    return (
      <span className="detail" data-testid="preview-approve-blocked" title={String(gate.blockers?.join("; ") ?? "")}>
        cannot approve: {String(gate.blockers?.[0] ?? "unknown")}
      </span>
    );
  }

  const willDeploy: string[] = gate.will_deploy ?? [];
  const effect = willDeploy.length
    ? `merges ${gate.title ? `“${gate.title}”` : "the PR"} → deploys ${willDeploy.join(", ")}`
    : "merges the PR (no auto-deploy target configured)";

  // COMPACT: A BUTTON, NOT A PARAGRAPH.
  //
  // The effect line names every app the merge would deploy. On a full-width panel that reads
  // fine; in an action bar it is a sentence hundreds of pixels long that shoves everything
  // after it off the edge of the screen. The information still has to be available BEFORE the
  // click, so it moves into the tooltip and the button says how many apps in a word.
  if (compact) {
    return (
      <form action={action} style={{ display: "inline-flex" }} title={effect}>
        <input type="hidden" name="preview_id" value={preview.id} />
        <SubmitButton className="cap-btn tone-green" data-testid="preview-approve">
          Approve &amp; ship
          {willDeploy.length ? (
            <span className="ct-approve-n" data-testid="preview-approve-count">
              {willDeploy.length}
            </span>
          ) : null}
        </SubmitButton>
      </form>
    );
  }

  return (
    <form action={action} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <input type="hidden" name="preview_id" value={preview.id} />
      <SubmitButton className="cap-btn tone-green" data-testid="preview-approve">
        Approve &amp; ship
      </SubmitButton>
      {/* What the click actually causes, before it is clicked. */}
      <span className="detail" data-testid="preview-approve-effect">
        {willDeploy.length
          ? `merges ${gate.title ? `“${gate.title}”` : "the PR"} → deploys ${willDeploy.join(", ")}`
          : "merges the PR (no auto-deploy target configured)"}
      </span>
      {state && !state.ok ? (
        <span className="cap-badge tone-red" data-testid="preview-approve-error">
          {state.error}
        </span>
      ) : null}
    </form>
  );
}
