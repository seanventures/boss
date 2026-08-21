"use client";

import React, { useActionState } from "react";
import type { PreviewRec } from "@boss/api-client";

import type { PreviewFeed } from "../lib/code-tasks";
import {
  PREVIEW_FIXTURE_BADGE,
  PREVIEW_FIXTURE_CTA,
  PREVIEW_HOW_TO_REAL_VIEW,
  PREVIEW_REAL_EMPTY_COPY,
  PREVIEW_ZERO_REAL_RUNNING,
  inventoryPreviews,
  isFixturePreview,
  previewFailClosedReason,
  previewOpenLabel,
  previewOperatorHref,
} from "../lib/previews";
import { approvePreviewAction, createPreviewFixtureAction, previewGateAction } from "./actions";
import { SubmitButton } from "./submit-button";

const PREVIEW_API = process.env.NEXT_PUBLIC_BOSS_API_URL ?? "";

function resolveHref(href: string): string {
  return href.startsWith("http") ? href : `${PREVIEW_API}${href}`;
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
function ApprovePreview({ preview }: { preview: PreviewRec }) {
  const [gate, setGate] = React.useState<any>(null);
  const [state, action] = useActionState(approvePreviewAction, null);

  React.useEffect(() => {
    let live = true;
    if (!preview.id) return;
    previewGateAction(preview.id).then((r) => {
      if (live && r.ok) setGate(r.data);
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
    return (
      <span className="detail" data-testid="preview-approve-blocked" title={String(gate.blockers?.join("; ") ?? "")}>
        cannot approve: {String(gate.blockers?.[0] ?? "unknown")}
      </span>
    );
  }

  const willDeploy: string[] = gate.will_deploy ?? [];
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

function PreviewRow({ preview, appName }: { preview: PreviewRec; appName?: string }) {
  const href = previewOperatorHref(preview);
  const failClosed = previewFailClosedReason(preview);
  const fixture = isFixturePreview(preview);
  return (
    <div
      className="ct-outcome"
      data-testid={fixture ? "preview-row-fixture" : "preview-row-real"}
      data-preview-mode={preview.mode || (fixture ? "fixture" : "app")}
      data-preview-status={preview.status}
      style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}
    >
      {href ? (
        <a className="cap-btn" href={resolveHref(href)} target="_blank" rel="noreferrer">
          {previewOpenLabel(preview)} ↗
        </a>
      ) : (
        <span className="detail" data-preview-view={failClosed ? "fail-closed" : "missing-url"}>
          {failClosed ?? `preview ${preview.status} — no operator URL`}
        </span>
      )}
      <span className={`cap-badge ${fixture ? "tone-amber" : "tone-blue"}`}>
        {fixture ? PREVIEW_FIXTURE_BADGE : `preview: ${preview.status}`}
      </span>
      {/* WHICH APP THIS IS.
          The row showed a bare preview id, and this list is GLOBAL -- every running preview in
          the project, in creation order. Sitting under a BugBox card, ApproveIt's preview read
          as one of BugBox's, and the only way to tell them apart was to open both. Nothing was
          mismatched; the row simply never said what it was serving. */}
      {appName ? <strong style={{ fontSize: 13 }}>{appName}</strong> : null}
      {preview.id ? <code className="detail">{preview.id}</code> : null}
      <ApprovePreview preview={preview} />
    </div>
  );
}

export function PreviewsHonesty({
  feed,
  projectId,
  actor,
  createAction,
  createState,
  runTitles = {},
}: {
  feed: PreviewFeed;
  /** run id -> app name, so a row can say which application it is serving. */
  runTitles?: Record<string, string>;
  projectId: string;
  actor: string;
  createAction?: (formData: FormData) => void | Promise<void>;
  createState?: { id?: string; error?: string; open_url?: string } | null;
}) {
  const inventory = inventoryPreviews(feed.items);
  return (
    <div className="stack-card" data-testid="mission-control-previews">
      <p className="detail" data-testid="preview-counts">
        {inventory.realRunningCount} real running · {inventory.fixtureCount} fixture ·{" "}
        {inventory.expiredCount} expired
      </p>

      {feed.blockedReason ? (
        <p className="detail" role="status" data-preview-list="error">
          Preview list unavailable — {feed.blockedReason}. View links cannot be offered until the list loads.
        </p>
      ) : null}

      <article data-testid="preview-real-lane">
        <h3 style={{ marginTop: 0 }}>Real running apps</h3>
        {inventory.realRunningCount === 0 ? (
          <div className="detail" data-testid="preview-real-empty">
            <p>{PREVIEW_REAL_EMPTY_COPY}</p>
            <p>{PREVIEW_HOW_TO_REAL_VIEW}</p>
            <p data-testid="preview-zero-running">{PREVIEW_ZERO_REAL_RUNNING}</p>
          </div>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {inventory.realRunning.map((preview) => (
              <PreviewRow key={preview.id} preview={preview}
                          appName={preview.run_id ? runTitles[preview.run_id] : undefined} />
            ))}
          </div>
        )}
      </article>

      <article data-testid="preview-fixture-lane" style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>Fixture Views</h3>
        <p className="detail">
          A fixture proves the View HTML path. It is {PREVIEW_FIXTURE_BADGE}.
        </p>
        {inventory.fixtures.length === 0 ? (
          <p className="detail" data-testid="preview-fixture-empty">
            No fixture Views yet. Create one to prove the gateway without a microVM.
          </p>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {inventory.fixtures.map((preview) => (
              <PreviewRow key={preview.id} preview={preview}
                          appName={preview.run_id ? runTitles[preview.run_id] : undefined} />
            ))}
          </div>
        )}
        <form action={createAction} style={{ marginTop: 10, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <SubmitButton className="cap-btn" data-testid="preview-fixture-cta">
            {PREVIEW_FIXTURE_CTA}
          </SubmitButton>
        </form>
        {createState?.error ? (
          <p className="detail" role="status" data-testid="preview-fixture-error" style={{ color: "#f85149" }}>
            {createState.error}
          </p>
        ) : null}
        {createState?.id ? (
          <p className="detail" data-testid="preview-fixture-created">
            Fixture {createState.id} created — {PREVIEW_FIXTURE_BADGE}.
          </p>
        ) : null}
      </article>

      {inventory.expired.length > 0 ? (
        <article data-testid="preview-expired-lane" style={{ marginTop: 16 }}>
          <h3 style={{ marginTop: 0 }}>Expired / not running</h3>
          <p className="detail">View is fail-closed. Opening an expired preview returns 409.</p>
          <div style={{ display: "grid", gap: 8 }}>
            {inventory.expired.slice(0, 8).map((preview) => (
              <PreviewRow key={preview.id} preview={preview}
                          appName={preview.run_id ? runTitles[preview.run_id] : undefined} />
            ))}
          </div>
        </article>
      ) : null}
    </div>
  );
}

export function PreviewsPanel({
  feed,
  projectId,
  actor,
  runTitles,
}: {
  feed: PreviewFeed;
  projectId: string;
  actor: string;
  runTitles?: Record<string, string>;
}) {
  const [state, action] = useActionState(createPreviewFixtureAction, null);
  return (
    <PreviewsHonesty
      runTitles={runTitles}
      feed={feed}
      projectId={projectId}
      actor={actor}
      createAction={action}
      createState={state}
    />
  );
}
