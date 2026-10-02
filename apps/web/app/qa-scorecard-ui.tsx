import type { QaScorecardFeed } from "../lib/qa-scorecard";

export function QaScorecardIsland({ feed }: { feed: QaScorecardFeed }) {
  return (
    <article className="stack-card" data-testid="qa-scorecard">
      <h3>Official QA scorecard</h3>
      {/* WHAT THESE APPS ARE. The rows are BOSS's own benchmark apps (BugBox, HookWatch...),
          and on a customer's Evidence screen they read as unrelated projects that had
          appeared in their factory (console walk, 2026-10-02). */}
      <p className="detail">
        BOSS&apos;s own benchmark apps — the fixed test apps BOSS is measured on, not your
        projects. A UI or UX score that was not measured stays &ldquo;not scored&rdquo;; nothing
        here is ever marked passed without evidence.
      </p>
      {!feed.apiReachable ? (
        <p className="detail">{feed.blockedReason ?? "QA catalog unavailable."}</p>
      ) : feed.apps.length === 0 ? (
        <p className="detail">No official benchmark apps are catalogued.</p>
      ) : (
        <div className="feed-list">
          {feed.apps.map((app) => (
            <div className="feed-row" key={app.id} data-testid={`qa-scorecard-${app.id}`}>
              <div>
                <h4>{app.name}</h4>
                <p className="detail">
                  {app.reportMissing
                    ? "No official report recorded yet"
                    : app.honest === false
                      ? "honesty failed"
                      : app.passed
                        ? "passed with evidence"
                        : "not a certified pass"}
                </p>
                <p className="detail">
                  Scored: {app.scored.length ? app.scored.map(readable).join(", ") : "nothing yet"}
                  {" · "}
                  UI: {readable(app.ui ?? "not_scored")}
                  {" · "}
                  UX: {readable(app.ux ?? "not_scored")}
                </p>
                {app.invented.length > 0 ? (
                  <p className="detail">invented scores present — do not trust this card</p>
                ) : (
                  <p className="detail">No invented scores</p>
                )}
                {app.builtRunId ? (
                  <p className="detail">
                    run <code className="mono">{app.builtRunId}</code>
                    {app.runStatus ? ` · ${app.runStatus}` : ""}
                  </p>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}

/** A recorded token ("not_scored") in words ("not scored"). Raw field values read as a bug. */
function readable(value: string): string {
  return String(value).replace(/_/g, " ");
}
