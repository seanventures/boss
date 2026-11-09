"use client";

import React, { useEffect, useState } from "react";

import { DecisionActions } from "./decision-inbox-ui";
import { Markdown } from "./markdown";
import { useSearch } from "./research/bits";
import type { DecisionFeedItem } from "../lib/decisions";
import {
  type ArchitectureReview,
  answerable,
  architecturePill,
  fetchArchitectureReview,
  missingSections,
} from "../lib/architecture";
import { decisionHref } from "../lib/research-panels";

/** The decision as the Approvals card's buttons take it, so both places answer the same way. */
export function architectureDecisionItem(review: ArchitectureReview): DecisionFeedItem | null {
  const d = review.decision;
  if (!d) return null;
  return {
    id: d.id, question: `Architecture v${review.current_version}`, status: d.status, riskTier: 2,
    recommendation: { action: "review_architecture", workflow_run_id: review.workflow_run_id,
                      step_id: review.step_id, capability: "architecture" },
    options: [], costSummary: {}, createdAt: "", updatedAt: "",
    kind: "architecture_review", allowedActions: d.actions,
  };
}

/**
 * The Architecture step's panel, modeled on the PRD panel: where it stands in words, the
 * document (every version, the current one first), what a person asked to change, and the
 * same Approve / Request changes / Reject the Approvals card offers.
 */
export function ArchitecturePanel({ reviewId, projectId, actorId = "", stepStatus = "" }: {
  reviewId: string; projectId: string; actorId?: string; stepStatus?: string;
}) {
  const search = useSearch();
  const [review, setReview] = useState<ArchitectureReview | null>(null);
  const [error, setError] = useState<string>("");
  const [shown, setShown] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    void fetchArchitectureReview(reviewId, projectId).then((got) => {
      if (!live) return;
      if (got.ok) { setReview(got.review); setError(""); } else setError(got.error);
    });
    return () => { live = false; };
  }, [reviewId, projectId, stepStatus]);

  if (!review) {
    return (
      <div className="rp rp-compact" data-testid="architecture-panel">
        <span className={error ? "rp-err" : "rp-sub"}>
          {error ? `The architecture could not be read: ${error}` : "Opening the architecture…"}
        </span>
      </div>
    );
  }
  const versions = review.versions ?? [];
  const current = versions.find((v) => v.version === review.current_version) ?? versions.at(-1);
  const selected = versions.find((v) => v.version === shown) ?? current;
  const pill = architecturePill(review);
  const missing = selected ? missingSections(review, selected) : [];
  const decision = architectureDecisionItem(review);

  return (
    <div className="rp rp-compact" data-testid="architecture-panel" data-status={review.status}>
      <div className="arch-head">
        <b>Architecture</b>
        <span className={`dq-impact ${pill.tone === "good" ? "teal" : pill.tone === "bad" ? "red"
                          : pill.tone === "warn" ? "amber" : "muted"}`}
              data-testid="architecture-pill">{pill.label}</span>
      </div>
      <p className="rp-sub" data-testid="architecture-status">{review.status_words}</p>
      {versions.length > 1 ? (
        <div className="arch-versions" role="tablist" aria-label="Versions">
          {versions.map((v) => (
            <button key={v.version} type="button" role="tab"
                    aria-selected={v.version === selected?.version}
                    className={`rp-btn sm${v.version === selected?.version ? " primary" : ""}`}
                    title={v.revised_for ? `Revised for: ${v.revised_for}` : "First version"}
                    onClick={() => setShown(v.version)}>
              v{v.version}
            </button>
          ))}
        </div>
      ) : null}
      {selected?.revised_for ? (
        <p className="rp-sub" data-testid="architecture-revised-for">
          Version {selected.version} answers: “{selected.revised_for}”
        </p>
      ) : null}
      {missing.length ? (
        <div className="rp-attn" data-testid="architecture-missing">
          <b>Not covered</b><span>{missing.join(", ")}. Request changes to have them added.</span>
        </div>
      ) : null}
      {selected ? (
        <div className="arch-doc" data-testid="architecture-doc"><Markdown text={selected.markdown} /></div>
      ) : null}
      {answerable(review) && decision ? (
        actorId ? (
          <DecisionActions decision={decision} projectId={projectId} actorId={actorId} />
        ) : (
          <a className="rp-btn sm primary" href={decisionHref(search, decision.id)}>
            Review in Approvals</a>
        )
      ) : null}
    </div>
  );
}
