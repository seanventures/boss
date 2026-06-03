import { resolveDecisionAction } from "./actions";
import { fetchDecisionFeed, type DecisionFeedItem } from "../lib/decisions";
import { fetchStatus } from "../lib/status";

function formatMicros(amount: unknown): string {
  if (typeof amount !== "number") {
    return "n/a";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(amount / 1_000_000);
}

function formatRecommendation(decision: DecisionFeedItem): string {
  const action = decision.recommendation.action;
  if (typeof action === "string" && action) {
    return action.replaceAll("_", " ");
  }
  return "review required";
}

function formatDeadline(deadlineAt?: string): string {
  if (!deadlineAt) {
    return "No deadline";
  }
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(deadlineAt));
}

export default async function HomePage() {
  const [status, decisions] = await Promise.all([
    fetchStatus(),
    fetchDecisionFeed(),
  ]);

  return (
    <main className="shell">
      <div className="frame">
        <section className="hero">
          <span className="eyebrow">Business Operating System Sentinel</span>
          <h1>Executive control for autonomous work.</h1>
          <p>
            Real GitHub and OpenAI integrations are gated behind receipts,
            reservations, and approvals. This shell is wired to the live control
            plane and stays visible even when integrations are blocked.
          </p>
          <span className={`pill${status.apiReachable ? "" : " warn"}`}>
            {status.apiReachable ? "API reachable" : "API unavailable"}
          </span>
        </section>

        <section className="grid">
          <article>
            <h2>Project</h2>
            <div className="metric">{status.projectName}</div>
            <p className="detail">
              {status.orgName} / {status.workspaceName} / {status.projectId}
            </p>
          </article>
          <article>
            <h2>Database</h2>
            <div className="metric">
              {status.databaseConnected ? "Connected" : "Offline"}
            </div>
            <p className="detail">
              Budget and workflow state are sourced from Postgres.
            </p>
          </article>
          <article>
            <h2>Budget State</h2>
            <div className="metric">{status.budgetState}</div>
            <p className="detail">
              Real model and GitHub actions will fail closed without an active
              reservation.
            </p>
          </article>
          <article>
            <h2>Pending Decisions</h2>
            <div className="metric">{status.pendingDecisions}</div>
            <p className="detail">
              Decisions stay explicit when credentials, budget, or approvals are
              missing.
            </p>
          </article>
        </section>

        <section className="grid">
          <article>
            <h2>Open Tasks</h2>
            <div className="metric">{status.openTasks}</div>
            <p className="detail">
              The first dogfood workspace is ready for command intake and
              software-factory execution.
            </p>
          </article>
          <article>
            <h2>Blocking State</h2>
            <div className="metric">
              {status.blockedReason ? "Visible" : "Clear"}
            </div>
            <p className="detail">
              {status.blockedReason ??
                "No bootstrap blocker reported by the control plane."}
            </p>
          </article>
        </section>

        <section className="panel-stack">
          <div className="section-head">
            <div>
              <span className="eyebrow">Decision Inbox</span>
              <h2>Explicit approvals before real effects.</h2>
            </div>
            <p className="status-meta">
              Decisions are served by the control plane API and expire to a safe
              default when their deadline passes.
            </p>
          </div>

          {!decisions.apiReachable ? (
            <article className="decision-card empty">
              <h3>Decision API unavailable</h3>
              <p className="detail">
                {decisions.blockedReason ??
                  "The executive console could not reach the decision inbox."}
              </p>
            </article>
          ) : decisions.items.length === 0 ? (
            <article className="decision-card empty">
              <h3>No active decisions</h3>
              <p className="detail">
                The control plane does not currently need an approval, rejection,
                or safe-default expiration.
              </p>
            </article>
          ) : (
            <div className="decision-list">
              {decisions.items.map((decision) => {
                const estimatedCost =
                  typeof decision.costSummary.estimated_usd_micros === "number"
                    ? decision.costSummary.estimated_usd_micros
                    : undefined;
                const isPending = decision.status === "pending";

                return (
                  <article className="decision-card" key={decision.id}>
                    <div className="decision-head">
                      <div>
                        <span className={`pill${isPending ? " warn" : ""}`}>
                          {decision.status}
                        </span>
                        <h3>{decision.question}</h3>
                      </div>
                      <div className="decision-meta">
                        <span>Risk tier {decision.riskTier}</span>
                        <span>Default: {decision.defaultAction ?? "hold"}</span>
                        <span>Deadline: {formatDeadline(decision.deadlineAt)}</span>
                      </div>
                    </div>

                    <div className="decision-grid">
                      <div>
                        <h4>Recommendation</h4>
                        <p className="detail">{formatRecommendation(decision)}</p>
                      </div>
                      <div>
                        <h4>Estimated Cost</h4>
                        <p className="detail">{formatMicros(estimatedCost)}</p>
                      </div>
                      <div>
                        <h4>Options</h4>
                        <p className="detail">
                          {decision.options
                            .map((option) =>
                              typeof option.label === "string"
                                ? option.label
                                : typeof option.action === "string"
                                  ? option.action
                                  : "review",
                            )
                            .join(" / ")}
                        </p>
                      </div>
                    </div>

                    <div className="decision-footer">
                      <p className="detail">
                        {decision.approvedBy
                          ? `Approved by ${decision.approvedBy} at ${formatDeadline(
                              decision.approvedAt,
                            )}.`
                          : "This card remains explicit until it is approved, rejected, or safely expired."}
                      </p>
                      {isPending ? (
                        <form action={resolveDecisionAction} className="decision-actions">
                          <input
                            type="hidden"
                            name="decision_id"
                            value={decision.id}
                          />
                          <button className="button" name="action" value="approve">
                            Approve
                          </button>
                          <button
                            className="button secondary"
                            name="action"
                            value="reject"
                          >
                            Reject
                          </button>
                        </form>
                      ) : null}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
