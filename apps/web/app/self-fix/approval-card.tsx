"use client";

/**
 * ONE CARD TO SAY YES TO A BOSS SELF-FIX (docs/product/BOSS_FIXES_ITSELF.md §10.2).
 *
 *   Install a BOSS self-fix?                                Tier 3, host effect ⓘ
 *   fix(deploy): a release that leaves the resources' project retires its app there
 *   WHAT BROKE, WHY (and the rejected first guess), CONFIDENCE
 *   CHANGE   files +58 −1 [View diff], host effect
 *   TESTS    ✗→✓ NEW test_…py, 41 affected, P0 tier, no test weakened
 *   REVIEW   code ✓ security ✓ (other engine) regression ✓ consistency ✓
 *   COST     $3.12, 14 min, engine
 *   ON APPROVE  Installs on this one-box and resumes the step.   UNDO …
 *
 * Drawn inside its Approvals row for `recommendation.action === "self_fix.install"`; the answer
 * goes through the inbox's own form (DecisionActions) with the card's content_hash, so an approval
 * of a fix that changed since the card was drawn is refused by the engine.
 */
import React, { useState } from "react";

import type { DecisionFeedItem } from "../../lib/decisions";
import { readSelfFix, selfFixAreaHref, usd, type Card, type DiffView } from "../../lib/self-fix";

export const SELF_FIX_ACTION = "self_fix.install";

export function isSelfFixDecision(decision: Pick<DecisionFeedItem, "recommendation">): boolean {
  return decision.recommendation?.action === SELF_FIX_ACTION;
}

export function cardOf(rec: Record<string, unknown>): Card | null {
  const c = rec.card;
  return c && typeof c === "object" ? (c as Card) : null;
}

const SEATS: Record<string, string> = {
  review_code: "code", security_check: "security", regression: "regression", consistency_check: "consistency",
};
const passed = (v?: string) => ["pass", "passed", "approve", "approved", "ok", "clean"].includes((v ?? "").toLowerCase());
const base = (p: string) => p.split("/").pop() || p;

export function SelfFixCard({ card, projectId, selfFixId }: { card: Card; projectId: string; selfFixId?: string }) {
  const [diff, setDiff] = useState<DiffView | null>(null);
  const [diffError, setDiffError] = useState("");
  const [open, setOpen] = useState(false);
  const id = selfFixId || card.self_fix_id;
  const hostEffect = (card.change?.host_effect ?? []).length > 0 || (card.flags ?? []).includes("host_effect");
  const showDiff = async () => {
    setOpen((v) => !v);
    if (diff || !id) return;
    const got = await readSelfFix<DiffView>("diff", { project_id: projectId, id });
    if (got.ok && !got.notModified) setDiff(got.data);
    else if (!got.ok) setDiffError(got.error);
  };
  const rejected = (card.rejected ?? []).filter((r) => r && (r.hypothesis || r.why));
  const reviewSeats = Object.entries(card.review ?? {});
  const files = card.change?.files ?? [];

  return (
    <div className="sfx-card" data-testid="sfx-card">
      <div className="sfx-card-head">
        <span className="sfx-card-area-name" title="BOSS found a bug in its own code, fixed it and tested it. It installs only if you approve.">
          Install a BOSS self-fix?
        </span>
        <span className="cap-badge tone-amber" data-testid="sfx-card-tier"
              title={card.tier >= 3 ? "Two people approve on a shared install" : "One person approves"}>
          Tier {card.tier}
        </span>
        {hostEffect ? (
          <span className="cap-badge tone-red" data-testid="sfx-card-host-effect"
                title={(card.change?.host_effect ?? []).join("; ") || "Changes a command BOSS runs on a host"}>
            host effect
          </span>
        ) : null}
      </div>
      <h3 className="dq-title sfx-card-title" data-testid="sfx-card-title">{card.title}</h3>
      <dl className="sfx-card-grid">
        <dt>What broke</dt>
        <dd data-testid="sfx-card-broke" data-user-text>{card.what_broke || "—"}</dd>
        <dt>Why</dt>
        <dd data-testid="sfx-card-why" data-user-text>
          {card.why || "—"}
          {rejected.length ? (
            <span className="sfx-rejected" data-testid="sfx-card-rejected"
                  title={rejected.map((r) => `${r.hypothesis ?? ""}${r.why ? `, ${r.why}` : ""}`).join("\n")}>
              {" "}First guess rejected ⓘ
            </span>
          ) : null}
        </dd>
        <dt>Confidence</dt>
        <dd data-testid="sfx-card-confidence" title={(card.confidence?.reasons ?? []).join(", ")}>
          {card.confidence?.level ?? "—"}
        </dd>
        <dt>Change</dt>
        <dd data-testid="sfx-card-change">
          <span title={files.join("\n")}>
            {files.length ? files.slice(0, 3).map(base).join(", ") : "no files"}
            {files.length > 3 ? ` +${files.length - 3}` : ""}
          </span>
          {" "}<span className="sfx-plus">+{card.change?.added ?? 0}</span>{" "}
          <span className="sfx-minus">−{card.change?.removed ?? 0}</span>{" "}
          <button type="button" className="ag-link" data-testid="sfx-card-view-diff" onClick={showDiff}
                  aria-expanded={open} title="The exact change, read only">View diff</button>
        </dd>
        <dt>Tests</dt>
        <dd data-testid="sfx-card-tests">
          {(card.tests?.new ?? []).map((t) => (
            <span key={t} className="sfx-redgreen" data-testid="sfx-card-new-test"
                  title="New regression test: fails on BOSS as it was, passes with this fix">
              <span className="tone-red">✗</span>→<span className="tone-green">✓</span> NEW {base(t)}
            </span>
          ))}
          <span className="sfx-sub" title="BOSS's tests that cover the changed code">✓ {card.tests?.affected ?? 0} affected</span>
          <span className="sfx-sub" title="The must-never-break tier, including the private-infrastructure test">✓ P0 tier</span>
          <span className="sfx-sub" title="No test deleted, skipped or loosened">
            {card.tests?.none_weakened ? "✓ none weakened" : "✗ a test was weakened"}
          </span>
        </dd>
        <dt>Review</dt>
        <dd data-testid="sfx-card-review" title={card.review_note || undefined}>
          {reviewSeats.map(([seat, r]) => (
            <span key={seat} className="sfx-seat" data-testid={`sfx-card-seat-${seat}`}
                  title={[r.engine ? `engine: ${r.engine}` : "", ...(r.findings ?? [])].filter(Boolean).join("\n") || r.verdict}>
              {SEATS[seat] ?? seat} {passed(r.verdict) ? "✓" : "✗"}
            </span>
          ))}
          {card.security_on_another_engine ? (
            <span className="sfx-sub" title="The security reviewer ran on a different engine from the one that wrote the fix">other engine</span>
          ) : null}
        </dd>
        <dt>Cost</dt>
        <dd data-testid="sfx-card-cost">
          {usd(card.cost_usd)}{card.minutes ? `, ${Math.round(card.minutes)} min` : ""}{card.engine ? `, ${card.engine}` : ""}
        </dd>
        <dt>On approve</dt>
        <dd data-testid="sfx-card-on-approve" title={card.on_approve}>Installs on this one-box and resumes the step</dd>
        <dt>Undo</dt>
        <dd data-testid="sfx-card-undo" title={card.undo}>Automatic, or Remove</dd>
      </dl>
      {open ? (
        <div className="sfx-diff" data-testid="sfx-card-diff">
          {diff ? <pre data-user-text>{diff.diff || "No diff recorded"}</pre>
            : diffError ? <span role="alert">{diffError}</span> : <span>Loading…</span>}
        </div>
      ) : null}
      <div className="sfx-card-foot">
        <span className="sfx-sub" data-testid="sfx-card-people" title={card.people}>
          {card.tier >= 3 ? "Two people on a shared install ⓘ" : "One approver ⓘ"}
        </span>
        {id ? <a className="ag-link" href={selfFixAreaHref(id)} data-testid="sfx-card-open"
                 title="The whole lineage of this fix">Self-fix</a> : null}
        {card.command ? <code className="sfx-sub" title="Or install it by hand on the host">{card.command}</code> : null}
      </div>
    </div>
  );
}
