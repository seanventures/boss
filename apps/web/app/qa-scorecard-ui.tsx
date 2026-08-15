import type { QaScorecardFeed } from "../lib/qa-scorecard";

export function QaScorecardIsland({ feed }: { feed: QaScorecardFeed }) {
  return (
    <article className="stack-card" data-testid="qa-scorecard">
      <h3>Official QA scorecard</h3>
      <p className="detail">
        Honesty only. Missing UI/UX stays not scored. This island never invents
        a pass.
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
                    ? "official /report missing"
                    : app.honest === false
                      ? "honesty failed"
                      : app.passed
                        ? "passed with evidence"
                        : "not a certified pass"}
                </p>
                <p className="detail">
                  scored {app.scored.length ? app.scored.join(", ") : "none"}
                  {" · "}
                  ui {app.ui ?? "not_scored"}
                  {" · "}
                  ux {app.ux ?? "not_scored"}
                </p>
                {app.invented.length > 0 ? (
                  <p className="detail">invented scores present — do not trust this card</p>
                ) : (
                  <p className="detail">invented_scores empty</p>
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
